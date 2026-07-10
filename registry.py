"""
registry.py — job registry, slug derivation, and path guards.

The registry is keyed on the normalized repo URL. Each entry records the
current state of the one tutorial that exists for that repo. Regenerating
overwrites in place, so tutorial_url is stable forever.
"""

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration -----------------------------------------------------------

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/app/output")).resolve()
REGISTRY_FILE = Path(os.environ.get("REGISTRY_FILE", "/app/data/jobs.json"))
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://toolservice.abrahamtech.com/tutorials"
).rstrip("/")

OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


# --- URL validation and slug derivation --------------------------------------

# GitHub owner/repo names: alphanumeric, hyphen, underscore, period.
# Anchored, https only, no userinfo, no port, no path beyond owner/repo.
_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$"
)


class InvalidRepoURL(ValueError):
    pass


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Validate and split a GitHub URL into (owner, repo).

    Rejects anything that isn't a plain https://github.com/owner/repo.
    This is the only gate between agent input and `git clone`, so it is
    deliberately strict: no ssh, no ext::, no file://, no --upload-pack.
    """
    if not isinstance(repo_url, str):
        raise InvalidRepoURL("repo_url must be a string")
    repo_url = repo_url.strip()
    if len(repo_url) > 300:
        raise InvalidRepoURL("repo_url is implausibly long")
    m = _REPO_RE.match(repo_url)
    if not m:
        raise InvalidRepoURL(
            f"Not a valid public GitHub repository URL: {repo_url!r}. "
            "Expected https://github.com/owner/repo"
        )
    return m.group(1), m.group(2)


def normalize_repo_url(repo_url: str) -> str:
    """Canonical form used as the registry key. Case-insensitive, no .git."""
    owner, repo = parse_repo_url(repo_url)
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def slug_for(repo_url: str) -> str:
    """Filesystem- and URL-safe directory name: {owner}--{repo}.

    Lowercased; periods folded to hyphens so nothing resembles a dotfile
    or a traversal component. Two distinct GitHub repos cannot collide,
    because GitHub owner/repo pairs are unique case-insensitively.
    """
    owner, repo = parse_repo_url(repo_url)
    clean = lambda s: s.lower().replace(".", "-")
    return f"{clean(owner)}--{clean(repo)}"


# --- Path guards -------------------------------------------------------------

class PathEscape(RuntimeError):
    pass


def output_path_for(slug: str) -> Path:
    """Resolve a slug to its output directory, asserting containment.

    Every write and every delete goes through here. If a slug ever escapes
    OUTPUT_ROOT, we raise rather than touch the filesystem.
    """
    if not re.fullmatch(r"[a-z0-9_-]+--[a-z0-9_-]+", slug):
        raise PathEscape(f"Malformed slug: {slug!r}")
    candidate = (OUTPUT_ROOT / slug).resolve()
    if candidate != OUTPUT_ROOT and OUTPUT_ROOT not in candidate.parents:
        raise PathEscape(f"Slug escapes output root: {slug!r}")
    return candidate


def tutorial_url_for(slug: str) -> str:
    return f"{PUBLIC_BASE_URL}/{slug}/index.md"


def clear_output(slug: str) -> None:
    """Remove a tutorial's output directory. Idempotent.

    Called before regeneration so that a shrinking chapter count does not
    leave orphaned NN_*.md files that index.md no longer links to.
    """
    path = output_path_for(slug)
    if path.exists():
        shutil.rmtree(path)


def output_files(slug: str) -> list[str]:
    """Filenames present in a tutorial's output dir, sorted. Empty if none."""
    path = output_path_for(slug)
    if not path.is_dir():
        return []
    return sorted(p.name for p in path.iterdir() if p.is_file())


def has_output(slug: str) -> bool:
    return (output_path_for(slug) / "index.md").is_file()


# --- Registry ----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_unlocked() -> dict:
    if not REGISTRY_FILE.exists():
        return {"tutorials": {}}
    try:
        data = json.loads(REGISTRY_FILE.read_text())
    except json.JSONDecodeError:
        return {"tutorials": {}}
    data.setdefault("tutorials", {})
    return data


def _save_unlocked(data: dict) -> None:
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(REGISTRY_FILE)  # atomic on POSIX


def load() -> dict:
    with _lock:
        return _load_unlocked()


def get(repo_url: str) -> dict | None:
    key = normalize_repo_url(repo_url)
    return load()["tutorials"].get(key)


def get_by_slug(slug: str) -> dict | None:
    for entry in load()["tutorials"].values():
        if entry["slug"] == slug:
            return entry
    return None


def list_all() -> list[dict]:
    return list(load()["tutorials"].values())


def upsert(repo_url: str, **fields) -> dict:
    """Create or update the entry for a repo. Returns the merged entry."""
    key = normalize_repo_url(repo_url)
    slug = slug_for(repo_url)
    with _lock:
        data = _load_unlocked()
        entry = data["tutorials"].get(key, {
            "repo_url": key,
            "slug": slug,
            "created_at": _now(),
        })
        entry.update(fields)
        entry["slug"] = slug
        entry["tutorial_url"] = tutorial_url_for(slug)
        entry["updated_at"] = _now()
        data["tutorials"][key] = entry
        _save_unlocked(data)
        return entry


def delete(slug: str) -> bool:
    """Remove registry entry and output directory. Returns True if removed."""
    output_path_for(slug)  # validate before mutating anything
    with _lock:
        data = _load_unlocked()
        key = next(
            (k for k, v in data["tutorials"].items() if v["slug"] == slug), None
        )
        if key:
            del data["tutorials"][key]
            _save_unlocked(data)
    clear_output(slug)
    return key is not None


def reconcile(add_untracked: bool = True) -> dict:
    """Bring the registry into agreement with what is on disk.

    The registry drifts: a container restart orphans a running job, a manual
    rm leaves a stale entry. This is the same problem, and the same remedy,
    as reconcile_deployments in docker-deployer.
    """
    changes = []
    with _lock:
        data = _load_unlocked()
        tutorials = data["tutorials"]

        for key, entry in list(tutorials.items()):
            slug = entry["slug"]
            old = entry.get("status")
            if has_output(slug):
                if old != "complete":
                    entry["status"] = "complete"
                    changes.append(f"{slug}: {old} -> complete")
            elif old == "running":
                entry["status"] = "failed"
                entry["error"] = "no output on disk; worker likely died"
                changes.append(f"{slug}: running -> failed (orphaned)")
            elif old == "complete":
                entry["status"] = "missing"
                entry["error"] = "output directory removed outside the registry"
                changes.append(f"{slug}: complete -> missing")
            entry["last_checked"] = _now()

        if add_untracked:
            known = {e["slug"] for e in tutorials.values()}
            for path in OUTPUT_ROOT.iterdir():
                if not path.is_dir() or path.name in known:
                    continue
                if not (path / "index.md").is_file():
                    continue
                slug = path.name
                tutorials[f"untracked:{slug}"] = {
                    "repo_url": None,
                    "slug": slug,
                    "status": "complete",
                    "tutorial_url": tutorial_url_for(slug),
                    "source": "untracked",
                    "note": "discovered by reconcile; not generated through the API",
                    "created_at": _now(),
                    "updated_at": _now(),
                    "last_checked": _now(),
                }
                changes.append(f"{slug}: (untracked) -> added")

        _save_unlocked(data)

    return {
        "reconciled": True,
        "total_entries": len(load()["tutorials"]),
        "changes": changes or ["no changes — registry matches disk"],
    }

"""
server.py — FastAPI front end for the tutorial generator.

Owns validation, the registry, and enqueueing. Never serves markdown --
nginx does that from the output volume, statically. Never runs main.py --
worker.py does that.

Reached only via nginx: /api/ from the VPC, nothing from the internet.
"""

import json
import os

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

import registry

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QUEUE_KEY = "tutorial:queue"

r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(
    title="PocketFlow Tutorial Service",
    description="Generates beginner-friendly markdown tutorials from public GitHub repositories.",
    version="1.0.0",
)


# --- Request models ----------------------------------------------------------

class GenerateRequest(BaseModel):
    repo_url: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_size: int = Field(default=100_000, ge=1_000, le=1_000_000)
    language: str = "english"
    max_abstractions: int = Field(default=10, ge=1, le=20)
    on_existing: str = "ignore"  # "ignore" | "update"

    @field_validator("repo_url")
    @classmethod
    def _repo(cls, v: str) -> str:
        try:
            registry.parse_repo_url(v)
        except registry.InvalidRepoURL as e:
            raise ValueError(str(e))
        return v

    @field_validator("include", "exclude")
    @classmethod
    def _globs(cls, v: list[str]) -> list[str]:
        if len(v) > 40:
            raise ValueError("too many glob patterns")
        for pattern in v:
            if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
                raise ValueError(f"invalid glob pattern: {pattern!r}")
            if "\x00" in pattern or pattern.startswith("-"):
                raise ValueError(f"invalid glob pattern: {pattern!r}")
        return v

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        if not v.replace(" ", "").isalpha() or len(v) > 40:
            raise ValueError("language must be a plain language name, e.g. 'english'")
        return v

    @field_validator("on_existing")
    @classmethod
    def _on_existing(cls, v: str) -> str:
        if v not in ("ignore", "update"):
            raise ValueError("on_existing must be 'ignore' or 'update'")
        return v


# --- Endpoints ---------------------------------------------------------------

@app.get("/healthz")
def healthz():
    try:
        r.ping()
        redis_ok = True
    except redis.ConnectionError:
        redis_ok = False
    return {"ok": redis_ok, "redis": "up" if redis_ok else "down"}


@app.post("/jobs", status_code=202)
def create_job(req: GenerateRequest):
    """Enqueue a tutorial generation job.

    Returns immediately. Generation takes 5-30 minutes; poll GET /jobs/{slug}.
    """
    slug = registry.slug_for(req.repo_url)
    existing = registry.get(req.repo_url)

    if existing and req.on_existing == "ignore":
        if existing.get("status") == "running":
            return {
                **existing,
                "status": "running",
                "next": f"Already generating. Poll GET /jobs/{slug}.",
            }
        if existing.get("status") == "complete" and registry.has_output(slug):
            return {
                **existing,
                "status": "exists",
                "next": (
                    f"A tutorial already exists at {existing['tutorial_url']}. "
                    "Fetch that URL, or resubmit with on_existing='update' to regenerate."
                ),
            }

    if existing and existing.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"A job for {slug} is already running. Wait for it to finish.",
        )

    job = {
        "slug": slug,
        "repo_url": req.repo_url,
        "include": req.include,
        "exclude": req.exclude,
        "max_size": req.max_size,
        "language": req.language,
        "max_abstractions": req.max_abstractions,
    }

    entry = registry.upsert(
        req.repo_url,
        status="queued",
        progress="waiting for a worker",
        error=None,
        language=req.language,
        max_abstractions=req.max_abstractions,
    )

    try:
        r.rpush(QUEUE_KEY, json.dumps(job))
    except redis.ConnectionError:
        registry.upsert(req.repo_url, status="failed", error="queue unreachable")
        raise HTTPException(status_code=503, detail="Job queue unavailable.")

    return {
        **entry,
        "next": (
            f"Generation takes 5-30 minutes. Poll GET /jobs/{slug} until "
            "status is 'complete', then fetch tutorial_url."
        ),
    }


@app.get("/jobs")
def list_jobs():
    """Every tutorial the service knows about."""
    tutorials = registry.list_all()
    return {"count": len(tutorials), "tutorials": tutorials}


@app.get("/jobs/{slug}")
def get_job(slug: str):
    """Status of one tutorial, keyed by slug."""
    try:
        registry.output_path_for(slug)  # shape + containment check
    except registry.PathEscape:
        raise HTTPException(status_code=400, detail=f"Malformed slug: {slug!r}")

    entry = registry.get_by_slug(slug)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No tutorial for slug {slug!r}")

    status = entry.get("status")
    if status == "complete":
        nxt = f"Ready. Fetch {entry['tutorial_url']} to read the tutorial."
    elif status in ("queued", "running"):
        nxt = f"Still generating. Wait 2-5 minutes, then poll GET /jobs/{slug} again."
    elif status == "failed":
        nxt = "Generation failed. See 'error' and 'log_tail'."
    else:
        nxt = f"Unexpected status {status!r}. Call POST /reconcile."

    return {**entry, "next": nxt}


@app.delete("/jobs/{slug}")
def delete_job(slug: str):
    """Remove a tutorial's registry entry and its output directory."""
    try:
        registry.output_path_for(slug)
    except registry.PathEscape:
        raise HTTPException(status_code=400, detail=f"Malformed slug: {slug!r}")

    entry = registry.get_by_slug(slug)
    if entry and entry.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"{slug} is currently generating. Wait for it to finish.",
        )

    removed = registry.delete(slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No tutorial for slug {slug!r}")
    return {"slug": slug, "deleted": True}


@app.post("/reconcile")
def reconcile(add_untracked: bool = True):
    """Bring the registry into agreement with what is on disk."""
    return registry.reconcile(add_untracked=add_untracked)

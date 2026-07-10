"""
worker.py — consumes generation jobs from redis and runs main.py.

One job at a time, in-process. Generation is slow (5-30 min) and LLM-bound,
so concurrency buys nothing here and would multiply spend on a mistake.
Scale by adding worker replicas if that ever changes.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import redis

import registry

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QUEUE_KEY = "tutorial:queue"
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "3600"))  # 1 hour
REPO_ROOT = Path(__file__).parent.resolve()

r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=60)

def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def build_command(job: dict) -> list[str]:
    """Assemble the main.py invocation.

    Every value is passed as a separate list element. subprocess is called
    with shell=False, so nothing here is interpreted by a shell -- an
    --exclude of "$(rm -rf /)" is a glob that matches nothing.
    """
    cmd = [
        sys.executable,
        "main.py",
        "--repo", job["repo_url"],
        "-n", job["slug"],
        "-o", str(registry.OUTPUT_ROOT),
        "--max-size", str(job["max_size"]),
        "--language", job["language"],
        "--max-abstractions", str(job["max_abstractions"]),
    ]
    if job["include"]:
        cmd += ["--include", *job["include"]]
    if job["exclude"]:
        cmd += ["--exclude", *job["exclude"]]
    return cmd


# main.py prints "Preparing to write N chapters..." and per-chapter lines.
# Crude, but it is the only progress signal the CLI offers.
_CHAPTER_TOTAL = re.compile(r"Preparing to write (\d+) chapters")
_CHAPTER_DONE = re.compile(r"Finished writing (\d+) chapters")


def run_job(job: dict) -> None:
    slug = job["slug"]
    repo_url = job["repo_url"]

    log(f"starting {slug} <- {repo_url}")
    registry.upsert(
        repo_url,
        status="running",
        progress="cloning repository",
        error=None,
        started_at=registry._now(),
    )

    # A shrinking chapter count would otherwise leave orphaned NN_*.md files
    # that index.md no longer links to.
    try:
        registry.clear_output(slug)
    except registry.PathEscape as e:
        registry.upsert(repo_url, status="failed", error=str(e))
        log(f"REFUSED {slug}: {e}")
        return

    env = os.environ.copy()
    cmd = build_command(job)
    log(f"exec: {' '.join(cmd)}")

    tail: list[str] = []
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
        )

        for line in proc.stdout:
            line = line.rstrip()
            log(line)
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)

            if m := _CHAPTER_TOTAL.search(line):
                registry.upsert(repo_url, progress=f"writing {m.group(1)} chapters")
            elif _CHAPTER_DONE.search(line):
                registry.upsert(repo_url, progress="assembling tutorial")
            elif "Identified" in line or "abstraction" in line.lower():
                registry.upsert(repo_url, progress="identifying abstractions")

            if time.monotonic() - started > JOB_TIMEOUT:
                proc.kill()
                raise TimeoutError(f"exceeded {JOB_TIMEOUT}s")

        rc = proc.wait()

    except Exception as e:
        registry.upsert(
            repo_url,
            status="failed",
            progress=None,
            error=f"{type(e).__name__}: {e}",
            log_tail=tail[-20:],
        )
        log(f"FAILED {slug}: {e}")
        return

    elapsed = int(time.monotonic() - started)

    if rc != 0:
        registry.upsert(
            repo_url,
            status="failed",
            progress=None,
            error=f"main.py exited {rc}",
            log_tail=tail[-20:],
            duration_seconds=elapsed,
        )
        log(f"FAILED {slug}: exit {rc}")
        return

    # Trust the filesystem, not the exit code.
    if not registry.has_output(slug):
        registry.upsert(
            repo_url,
            status="failed",
            progress=None,
            error="main.py exited 0 but wrote no index.md",
            log_tail=tail[-20:],
            duration_seconds=elapsed,
        )
        log(f"FAILED {slug}: no index.md")
        return

    entry = registry.upsert(
        repo_url,
        status="complete",
        progress=None,
        error=None,
        files=registry.output_files(slug),
        duration_seconds=elapsed,
        completed_at=registry._now(),
    )
    log(f"complete {slug} in {elapsed}s -> {entry['tutorial_url']}")


def main() -> None:
    log(f"connected to {REDIS_URL}, waiting on {QUEUE_KEY}")

    # A job that was running when the container died is orphaned. Mark it.
    registry.reconcile(add_untracked=False)

    while True:
        try:
            item = r.blpop(QUEUE_KEY, timeout=30)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            log(f"redis: {type(e).__name__}: {e}; retrying in 5s")
            time.sleep(5)
            continue

        if item is None:
            continue

        _, payload = item
        try:
            job = json.loads(payload)
        except json.JSONDecodeError:
            log(f"discarding malformed job: {payload[:200]}")
            continue

        try:
            run_job(job)
        except Exception as e:
            log(f"unhandled error on {job.get('slug')}: {e}")


if __name__ == "__main__":
    main()

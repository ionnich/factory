"""Factory-owned repo mirrors at trunk. Never firstmate's projects/ clones."""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

from . import db
from .config import Config, secret


def git(path: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {path}: {r.stderr.strip()}")
    return r.stdout.strip()


def fetch(cfg: Config, repo: str) -> str:
    """Fetch trunk and hard-reset the mirror to it; returns the SHA. Touches only the mirror (thread-safe)."""
    path, branch = cfg.mirror_path(repo), cfg.trunk(repo)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--branch", branch, f"https://github.com/{repo}.git", str(path)],
                       check=True, capture_output=True, timeout=900)
    git(path, "fetch", "--quiet", "--prune", "origin", branch)
    git(path, "reset", "--quiet", "--hard", f"origin/{branch}")
    git(path, "clean", "-qfdx")
    return git(path, "rev-parse", "HEAD")


def sync_all(cfg: Config, conn, only: set[str] | None = None) -> dict[str, str]:
    """Fetch every mapped repo (or `only` these) in parallel, then record their trunk SHAs (no DB lock while git
    runs)."""
    # gh's git credential helper needs the token; cron/launchd envs don't carry it.
    os.environ.setdefault("GITHUB_TOKEN", secret(cfg, "GITHUB_TOKEN"))
    want = sorted(only if only is not None else {c.repo for c in cfg.contexts})
    with ThreadPoolExecutor(max_workers=max(1, len(want))) as pool:
        shas = dict(zip(want, pool.map(lambda r: fetch(cfg, r), want)))
    with db.tx(conn):
        for repo, sha in shas.items():
            conn.execute(
                "INSERT INTO repo_trunk VALUES (?,?,?,?) ON CONFLICT(repo) DO UPDATE SET "
                "branch=excluded.branch, sha=excluded.sha, fetched_at=excluded.fetched_at",
                (repo, cfg.trunk(repo), sha, db.now()))
    return shas


@cache
def changed_paths(mirror: Path, old: str, new: str) -> frozenset[str] | None:
    """Paths touched between two trunk SHAs; None when old is unknown to the mirror."""
    if old == new:
        return frozenset()
    if subprocess.run(["git", "-C", str(mirror), "cat-file", "-e", f"{old}^{{commit}}"],
                      capture_output=True).returncode:
        return None
    return frozenset(git(mirror, "diff", "--name-only", old, new).splitlines())


def path_exists(mirror: Path, sha: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(mirror), "cat-file", "-e", f"{sha}:{path}"],
                          capture_output=True).returncode == 0

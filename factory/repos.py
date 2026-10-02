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


def sync_all(cfg: Config, conn, only: set[str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch every mapped repo (or `only` these) in parallel, then record their trunk SHAs (no DB lock while git
    runs). A timed-out or failed repo keeps its last recorded SHA and is returned in the errors dict; it does
    not fail the rest."""
    # gh's git credential helper needs the token; cron/launchd envs don't carry it.
    os.environ.setdefault("GITHUB_TOKEN", secret(cfg, "GITHUB_TOKEN"))
    want = sorted(only if only is not None else {c.repo for c in cfg.contexts})

    def one(repo: str) -> tuple[str, str | None, str | None]:
        try:
            return repo, fetch(cfg, repo), None
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
            return repo, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, len(want) or 1)) as pool:
        results = list(pool.map(one, want))
    shas, errors = {}, {}
    now = db.now()
    with db.tx(conn):
        for repo, sha, err in results:
            if sha:
                conn.execute(
                    "INSERT INTO repo_trunk VALUES (?,?,?,?) ON CONFLICT(repo) DO UPDATE SET "
                    "branch=excluded.branch, sha=excluded.sha, fetched_at=excluded.fetched_at",
                    (repo, cfg.trunk(repo), sha, now))
                shas[repo] = sha
                continue
            errors[repo] = err
            prev = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (repo,)).fetchone()
            if prev:
                shas[repo] = prev["sha"]
    return shas, errors


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

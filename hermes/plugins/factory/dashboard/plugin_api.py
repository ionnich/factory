"""Factory tab backend. Every figure and every action goes through the `factory` CLI (single source of
truth, which enforces the invariants) plus Hermes's own cron job records. Writes: stage, handoff, resolve
flag. The dashboard sits behind Hermes login on the tailnet, so a click by the logged-in user is the human
approval. POSTs take JSON bodies only (a cross-site form or no-cors fetch can't send application/json)."""
import asyncio
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()

HOME = Path.home()
FACTORY = str(HOME / ".local/bin/factory")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
# factory shells out to git for evidence freshness; the dashboard may run with a minimal PATH.
ENV = {**os.environ, "PATH": ":".join([str(HOME / ".local/bin"), "/etc/profiles/per-user/nich/bin",
                                       "/run/current-system/sw/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"])}
RUN_ID = re.compile(r"^[\w.:-]{1,80}$")
IDENT = re.compile(r"^[A-Z]+-\d+$")


async def factory(*args: str, timeout: float = 60):
    proc = await asyncio.create_subprocess_exec(FACTORY, *args, env=ENV, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise HTTPException(504, f"factory {' '.join(args)} timed out")
    err = err.decode().strip()
    if proc.returncode == 1 and "factory: refused: " in err:  # an invariant said no: the text is for the user
        raise HTTPException(409, err.rsplit("factory: refused: ", 1)[1].strip())
    if proc.returncode:
        raise HTTPException(502, f"factory {' '.join(args)}: {err[-400:]}")
    return json.loads(out)


def jobs() -> list[dict]:
    path = HERMES_HOME / "cron" / "jobs.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    keep = ("name", "schedule_display", "last_run_at", "last_status", "last_error", "next_run_at",
            "paused_at", "enabled")
    return [{k: j.get(k) for k in keep} for j in (data.get("jobs", data) if isinstance(data, dict) else data)
            if str(j.get("name", "")).startswith("factory-")]


@router.get("/overview")
async def overview():
    status, tickets, candidates = await asyncio.gather(factory("status"), factory("tickets"), factory("candidates"))
    runs = [d["run_id"] for d in status["dispatches"] if RUN_ID.match(d["run_id"])]
    dispatches = await asyncio.gather(*(factory("status", r) for r in runs))
    return {"status": status, "tickets": tickets, "candidates": candidates, "dispatches": dispatches, "jobs": jobs()}


class Stage(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=20)


class Handoff(BaseModel):
    run_id: str


class Resolve(BaseModel):
    resolution: str = Field(min_length=1, max_length=2000)


@router.post("/stage")
async def stage(body: Stage):
    bad = [i for i in body.identifiers if not IDENT.match(i)]
    if bad:
        raise HTTPException(422, f"not a ticket identifier: {', '.join(bad)}")
    return await factory("stage", *body.identifiers, "--actor", "user:dashboard", timeout=120)


@router.post("/handoff")
async def handoff(body: Handoff):
    if not RUN_ID.match(body.run_id) or body.run_id.startswith("-"):
        raise HTTPException(422, "bad run_id")
    return await factory("handoff", body.run_id, timeout=240)  # may have to start the executor agent


@router.post("/flags/{flag_id}/resolve")
async def resolve_flag(flag_id: int, body: Resolve):
    text = body.resolution.strip()
    if not text:
        raise HTTPException(422, "resolution is empty")
    return await factory("flag", "resolve", str(flag_id), f"--resolution={text}")


@router.get("/metrics")
async def metrics(days: int = Query(28, ge=1, le=365)):
    return await factory("metrics", "--days", str(days))

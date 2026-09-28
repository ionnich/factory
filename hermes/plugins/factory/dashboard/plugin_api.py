"""Factory status tab backend: read-only. Every figure comes from the `factory` CLI's read
commands (single source of truth) plus Hermes's own cron job records. No route writes."""
import asyncio
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

HOME = Path.home()
FACTORY = str(HOME / ".local/bin/factory")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
# factory shells out to git for evidence freshness; the dashboard may run with a minimal PATH.
ENV = {**os.environ, "PATH": ":".join([str(HOME / ".local/bin"), "/etc/profiles/per-user/nich/bin",
                                       "/run/current-system/sw/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"])}
RUN_ID = re.compile(r"^[\w.:-]{1,80}$")


async def factory(*args: str):
    proc = await asyncio.create_subprocess_exec(FACTORY, *args, env=ENV, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 60)
    except TimeoutError:
        proc.kill()
        raise HTTPException(504, f"factory {' '.join(args)} timed out")
    if proc.returncode:
        raise HTTPException(502, f"factory {' '.join(args)}: {err.decode()[-400:].strip()}")
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
    status, tickets = await asyncio.gather(factory("status"), factory("tickets"))
    runs = [d["run_id"] for d in status["dispatches"] if RUN_ID.match(d["run_id"])]
    dispatches = await asyncio.gather(*(factory("status", r) for r in runs))
    return {"status": status, "tickets": tickets, "dispatches": dispatches, "jobs": jobs()}

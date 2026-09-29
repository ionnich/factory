"""Factory tab backend. Every figure and every action goes through the `factory` CLI (single source of
truth, which enforces the invariants) plus Hermes's own cron job records. Writes: stage (a draft), draft notes,
and answering decisions (approve/hold/reject a draft, questions, blocked tickets, held Linear writes, executor). The dashboard sits behind Hermes login on the tailnet, so a click by
the logged-in user is the human approval. POSTs take JSON bodies only (a cross-site form or no-cors fetch can't
send application/json). /stream tells an open tab when factory.db or the cron jobs changed, so it refreshes at
once instead of polling."""
import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()

HOME = Path.home()
FACTORY = str(HOME / ".local/bin/factory")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
DB = Path(os.environ.get("FACTORY_DB", HOME / ".hermes/factory.db"))  # factory.toml [paths] db
# factory shells out to git for evidence freshness; the dashboard may run with a minimal PATH.
ENV = {**os.environ, "PATH": ":".join([str(HOME / ".local/bin"), "/etc/profiles/per-user/nich/bin",
                                       "/run/current-system/sw/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"])}
RUN_ID = re.compile(r"^[\w.:-]{1,80}$")
IDENT = re.compile(r"^[A-Z]+-\d+$")
NODE = re.compile(r"^(root|[A-Z]+-\d+(/[\d.]+)?)$")


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


# The factory's cron jobs (default profile) and the planner bot's routine (its own cron store).
JOB_FILES = (HERMES_HOME / "cron" / "jobs.json", HERMES_HOME / "profiles" / "planner" / "cron" / "jobs.json")


def jobs() -> list[dict]:
    keep = ("name", "schedule_display", "last_run_at", "last_status", "last_error", "next_run_at",
            "paused_at", "enabled")
    out = []
    for path in JOB_FILES:
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        out += [{k: j.get(k) for k in keep} for j in (data.get("jobs", data) if isinstance(data, dict) else data)
                if str(j.get("name", "")).startswith(("factory-", "[bot:planner]"))]
    return out


@router.get("/overview")
async def overview():
    return {**await factory("overview"), "jobs": jobs()}


def _stamp(conn: sqlite3.Connection) -> tuple:
    """Moves when another connection commits to factory.db (PRAGMA data_version on this connection; reads don't
    move it) or a cron job record changes."""
    return (conn.execute("PRAGMA data_version").fetchone()[0],
            *(p.stat().st_mtime_ns if p.exists() else None for p in JOB_FILES))


@router.get("/stream")
async def stream(request: Request):
    async def events():
        conn = sqlite3.connect(DB, isolation_level=None, timeout=5)
        try:
            last, idle = _stamp(conn), 0
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(1)
                now = _stamp(conn)
                if now != last:
                    last, idle = now, 0
                    yield "event: change\ndata: {}\n\n"
                elif (idle := idle + 1) >= 15:  # keep proxies from closing a quiet stream
                    idle = 0
                    yield ": ping\n\n"
        finally:
            conn.close()
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Stage(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=20)


class Note(BaseModel):
    node: str
    body: str = Field(min_length=1, max_length=4000)


class Choice(BaseModel):
    option: str = Field(min_length=1, max_length=40, pattern=r"^[\w-]+$")
    note: str | None = Field(default=None, max_length=4000)


@router.post("/stage")
async def stage(body: Stage):
    bad = [i for i in body.identifiers if not IDENT.match(i)]
    if bad:
        raise HTTPException(422, f"not a ticket identifier: {', '.join(bad)}")
    return await factory("stage", *body.identifiers, "--actor", "user:dashboard", timeout=120)


def run_id_ok(run_id: str) -> str:
    if not RUN_ID.match(run_id) or run_id.startswith("-"):
        raise HTTPException(422, "bad run_id")
    return run_id


def text_ok(text: str, what: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(422, f"{what} is empty")
    return text


@router.post("/drafts/{run_id}/notes")
async def draft_note(run_id: str, body: Note):
    if not NODE.match(body.node):
        raise HTTPException(422, "bad node id")
    return await factory("draft", "note", run_id_ok(run_id), "--node", body.node,
                         f"--body={text_ok(body.body, 'note')}", "--actor", "user:dashboard")


@router.post("/decisions/{decision_id}")
async def choose(decision_id: int, body: Choice):
    # an approval freezes and hands off, and may have to start the executor agent first
    note = [f"--note={text_ok(body.note, 'note')}"] if body.note and body.note.strip() else []
    return await factory("decide", "choose", str(decision_id), body.option, *note, "--actor", "user:dashboard",
                         timeout=300)


@router.get("/metrics")
async def metrics(days: int = Query(28, ge=1, le=365)):
    return await factory("metrics", "--days", str(days))

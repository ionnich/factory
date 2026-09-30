""""Why?" on a decision: the planner explains inline. `new` records a pending ask and spawns a detached `factory ask
run <id>`, which runs the planner one-shot (file tools only) and stores its answer or the failure."""
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta

from . import db, decide, dispatch
from .dispatch import HERMES, StageError

TIMEOUT = 180  # seconds the planner gets; a pending ask older than this (+ slack) lost its wrapper
# No read-only terminal toolset exists in Hermes (`hermes tools list`): `file` is the least that can read the
# mirrors. It can also write files; the prompt forbids it, and the mirrors are re-synced from trunk anyway.
TOOLSETS = "file"
SESSION = re.compile(r"^session_id:\s*(\S+)", re.M)  # hermes -Q prints it on stderr for wrappers

RULES = """Rules: explain only. Change nothing: no file writes, no factory commands, no Linear. Read code in the
mirror if it helps. Answer in at most 5 short lines, plain text, citing file:line for any claim about code."""


def rows(conn, decision_ids) -> dict:
    ids = list(decision_ids)
    out = {}
    for r in conn.execute(f"SELECT * FROM ask WHERE decision_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
        out.setdefault(r["decision_id"], []).append(dict(r))
    return out


def _session(conn, decision_id: int) -> str | None:
    r = conn.execute("SELECT session_id FROM ask WHERE decision_id=? AND session_id IS NOT NULL ORDER BY id DESC "
                     "LIMIT 1", (decision_id,)).fetchone()
    return r["session_id"] if r else None


def prompt(cfg, conn, d: dict, question: str, resumed: bool) -> str:
    if resumed:  # the session already holds the decision and the plan
        return f"Follow-up question about the same decision:\n{question}\n\n{RULES}"
    lines = [f"The user asks about a factory decision you may have raised (#{d['id']}, {d['kind']}, "
             f"on {d['node_id']}).", f"Decision: {d['question']}", "Options:"]
    lines += [f"- {o['id']}: {o['label']} -> {o['leads_to']}" for o in d["options"]]
    lines.append(f"Recommended: {d['recommended']} because: {d['why']}")
    if d["run_id"]:
        lines += ["", f"Plan tree of dispatch {d['run_id']}:"]
        lines += [f"- {n['id']} ({n['kind']}, under {n['parent']}): {n['title']}"
                  + (f" | {n['detail']}" if n["detail"] else "")
                  + (f" | depends on {', '.join(n['depends_on'])}" if n["depends_on"] else "")
                  for n in dispatch.tree(conn, d["run_id"])]
        lines += ["", "Tickets (title, verdict, repo mirror):"]
        for t in conn.execute(
                "SELECT t.identifier, json_extract(l.raw_json, '$.title') title, v.kind, v.reason, v.repo "
                "FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id LEFT JOIN linear_latest l "
                "ON l.issue_id=t.issue_id WHERE t.run_id=? ORDER BY t.rowid", (d["run_id"],)):
            mirror = cfg.mirror_path(t["repo"]) if t["repo"] else "no repo"
            lines.append(f"- {t['identifier']} {t['title']}: {t['kind']} ({t['reason']}); {mirror}")
    lines += ["", f"The user's question:\n{question}", "", RULES]
    return "\n".join(lines)


def command(query_file: str, session: str | None) -> list[str]:
    return [HERMES, "-p", "planner", "chat", "--oneshot", "-Q", "-t", TOOLSETS, "--run-budget", str(TIMEOUT - 30),
            "--query-file", query_file, *(["--resume", session] if session else [])]


def new(conn, decision_id: int, text: str, actor: str, spawn=subprocess.Popen) -> dict:
    text = text.strip()
    if not 1 <= len(text) <= 2000:
        raise StageError("ask 1-2000 characters")
    if decide.one(conn, decision_id) is None:
        raise StageError(f"no decision #{decision_id}")
    cutoff = (datetime.now(UTC) - timedelta(seconds=TIMEOUT + 60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db.tx(conn):
        conn.execute("UPDATE ask SET status='failed', error='no answer (the planner run vanished)', answered_at=? "
                     "WHERE decision_id=? AND status='pending' AND asked_at < ?", (db.now(), decision_id, cutoff))
        if conn.execute("SELECT 1 FROM ask WHERE decision_id=? AND status='pending'", (decision_id,)).fetchone():
            raise StageError(f"decision #{decision_id} already has a question the planner is answering")
        aid = conn.execute("INSERT INTO ask(decision_id, question, asked_by, asked_at) VALUES (?,?,?,?)",
                           (decision_id, text, actor, db.now())).lastrowid
    # detached: the dashboard/CLI call returns at once; the wrapper outlives it and writes the answer
    spawn([sys.executable, "-m", "factory", "ask", "run", str(aid)], start_new_session=True,
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ask": aid, "decision": decision_id, "status": "pending"}


def answer(conn, aid: int, text: str, session: str | None = None) -> dict:
    with db.tx(conn):
        n = conn.execute("UPDATE ask SET status='answered', answer=?, session_id=?, answered_at=? "
                         "WHERE id=? AND status='pending'", (text.strip(), session, db.now(), aid)).rowcount
    if not n:
        raise StageError(f"ask #{aid} is not pending")
    return {"ask": aid, "status": "answered"}


def fail(conn, aid: int, error: str, session: str | None = None) -> dict:
    with db.tx(conn):
        conn.execute("UPDATE ask SET status='failed', error=?, session_id=?, answered_at=? WHERE id=? AND "
                     "status='pending'", (error[-500:], session, db.now(), aid))
    return {"ask": aid, "status": "failed", "error": error[-500:]}


def run(cfg, conn, aid: int, runner=subprocess.run) -> dict:
    """The detached wrapper: ask the planner, store what came back."""
    a = conn.execute("SELECT * FROM ask WHERE id=?", (aid,)).fetchone()
    if a is None or a["status"] != "pending":
        raise StageError(f"ask #{aid} is not pending")
    session = _session(conn, a["decision_id"])
    d = decide.one(conn, a["decision_id"])
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="factory-ask-") as f:
        f.write(prompt(cfg, conn, d, a["question"], resumed=bool(session)))
        f.flush()
        try:
            r = runner(command(f.name, session), capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return fail(conn, aid, f"no answer within {TIMEOUT}s", session)
        except OSError as e:
            return fail(conn, aid, f"could not start hermes: {e}", session)
    m = SESSION.search(r.stderr or "")
    sid = m[1] if m else session
    if r.returncode != 0 or not r.stdout.strip():
        return fail(conn, aid, f"planner exited {r.returncode}: {(r.stderr or r.stdout or '').strip()}", sid)
    return answer(conn, aid, r.stdout, sid)


def cli(cfg, conn, a) -> dict | list:
    if a.acmd == "new":
        return new(conn, a.decision_id, a.text, a.actor)
    if a.acmd == "run":
        return run(cfg, conn, a.id)
    if a.acmd == "answer":
        text = a.text if a.text is not None else open(a.file).read()
        return answer(conn, a.id, text, a.session)
    return rows(conn, [a.decision_id]).get(a.decision_id, [])

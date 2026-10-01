"""Cost ledger: what each factory stage spends, read from the agents' own session records (Hermes state.db files
and omp session logs). Derived data, rebuilt incrementally from those records; never a source of truth."""
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db

HERMES_STAGE = {"factory-prune": "prune", "factory-reconcile": "reconcile", "[bot:planner] Plan drafts": "plan"}
EXECUTION = ("captain", "secondmate", "crew")
OMP_SESSIONS = Path.home() / ".omp" / "agent" / "sessions"
FLEET_DIR = "-.local-share-factory-fleet-homes-"
RUN_ID = re.compile(r"\b\d{8}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*\b")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hermes(home: Path) -> list[tuple]:
    """Factory sessions in every Hermes profile: the factory cron agents by job name, plus the factory and planner
    profiles' chats."""
    rows = []
    for state in [home / "state.db", *home.glob("profiles/*/state.db")]:
        profile = "default" if state.parent == home else state.parent.name
        jobs_file = state.parent / "cron" / "jobs.json"
        jobs = json.loads(jobs_file.read_text()).get("jobs", []) if jobs_file.exists() else []
        stage_of = {j["id"]: HERMES_STAGE[j.get("name")] for j in jobs if j.get("name") in HERMES_STAGE}
        src = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
        try:
            for sid, started, ended, inp, out, cached, usd in src.execute(
                    "SELECT id, started_at, ended_at, input_tokens, output_tokens + coalesce(reasoning_tokens, 0), "
                    "cache_read_tokens, coalesce(actual_cost_usd, estimated_cost_usd, 0) FROM sessions"):
                m = re.match(r"cron_([0-9a-f]+)_", sid)
                stage = stage_of.get(m[1]) if m else ("chat" if profile in ("factory", "planner") else None)
                if stage and started:
                    rows.append((f"hermes:{profile}:{sid}", stage, None, _iso(float(started)), float(ended or started),
                                 inp or 0, out or 0, cached or 0, usd or 0.0))
        finally:
            src.close()
    return rows


def _omp_stage(d: str) -> str | None:
    if d.startswith(FLEET_DIR):
        return "captain" if d == FLEET_DIR + "factory-primary" else "secondmate"
    return "crew" if d.startswith("-.treehouse-") else None


def _omp(path: Path, stage: str, runs: set[str]) -> tuple | None:
    text = path.read_text(errors="replace")
    run = next((r for r in RUN_ID.findall(text) if r in runs), None)
    # ponytail: crews share treehouse worktrees with nix-fleet; one counts as factory work only when it names a
    # dispatch or factory-fleet. Upgrade: crews record their dispatch id at spawn.
    if stage == "crew" and run is None and not any(m in text for m in ("factory-fleet", "/fm-fx-", "dispatch-intake")):
        return None
    started = next((json.loads(line).get("timestamp") for line in text.splitlines()[:5]
                    if '"type":"session"' in line), None) or _iso(path.stat().st_mtime)
    inp = out = cached = 0
    usd = 0.0
    for line in text.splitlines():
        if '"usage"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        u = (o.get("message") or {}).get("usage") or o.get("usage")
        if isinstance(u, dict):
            inp += u.get("input") or 0
            out += (u.get("output") or 0) + (u.get("reasoningTokens") or 0)
            cached += u.get("cacheRead") or 0
            usd += (u.get("cost") or {}).get("total") or 0
    return started[:19] + "Z", run, inp, out, cached, usd


def sync(conn, home: Path, sessions: Path = OMP_SESSIONS) -> int:
    """Refresh cost_session. Hermes rows are cheap to re-read; omp logs are re-read only when the file changed."""
    runs = {r[0] for r in conn.execute("SELECT run_id FROM dispatch")}
    seen = {r[0]: r[1] for r in conn.execute("SELECT id, mtime FROM cost_session WHERE id LIKE 'omp:%'")}
    rows = _hermes(home)
    for d in (p for p in sessions.iterdir() if p.is_dir() and _omp_stage(p.name)) if sessions.exists() else ():
        for f in d.glob("*.jsonl"):
            key, mtime = f"omp:{f}", f.stat().st_mtime
            if seen.get(key) == mtime:
                continue
            stage = _omp_stage(d.name)
            r = _omp(f, stage, runs)
            # Not factory work: remember the file (stage 'other', no cost) so it isn't re-read until it changes.
            started, run, inp, out, cached, usd = r if r else (_iso(mtime), None, 0, 0, 0, 0.0)
            rows.append((key, stage if r else "other", run, started, mtime, inp, out, cached, usd))
    with db.tx(conn):
        conn.executemany("INSERT OR REPLACE INTO cost_session VALUES (?,?,?,?,?,?,?,?,?)", rows)
        # Execution sessions that name no dispatch are attributed only when exactly one dispatch overlaps the
        # session's window. Under bounded parallel execution several can overlap; leave the run_id NULL rather than
        # fabricate an attribution onto the latest one (the stage spend stays visible in by_stage/per_week).
        conn.execute("""UPDATE cost_session SET run_id = (
            SELECT CASE WHEN count(*) = 1 THEN min(d.run_id) END FROM dispatch d
              WHERE d.executing_at IS NOT NULL AND d.executing_at <= cost_session.started_at
                AND coalesce(d.done_at, '9999') >= cost_session.started_at)
            WHERE run_id IS NULL AND stage IN ('captain', 'secondmate', 'crew')""")
    return len(rows)


def summary(conn, days: int) -> dict:
    """Spend by stage, per unit of work (verdict, plan, dispatch) and per week, over the last `days`."""
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
    stage = {r[0]: round(r[1], 2) for r in conn.execute(
        "SELECT stage, sum(usd) FROM cost_session WHERE started_at >= ? AND stage <> 'other' GROUP BY stage", (since,))}
    per_dispatch = {r[0]: round(r[1], 2) for r in conn.execute(
        f"SELECT run_id, sum(usd) FROM cost_session WHERE run_id IS NOT NULL AND stage IN {EXECUTION} "
        "GROUP BY run_id")}
    verdicts = one("SELECT count(*) FROM verdict WHERE created_by NOT LIKE 'factory:%' AND created_at >= ?", since)
    plans = one("SELECT count(*) FROM dispatch WHERE planned_at >= ?", since)
    ran = [r[0] for r in conn.execute("SELECT run_id FROM dispatch WHERE executing_at >= ?", (since,))]
    per = lambda usd, n: round(usd / n, 2) if n else None
    weeks: dict = {}
    for day, st, usd in conn.execute("SELECT date(started_at), stage, sum(usd) FROM cost_session WHERE started_at >= ? "
                                     "AND stage <> 'other' GROUP BY 1, 2", (since,)):
        d = datetime.fromisoformat(day).date()
        w = weeks.setdefault((d - timedelta(days=d.weekday())).isoformat(), {})
        w[st] = round(w.get(st, 0) + usd, 2)
    return {"total": round(sum(stage.values()), 2), "by_stage": stage,
            "per_unit": {"verdict": per(stage.get("prune", 0), verdicts), "plan": per(stage.get("plan", 0), plans),
                         "dispatch": per(sum(per_dispatch.get(r, 0) for r in ran), len(ran))},
            "per_dispatch": per_dispatch,
            "per_week": [{"week": w, **v} for w, v in sorted(weeks.items())]}

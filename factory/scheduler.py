"""Execution scheduling: capacity, hierarchical resource claims, and launch reservations.

The database holds the truth — the Briefs slice owns the schema.sql triggers that guard capacity, shared
resources, route and pane (including a direct execute bypass). This module enforces the same rules in application
code so a direct call (execute, handoff, resume) cannot skip them, and gives proposer/handoff/execute one shared
admission check. No generic scheduler framework.

Resource keys are ``namespace:path``. ``global:*`` conflicts with everything; two non-global keys conflict when
equal or when one namespace-path is a slash ancestor/descendant of the other (``clickhouse:serving`` conflicts
with ``clickhouse:serving/ck_dev`` and ``clickhouse:serving/ck_dev/master_profiles``, but not ``serving2``).
Claims live in ``dispatch_resource`` and are written while a dispatch is a draft (frozen by a schema trigger once
it leaves draft), then held through executing/done/reconciled until the dispatch is archived.

Launch reservations (``dispatch_launch``) are the capacity + pane slot: reserved atomically (``BEGIN IMMEDIATE``)
before any external ``/new`` or intake send, then ``sent``, or ``uncertain`` if the send may have partially landed.
Capacity counts distinct run ids (an executing dispatch and its own reservation count once), bounded by
``execution_policy.max_parallel`` (default 2). A terminal dispatch frees its capacity automatically (launch_active)
but the pane reservation is retained through done/reconciled/archived until ``dispatch.release_safe_terminal``
positively proves the pane idle and its supervising home free of children/decisions; resource claims persist until
archived; a rejected draft releases its claims immediately. An uncertain launch is never auto-expired or replayed —
only the explicit human recovery (``dispatch.release_unsent``) clears it.
"""
import sqlite3

from . import db

GLOBAL = "global:*"


def max_parallel(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max_parallel FROM execution_policy WHERE id=1").fetchone()
    return int(row["max_parallel"]) if row else 2


def sync_policy(conn: sqlite3.Connection, value: int | None = None) -> int:
    """Sync the bounded config policy into the singleton ``execution_policy`` row, in a transaction. Lowering the
    cap never kills work in flight: it only tightens future admission. ``value=None`` is a read."""
    if value is None:
        return max_parallel(conn)
    value = max(1, int(value))
    with db.tx(conn):
        conn.execute("INSERT INTO execution_policy(id, max_parallel) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET max_parallel=excluded.max_parallel", (value,))
    return value


def _parts(key: str) -> tuple[str, str]:
    ns, _, path = key.partition(":")
    return ns, path


def resources_conflict(a: str, b: str) -> bool:
    """Two resource keys conflict if equal, one is in the `global` namespace (conflicts with everything), or they
    share a namespace and one path is a slash ancestor/descendant of the other."""
    if a == b:
        return True
    an, ap = _parts(a)
    bn, bp = _parts(b)
    if an == "global" or bn == "global":
        return True
    if an != bn:
        return False
    return ap == bp or ap.startswith(bp.rstrip("/") + "/") or bp.startswith(ap.rstrip("/") + "/")


def claims(conn: sqlite3.Connection, run_id: str) -> set[str]:
    return {r["resource"] for r in conn.execute(
        "SELECT resource FROM dispatch_resource WHERE run_id=?", (run_id,))}


def set_claims(conn: sqlite3.Connection, run_id: str, resources) -> None:
    """Write a draft's resource claims (replaces). The schema freezes these once the dispatch leaves draft; the
    caller must be inside the dispatch's draft transaction. ``resources`` may be empty -> default ``global:*``."""
    conn.execute("DELETE FROM dispatch_resource WHERE run_id=?", (run_id,))
    resources = sorted(set(resources) or {GLOBAL})
    conn.executemany("INSERT INTO dispatch_resource(run_id, resource) VALUES (?,?)",
                     [(run_id, r) for r in resources])


def conflicting_holder(conn: sqlite3.Connection, run_id: str, resources) -> str | None:
    """A current claim holder (executing/done/reconciled, or a nonterminal launch) whose claims conflict with any
    of `resources`. Uses the authoritative `claim_holders` view (route/resource persist until archive; archived
    dispatches no longer conflict)."""
    if not resources:
        return None
    holders = conn.execute(
        "SELECT d.run_id, r.resource FROM dispatch_resource r JOIN dispatch d USING (run_id) "
        "WHERE d.run_id IN (SELECT run_id FROM claim_holders) AND d.run_id <> ?", (run_id,)).fetchall()
    for h in holders:
        for want in resources:
            if resources_conflict(h["resource"], want):
                return f"{h['resource']} held by dispatch {h['run_id']}"
    return None


def capacity_used(conn: sqlite3.Connection) -> int:
    """Distinct running+nonterminal-launch run ids, from the authoritative `launch_active` view. A terminal dispatch
    frees capacity even if its (unsafe) launch reservation is retained for explicit recovery."""
    return conn.execute("SELECT count(DISTINCT run_id) FROM launch_active").fetchone()[0]


def launch(conn: sqlite3.Connection, run_id: str):
    return conn.execute("SELECT * FROM dispatch_launch WHERE run_id=?", (run_id,)).fetchone()


def launches(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM dispatch_launch ORDER BY claimed_at")]


def uncertain_launches(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT l.*, d.state AS dispatch_state, d.route FROM dispatch_launch l JOIN dispatch d USING (run_id) "
        "WHERE l.state='uncertain' ORDER BY l.claimed_at")]


def admit(conn: sqlite3.Connection, run_id: str, resources, pane_id: str | None) -> str | None:
    """The shared admission check: capacity, hierarchical resource conflicts, then pane exclusivity. Returns a
    blocker reason or None. Route exclusivity is a resource conflict via the ``route:`` claim."""
    used = capacity_used(conn)
    if used >= max_parallel(conn):
        return f"capacity full ({used}/{max_parallel(conn)}); wait for a running or reserved dispatch"
    if c := conflicting_holder(conn, run_id, resources):
        return f"resource conflict: {c}"
    if pane_id:
        other = conn.execute("SELECT run_id FROM dispatch_launch WHERE pane_id=? AND run_id<>?",
                             (pane_id, run_id)).fetchone()
        if other:
            return f"pane {pane_id} is reserved for dispatch {other['run_id']}"
        other = conn.execute("SELECT run_id FROM dispatch WHERE executor_pane=? AND state='executing' AND run_id<>?",
                             (pane_id, run_id)).fetchone()
        if other:
            return f"pane {pane_id} is running dispatch {other['run_id']}"
    return None


def reserve(conn: sqlite3.Connection, run_id: str, pane_id: str, owner_pid: int) -> None:
    """Insert the launch reservation. Caller must hold a BEGIN IMMEDIATE transaction."""
    conn.execute("INSERT INTO dispatch_launch(run_id, pane_id, state, owner_pid, claimed_at) "
                 "VALUES (?,?,?,?,?)", (run_id, pane_id, "reserved", owner_pid, db.now()))


def reserve_if_free(conn: sqlite3.Connection, run_id: str, pane_id: str, owner_pid: int) -> str | None:
    """Atomic admission + reservation, single-winner under concurrent SQLite connections. Reads the dispatch's
    claims inside the transaction and returns a blocker reason (None on success). Caller must not already be in a
    transaction."""
    with db.tx(conn):
        d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d is None or d["state"] != "staged":
            return f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged"
        resources = claims(conn, run_id)
        if not resources:
            return f"dispatch {run_id} has no resource claims"
        if blocker := admit(conn, run_id, resources, pane_id):
            return blocker
        reserve(conn, run_id, pane_id, owner_pid)
    return None


def mark_sent(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE dispatch_launch SET state='sent', sent_at=?, error=NULL WHERE run_id=?",
                 (db.now(), run_id))


def mark_uncertain(conn: sqlite3.Connection, run_id: str, error: str) -> None:
    conn.execute("UPDATE dispatch_launch SET state='uncertain', error=? WHERE run_id=?",
                 (error[:400], run_id))


def release_launch(conn: sqlite3.Connection, run_id: str) -> None:
    """Delete a launch row that is definitely unsent (state `reserved`); the schema launch_release_guard blocks
    `uncertain` and nonterminal `sent`. Used only by the explicit human recovery release_unsent (uncertain ->
    reserved -> delete); terminal sent/reserved launches are released by release_safe_terminal after positive proof."""
    conn.execute("DELETE FROM dispatch_launch WHERE run_id=?", (run_id,))


def release_claims(conn: sqlite3.Connection, run_id: str) -> None:
    """Release resource claims (archive/reject). History (dispatch_resource is not append-only here) is dropped
    intentionally: the claim is a live lock, not a record."""
    conn.execute("DELETE FROM dispatch_resource WHERE run_id=?", (run_id,))


def status(conn: sqlite3.Connection) -> dict:
    """Read-only execution summary for the Run tab / dashboard: capacity, running, launches, held resources."""
    holders = conn.execute(
        "SELECT d.run_id, d.state, r.resource, d.brief_id FROM dispatch_resource r JOIN dispatch d USING (run_id) "
        "WHERE d.run_id IN (SELECT run_id FROM claim_holders) ORDER BY r.resource").fetchall()
    return {
        "max_parallel": max_parallel(conn),
        "capacity_used": capacity_used(conn),
        "running": [dict(r) for r in conn.execute(
            "SELECT run_id, brief_id, route, executor_pane FROM dispatch WHERE state='executing' "
            "ORDER BY executing_at")],
        "launches": launches(conn),
        "holders": [dict(r) for r in holders],
    }

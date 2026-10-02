"""One real-agent draft per tick, with human review and no execution authority."""
from __future__ import annotations

import fcntl
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db, prune, strategy, workgroups
from .dispatch import StageError

MAX_REVIEWS = 3
ACTOR = "agent:brief-proposer"


def _unchanged(conn, source, captured_at):
    # Reconcile's own writes are not new work. An intervening human edit still is.
    return not conn.execute(
        "SELECT 1 FROM linear_snapshot s WHERE issue_id=? AND updated_at>? AND updated_at<=? "
        "AND NOT EXISTS (SELECT 1 FROM linear_own_write w WHERE w.issue_id=s.issue_id "
        "AND w.updated_at=s.updated_at) LIMIT 1",
        (source["issue_id"], captured_at, source["snapshot_updated_at"])).fetchone()


def _covered(conn, sources):
    active = set(strategy._current_published(conn))
    drafts = {b["id"] for b in strategy.brief_reviews(conn)}
    covered = set()
    for row in conn.execute(
            "SELECT b.id,b.sources_json,d.state dispatch_state,x.brief_id dismissed "
            "FROM work_brief b LEFT JOIN dispatch d ON d.brief_id=b.id "
            "LEFT JOIN brief_dismissal x ON x.brief_id=b.id"):
        for old in json.loads(row["sources_json"]):
            current = sources.get(old["identifier"])
            if current is None:
                continue
            if (row["id"] in drafts or row["id"] in active and row["dispatch_state"] != "archived"
                    or row["dismissed"] is not None and _unchanged(conn, current, old["snapshot_updated_at"])
                    or row["dispatch_state"] == "archived" and _unchanged(conn, current, old["snapshot_updated_at"])):
                covered.add(old["identifier"])
    # Legacy dispatches have no brief. They still consumed their pinned source versions.
    for row in conn.execute("SELECT t.identifier,t.snapshot_updated_at FROM dispatch_ticket t "
                            "JOIN dispatch d USING(run_id) WHERE d.state='archived'"):
        current = sources.get(row["identifier"])
        if current and _unchanged(conn, current, row["snapshot_updated_at"]):
            covered.add(row["identifier"])
    return covered


def _order(ticket):
    return (ticket["priority"] or 5, ticket["due_date"] or "9999-12-31", ticket["identifier"])


def _candidates(cfg, conn):
    tickets, sources = {}, {}
    for ticket in strategy._ticket_list(cfg, conn):
        ident = ticket["identifier"]
        source = strategy._sources_for(cfg, conn, [ident])[0]
        if (strategy._source_safety(cfg, conn, source) or source["verdict_kind"] not in (None, "valid")
                or not source["relationships"]["complete"]):
            continue
        snapshot = prune.latest(conn, ident)
        context, _ = prune.map_context(cfg, snapshot)
        if (source["repo"], source["context"]) != (context.repo, context.name):
            continue  # an old verdict's mapping is not current routing authority
        tickets[ident], sources[ident] = ticket, source
    for ident in _covered(conn, sources):
        tickets.pop(ident)
        sources.pop(ident)
    boundaries = {}
    for ident, source in sources.items():
        boundaries.setdefault((source["repo"], source["context"], source["route"]), []).append(tickets[ident])
    candidates, skipped = [], []
    for members in boundaries.values():
        groups = workgroups.build(members, {t["identifier"]: sources[t["identifier"]]["relationships"]
                                           for t in members})
        for group in groups:
            if group["cycles"]:
                skipped.append({"sources": group["members"], "reason": "recorded relationship cycle"})
                continue
            cohorts = ([group["members"]] if group["kind"] in ("parent", "dependency", "related")
                       else [[ident] for ident in group["members"]])
            for cohort in cohorts:
                cohort.sort(key=lambda ident: _order(tickets[ident]))
                captured = [sources[ident] for ident in cohort]
                try:
                    strategy._idents(cohort)
                    strategy._derived_resources(captured)
                    strategy._groom_prompt(captured)
                    strategy._ensure_acyclic(conn, set(cohort), strategy._dependency_candidates(captured), None)
                except StageError as exc:
                    skipped.append({"sources": cohort, "reason": str(exc)})
                    continue
                candidates.append(captured)
    candidates.sort(key=lambda cohort: _order(tickets[cohort[0]["identifier"]]))
    return candidates, skipped


def _capture(sources):
    # Relationship polling time alone is not source drift; all model-visible facts still must match.
    return [{**source, "relationships": {k: v for k, v in source["relationships"].items()
                                         if k != "observed_at"}} for source in sources]


def propose(cfg, conn) -> dict:
    """Single-host single-flight; never hold SQLite's writer lock while the model runs."""
    path = cfg.db.resolve()
    with path.with_name(path.name + ".brief-propose.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        if len(strategy.brief_reviews(conn)) >= MAX_REVIEWS:
            return {"status": "capped"}
        candidates, skipped = _candidates(cfg, conn)
        if not candidates:
            return {"status": "blocked" if skipped else "idle", "skipped": skipped}
        sources = candidates[0]
        norm = strategy._groom_body(sources)
        with db.tx(conn):
            if len(strategy.brief_reviews(conn)) >= MAX_REVIEWS:
                return {"status": "capped", "reason": "review inbox filled during grooming"}
            current, current_skipped = _candidates(cfg, conn)
            if not any(_capture(candidate) == _capture(sources) for candidate in current):
                return {"status": "stale", "reason": "sources, coverage or eligibility changed during grooming",
                        "skipped": current_skipped}
            strategy._ensure_acyclic(conn, {s["identifier"] for s in sources}, norm["dependencies"], None)
            brief = strategy._insert_draft(conn, sources, norm, ACTOR)
        return {"status": "created", "brief": {"id": brief["id"], "title": brief["body"]["title"],
                                                "sources": [s["identifier"] for s in sources]}, "skipped": skipped}


def announcement(cfg, brief) -> str:
    parts = urlsplit(cfg.raw.get("notify", {}).get("url", ""))
    query = dict(parse_qsl(parts.query))
    query.update(stage="strategy", brief=str(brief["id"]))
    link = urlunsplit(parts._replace(query=urlencode(query)))
    return f"Draft brief #{brief['id']}: {brief['title']} — human review required (not approved).\n{link}"

"""Dispatch lifecycle: candidates, stage (draft -> staged). Invariants live in schema.sql triggers."""
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from . import db, decide, jev, learn, prune, repos, scheduler
from .config import Config, secret

HERMES = str(Path.home() / ".local/bin/hermes")

# The strategy module is owned by the Briefs slice; imported lazily so dispatch stays importable in either order
# (strategy.py raises StageError from here, and imports dispatch for it).
def _brief(conn, brief_id: int):
    from . import strategy
    return strategy.get(conn, brief_id)


def _ready(cfg: Config, conn):
    from . import strategy
    return strategy.ready(cfg, conn)


def _ticket_context(conn, brief_id: int, identifier: str):
    from . import strategy
    return strategy.ticket_context(conn, brief_id, identifier)


def _render_brief(conn, brief_id: int) -> str | None:
    from . import strategy
    return strategy.render(conn, brief_id)


class StageError(Exception):
    pass


def _foreign_holds(cfg: Config) -> dict[str, str]:
    """Where other fleets record their work (read-only): backlog files, plus live herdr workspace labels
    (nix-fleet crews name their worktree after the ticket, e.g. `fin3733-...` or `fin-2068-...`)."""
    pats = cfg.raw.get("stage", {}).get("foreign_backlogs", [])
    holds = {p: Path(p).read_text() for pat in pats for p in glob.glob(os.path.expanduser(pat))}
    own = cfg.raw.get("executor", {}).get("workspace", "factory")
    try:
        r = subprocess.run(["herdr", "workspace", "list"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StageError(f"herdr workspace list: {e}") from None
    if r.returncode == 0:
        labels = [w["label"] for w in json.loads(r.stdout)["result"]["workspaces"]
                  if w["label"] != own and not w["label"].startswith("2ndmate-fx-") and "fx-" not in w["label"]]
        holds["herdr workspaces"] = "\n".join(labels)
    return holds


def _names(text: str, ident: str) -> bool:
    team, num = ident.split("-")
    return re.search(rf"(?i)(?<![a-z0-9]){team}-?{num}(?!\d)", text) is not None


def candidates(cfg: Config, conn) -> dict:
    """Owned, mapped, fresh `valid` tickets that nobody else holds. Everything else, with the reason."""
    lead = cfg.linear["lead"]
    live = {r[0] for r in conn.execute(
        "SELECT t.issue_id FROM dispatch_ticket t JOIN dispatch d USING (run_id) WHERE d.state <> 'archived'")}
    holds = _foreign_holds(cfg)
    ok, skipped = [], []
    for s in prune.owned_in_scope(cfg, conn):
        raw = json.loads(s["raw_json"])
        ident = raw["identifier"]
        ctx, _ = prune.map_context(cfg, s)
        v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                         (s["issue_id"],)).fetchone()
        assignee = (raw["assignee"] or {}).get("email")
        held = next((p for p, text in holds.items() if _names(text, ident)), None)
        # A verdict is dispatched at most once: a done card already landed, a blocked one needs the ticket to
        # change (new verdict) first, unless the user chose "retry" on the block. Without this, archived tickets in
        # Ready for QA would be restaged forever.
        prior = v and conn.execute(
            "SELECT t.run_id, CASE WHEN d.rejected_reason IS NOT NULL THEN 'rejected in review' ELSE t.card_status END "
            "card_status FROM dispatch_ticket t JOIN dispatch d USING (run_id) WHERE t.verdict_id=? "
            "AND NOT EXISTS (SELECT 1 FROM decision x WHERE x.run_id=t.run_id AND x.issue_id=t.issue_id "
            "AND x.kind='blocked' AND x.chosen='retry') ORDER BY d.created_at DESC LIMIT 1", (v["id"],)).fetchone()
        owner = ctx and ctx.owner(prune.issue_fields(s)[0])
        why = ("unmapped" if ctx is None
               else f"no factory-fleet owner for context {ctx.name} (route)" if owner is None
               else "no verdict" if v is None
               else f"verdict {v['kind']}" if v["kind"] != "valid"
               else f"verdict stale ({r})" if (r := prune.staleness(cfg, conn, s, ctx))
               else "in a live dispatch" if s["issue_id"] in live
               else f"already dispatched on this verdict ({prior['run_id']}: {prior['card_status']})" if prior
               else f"assigned to {assignee}" if assignee not in (None, lead)
               else f"held by another fleet ({held})" if held
               else None)
        if why:
            skipped.append({"identifier": ident, "reason": why})
            continue
        ok.append({"identifier": ident, "title": raw["title"], "url": raw["url"], "repo": ctx.repo,
                   "context": ctx.name, "route": owner, "priority": raw["priority"], "state": raw["state"]["name"],
                   "reason": v["reason"]})
    ok.sort(key=lambda c: (c["priority"] or 5, c["repo"]))
    return {"max_tickets": max_tickets(cfg), "candidates": ok, "skipped": skipped,
            "suggested": _cohort(cfg, conn, ok) if ok else []}  # what the factory would group next


def max_tickets(cfg: Config) -> int:
    return cfg.raw.get("stage", {}).get("max_tickets", 3)


def _render(run_id: str, when: str, actor: str, trunks: dict, tickets: list, tree: list, rejected: str | None,
            answers: list, route: str | None = None, pitfalls: list = ()) -> str:
    notes = lambda node: [f"- {n['author']} ({n['at'][:16]}Z): {n['body'].strip()}" for n in node["notes"]]
    paths = lambda fs: ", ".join(f"`{f['path']}`" + (" (new)" if f["new"] else "") for f in fs)
    root = tree[0]
    lines = [f"# Dispatch {run_id}", ""]
    lines += ([f"**REJECTED in review** by {actor} at {when}: {rejected}", ""] if rejected else
              [f"Approved {when} by {actor}. This file is immutable (`chflags uchg`); factory.db holds its sha256.", ""])
    lines += ["## Repos", ""] + [f"- {repo} @ trunk `{sha}`" for repo, sha in sorted(trunks.items())]
    lines += ["", "## Runs in", "", f"- `{route}` (factory-fleet), handed this dispatch directly by the factory"
              if route else "- factory-primary routes each card (no single factory-fleet owner)"]
    lines += ["", "## Rules", "",
              "- Work only the tickets below. One PR per ticket, against the repo's trunk.",
              "- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and "
              "override the plan and the ticket body; if one cannot be followed, `factory card block` with why.",
              "- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. "
              "Name the step id (e.g. FIN-1/2) in comments.",
              "- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep "
              "working on other tickets; the answer arrives in this session.",
              "- Never write to Linear. Reconcile does that after the dispatch closes.",
              "- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, "
              "`factory card block` with the evidence instead of forcing a change.", ""]
    if pitfalls:
        lines += ["## Known pitfalls", "", "Learned from earlier blocks in these repos. Cite `L<id>` in a card comment "
                  "when one saved you work.", "", *pitfalls, ""]
    if root["title"] != run_id or root["detail"]:
        lines += [f"## Theme: {root['title']}", "", root["detail"], ""]
    if root["result"]:
        lines += [f"Result: {root['result']}", ""]
    if root["notes"]:
        lines += ["## Operator notes (whole dispatch)", "", *notes(root), ""]
    if answers:
        chosen = {a["key"]: a["chosen"] for a in answers if a["key"]}
        lines += ["## Answered questions (binding)", ""]
        for a in answers:
            o, dep = next(o for o in a["options"] if o["id"] == a["chosen"]), a["depends_on"]
            if dep and chosen.get(dep["question"]) != dep["option"]:  # only mattered under another answer
                lines.append(f"- On {a['node_id']}: {a['question']} Does not apply ({dep['question']} is not "
                             f"{dep['option']}).")
                continue
            lines.append(f"- On {a['node_id']}: {a['question']} **{o['label']}**: {o['leads_to']}"
                         + (f" — result: {o['result']}" if o["result"] else "")
                         + (f" Note: {a['chosen_note']}" if a["chosen_note"] else "") + f" ({a['chosen_by']})")
            for c in o["changes"]:
                if "add" in c:
                    s = c["add"]
                    lines.append(f"  - add {s['id']}: {s['title']}"
                                 + (f" (after {', '.join(s['depends_on'])})" if s["depends_on"] else "")
                                 + (f"; files {paths(s['files'])}" if s["files"] else ""))
                else:
                    lines.append(f"  - {c['step']}: " + (f"now \"{c['becomes']}\"" if c["becomes"] else "dropped"))
        lines.append("")
    by_parent = {}
    for node in tree[1:]:
        if node["kind"] == "step":
            by_parent.setdefault(node["parent"], []).append(node)
    order = {n["id"]: i for i, n in enumerate(tree)}
    tickets = sorted(tickets, key=lambda t: order[t["identifier"]])

    def plan(parent: str, depth: int) -> list:
        out = []
        for s in by_parent.get(parent, []):
            after = f" (after {', '.join(s['depends_on'])})" if s["depends_on"] else ""
            pad = "  " * depth
            out.append(f"{pad}- **{s['id']}** {s['title']}{after}")
            if s["detail"].strip():
                out.append(f"{pad}  {s['detail'].strip()}")
            if s["files"]:
                out.append(f"{pad}  Files: {paths(s['files'])}")
            out += [f"{pad}  - note {line[2:]}" for line in notes(s)]
            out += plan(s["id"], depth + 1)
        return out

    for t in tickets:
        node = next(n for n in tree if n["id"] == t["identifier"])
        ev = "\n".join(f"  - `{json.dumps(e, sort_keys=True)}`" for e in t["evidence"])
        lines += [f"## {t['identifier']}: {t['title']}", ""]
        if node["parent"] != "root":
            lines.append(f"- Part of: {node['parent']}")
        if node["depends_on"]:
            lines.append(f"- After: {', '.join(node['depends_on'])}")
        if node["detail"]:
            lines.append(f"- In this dispatch: {node['detail']}")
        if node["result"]:
            lines.append(f"- Result: {node['result']}")
        lines += [f"- Linear: {t['url']} ({t['state']}, assignee {t['assignee'] or 'none'})",
                  f"- Repo: {t['repo']} (context {t['context']}), trunk `{trunks[t['repo']]}`",
                  f"- Verdict: valid — {t['reason']}",
                  f"- Evidence:\n{ev}", ""]
        if node["notes"]:
            lines += [f"### Operator notes on {t['identifier']}", "", *notes(node), ""]
        lines += ["### Plan", "", *(plan(t["identifier"], 0) or ["_(no plan written)_"]), "",
                  "### Ticket body (claim, not truth)", "", t["description"].strip() or "_(empty)_", ""]
    return "\n".join(lines)


def _draft(conn, run_id: str):
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "draft":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not a draft in review")
    return d


def _step_key(step_id: str) -> tuple:
    return tuple(int(p) for p in step_id.split("/", 1)[1].split("."))


STEP_ID = re.compile(r"^([A-Z]+-\d+)/(\d+(?:\.\d+)*)$")


def tree(conn, run_id: str) -> list:
    """Pre-order: root, then each top-level ticket, its steps depth-first, then the tickets nested under it.
    Step parents derive from the id; a ticket's parent is set by the planner (`under`), default root."""
    notes = {}
    for n in conn.execute("SELECT id, node_id, author, body, at FROM dispatch_note WHERE run_id=? ORDER BY id",
                          (run_id,)):
        notes.setdefault(n["node_id"], []).append({k: n[k] for k in ("id", "author", "body", "at")})
    rows = {r["step_id"]: r for r in conn.execute("SELECT * FROM dispatch_step WHERE run_id=?", (run_id,))}
    steps = sorted((r for k, r in rows.items() if "/" in k), key=lambda r: _step_key(r["step_id"]))
    node = lambda i, parent, kind, title, r: {
        "id": i, "parent": parent, "kind": kind, "title": title, "detail": r["detail"] if r else "",
        "depends_on": json.loads(r["depends_on_json"]) if r else [], "notes": notes.get(i, []),
        "result": r["result"] if r else None, "files": json.loads(r["files_json"]) if r and r["files_json"] else []}
    root = rows.get("root")
    out = [node("root", None, "dispatch", root["title"] if root else run_id, root)]
    tickets = conn.execute("SELECT t.identifier, json_extract(s.raw_json, '$.title') title FROM dispatch_ticket t "
                           "JOIN linear_snapshot s ON s.issue_id=t.issue_id AND s.updated_at=t.snapshot_updated_at "
                           "WHERE t.run_id=? ORDER BY t.rowid", (run_id,)).fetchall()
    parent_of = {t["identifier"]: (rows[t["identifier"]]["parent"] if t["identifier"] in rows else None) or "root"
                 for t in tickets}

    def walk(parent: str):
        for t in tickets:
            if parent_of[t["identifier"]] != parent:
                continue
            out.append(node(t["identifier"], parent, "ticket", t["title"], rows.get(t["identifier"])))
            for r in steps:
                if r["step_id"].split("/")[0] == t["identifier"]:
                    sp = r["step_id"].rsplit(".", 1)[0] if "." in r["step_id"] else t["identifier"]
                    out.append(node(r["step_id"], sp, "step", r["title"], r))
            walk(t["identifier"])
    walk("root")
    return out


def review(d, due: str | None = None) -> str | None:
    """A draft's review state; `due` is when its review decision takes ★ without the user."""
    if d["state"] != "draft":
        return None
    if d["planned_at"] is None:
        return "planning"
    if d["held_reason"]:
        return "held"
    return "in-review" if due else "waiting-approval"


# A dispatch's lifecycle stage from its own record (SQL over dispatch columns): draft until the plan gate offered it to
# the planner, a replan asked, or a plan for it was refused (a refusal is planning, never a made-up offer), plan until a
# plan is written, review until it leaves draft (held too), run while staged or executing, reconcile while done or
# reconciled, archive once archived (rejected drafts too).
PHASE = ("CASE WHEN state = 'draft' AND planned_at IS NOT NULL THEN 'review' "
         "WHEN state = 'draft' AND (planning_requested_at IS NOT NULL OR planning_error IS NOT NULL) THEN 'plan' "
         "WHEN state = 'draft' THEN 'draft' WHEN state IN ('staged', 'executing') THEN 'run' "
         "WHEN state IN ('done', 'reconciled') THEN 'reconcile' ELSE 'archive' END")


def _current_published_heads(conn) -> set[int]:
    """The authoritative head-of-lineage ids from Strategy. Reused verbatim (lazy import preserves the
    dispatch<->strategy cycle design); no local recursive query or head convention is duplicated here."""
    from . import strategy
    return set(strategy._current_published(conn))


def _approved_unconsumed(conn) -> list:
    """Approved, current (head-of-lineage), unconsumed briefs in stable approved order. Supersession is decided by
    the authoritative Strategy heads: an intermediate DRAFT revision does not re-activate an approved ancestor, and
    any later published descendant (even past a draft) supersedes it."""
    heads = _current_published_heads(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM work_brief WHERE state='approved' AND "
        "NOT EXISTS (SELECT 1 FROM dispatch d WHERE d.brief_id = work_brief.id) "
        "ORDER BY (approved_at IS NULL), approved_at, id") if r["id"] in heads]


def _resolve_brief(conn, identifiers: list[str]) -> int:
    """The approved, unconsumed, current brief whose captured sources are exactly `identifiers` (set equality)."""
    want = set(identifiers)
    matches = [b["id"] for b in _approved_unconsumed(conn)
               if {s.get("identifier") for s in json.loads(b["sources_json"])} == want]
    if not matches:
        raise StageError("no approved brief matches exactly these tickets; publish and approve a brief first")
    if len(matches) > 1:
        raise StageError("more than one approved brief matches these tickets; pass an explicit brief id")
    return matches[0]


def _load_approved_brief(conn, brief_id: int) -> dict:
    """The shared approved-brief gate for both stage entrypoints: approved (not held/draft), not superseded by a
    published child, and not already consumed. Rejects expired/superseded/held intent atomically on first read."""
    brief = _brief(conn, brief_id)
    if brief.get("state") != "approved":
        raise StageError(f"brief #{brief_id} is {brief.get('state', 'unknown')}, not approved")
    if brief_id not in _current_published_heads(conn):  # superseded by any later published descendant
        raise StageError(f"brief #{brief_id} was superseded by a newer revision; use the current version")
    if consumed := conn.execute("SELECT run_id FROM dispatch WHERE brief_id=?", (brief_id,)).fetchone():
        raise StageError(f"brief #{brief_id} is already dispatched as {consumed['run_id']}")
    return brief


def _brief_verdict(conn, brief_id: int, issue_id: str):
    """The exact version -> verdict association for a brief source: the current valid verdict this brief version was
    verified with. Never a borrowed generic/prior source verdict; a missing or superseded association is refused."""
    bv = conn.execute("SELECT verdict_id FROM brief_verdict WHERE brief_id=? AND issue_id=?",
                      (brief_id, issue_id)).fetchone()
    if bv is None:
        raise StageError(f"brief #{brief_id} has no recorded verification for issue {issue_id}; "
                         "verify it from the brief first")
    v = conn.execute("SELECT * FROM verdict WHERE id=?", (bv["verdict_id"],)).fetchone()
    if v is None or v["superseded_at"] is not None or v["issue_id"] != issue_id:
        raise StageError(f"brief #{brief_id}'s verification for issue {issue_id} was superseded; re-verify")
    if v["kind"] != "valid":
        raise StageError(f"brief #{brief_id}'s verification for issue {issue_id} is {v['kind']}, not valid")
    return v


def _dependency_status(conn, identifier: str) -> str:
    """Dependency readiness follows the authoritative Strategy rule (lazy import preserves the dispatch<->strategy
    cycle design): ready only on a recorded completed state or a CURRENT-snapshot already-done verdict — a reopened
    ticket's stale, still-unsuperseded already-done verdict is not readiness."""
    from . import strategy
    return strategy._dependency_status(conn, identifier)


def _create_brief_draft(cfg: Config, conn, identifiers: list[str], actor: str, emergency: bool,
                        brief_id: int, brief: dict) -> dict:
    """Verify the brief's sources against the pinned capture (no Linear re-read), the exact-version verification
    association, and readiness, then draft the dispatch: brief_id, captured snapshot + associated verdict anchors,
    and the reviewed resource claims are pinned into it. Nothing is frozen or started until approved."""
    sources = {s["identifier"]: s for s in brief.get("sources", [])}
    if set(sources) != set(identifiers):
        raise StageError(f"brief #{brief_id} covers {', '.join(sorted(sources))}, not {', '.join(identifiers)}")
    for dep in brief.get("body", {}).get("dependencies", []):  # readiness before anything is written
        if (st := _dependency_status(conn, dep)) != "ready":
            raise StageError(f"dependency {dep} is {st}; not staging")
    review = {k: t.get("review_state") for k, t in cfg.linear.get("team", {}).items()}
    rows, owners = [], set()
    for ident in identifiers:
        src = sources[ident]
        s = prune.latest(conn, ident)
        if s["updated_at"] != src.get("snapshot_updated_at"):
            raise StageError(f"{ident} changed since the brief was captured; amend the brief before staging")
        if not prune.owned(cfg, conn, s):
            raise StageError(f"{ident}: not the factory's concern (its Domain project is not led by {cfg.linear['lead']})")
        raw = json.loads(s["raw_json"])
        assignee = (raw.get("assignee") or {}).get("email")
        if assignee not in (None, cfg.linear["lead"]):
            raise StageError(f"{ident}: assigned to {assignee}")
        if s["state_type"] in ("completed", "canceled"):
            raise StageError(f"{ident}: already {s['state_type']}")
        if raw["state"]["name"] == review.get(raw.get("team", {}).get("key")):
            raise StageError(f"{ident}: waits on a human ({raw['state']['name']})")
        ctx, why = prune.map_context(cfg, s)
        if ctx is None:
            raise StageError(f"{ident}: {why}")
        owner = ctx.owner(prune.issue_fields(s)[0])
        if owner is None:
            raise StageError(f"{ident}: no factory-fleet owner for context {ctx.name}")
        owners.add(owner)
        v = _brief_verdict(conn, brief_id, s["issue_id"])  # exact association, never a borrowed source verdict
        if (r := prune.verdict_staleness(cfg, conn, s, ctx, v)):
            raise StageError(f"{ident}: verification stale ({r}); re-verify from the brief before staging")
        if conn.execute("SELECT 1 FROM dispatch_ticket t JOIN dispatch d USING (run_id) WHERE t.issue_id=? "
                        "AND d.state <> 'archived'", (s["issue_id"],)).fetchone():
            raise StageError(f"{ident}: already in a live dispatch")
        rows.append((s["issue_id"], ident, s["updated_at"], v["id"], ctx.repo))
    trunks = {r[4]: conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (r[4],)).fetchone()["sha"] for r in rows}
    resources = set(brief.get("body", {}).get("resources") or [])
    resources |= {f"repo:{r[4]}" for r in rows}  # mandatory repo claims, derived, cannot be removed
    if not any(k.startswith("route:") for k in resources):  # canonical fallback home (never an invented fleet)
        resources.add(f"route:{owners.pop() if len(owners) == 1 else 'home'}")
    run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{identifiers[0].lower()}"
    with db.tx(conn):
        # Re-validate approved/current/unconsumed + readiness (source-change, dependencies) under the same
        # transaction that writes the dispatch: a concurrent amend/supersede/consume is never raced.
        row = conn.execute("SELECT state, body_json, sources_json FROM work_brief WHERE id=?", (brief_id,)).fetchone()
        if row is None or row["state"] != "approved":
            raise StageError(f"brief #{brief_id} changed while staging; nothing was written")
        if brief_id not in _current_published_heads(conn):
            raise StageError(f"brief #{brief_id} was superseded while staging; nothing was written")
        if conn.execute("SELECT 1 FROM dispatch WHERE brief_id=?", (brief_id,)).fetchone():
            raise StageError(f"brief #{brief_id} was consumed while staging; nothing was written")
        for src in json.loads(row["sources_json"]):
            cur = conn.execute("SELECT updated_at FROM linear_latest WHERE issue_id=?",
                               (src["issue_id"],)).fetchone()
            if cur is not None and cur["updated_at"] != src["snapshot_updated_at"]:
                raise StageError(f"{src['identifier']} changed while staging; nothing was written")
        for dep in json.loads(row["body_json"]).get("dependencies", []):
            if _dependency_status(conn, dep) != "ready":
                raise StageError(f"dependency {dep} is not ready; nothing was written")
        conn.execute("INSERT INTO dispatch(run_id, state, repos_json, last_actor, created_at, drafted_by, emergency, "
                     "brief_id) VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, "draft", json.dumps([{"repo": r, "trunk_sha": s} for r, s in sorted(trunks.items())]),
                      actor, db.now(), actor, int(emergency), brief_id))
        conn.executemany("INSERT INTO dispatch_ticket(run_id, issue_id, identifier, snapshot_updated_at, verdict_id) "
                         "VALUES (?,?,?,?,?)", [(run_id, *r[:4]) for r in rows])
        scheduler.set_claims(conn, run_id, resources)
        for issue_id, ident, _, verdict_id, _ in rows:  # a retry after a block carries the user's guidance along
            g = conn.execute("SELECT x.run_id, x.chosen_note, x.chosen_by, json_extract(x.detail_json, '$.reason') "
                             "reason FROM decision x JOIN dispatch_ticket t ON t.run_id=x.run_id AND "
                             "t.issue_id=x.issue_id WHERE x.issue_id=? AND x.kind='blocked' AND x.chosen='retry' AND "
                             "t.verdict_id=? ORDER BY x.id DESC LIMIT 1", (issue_id, verdict_id)).fetchone()
            if g:
                conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                             (run_id, ident, g["chosen_by"], f"Retry of {g['run_id']}, which blocked: {g['reason']}\n"
                                                             f"Guidance: {g['chosen_note']}", db.now()))
    return {"run_id": run_id, "state": "draft", "tickets": identifiers, "brief_id": brief_id, "emergency": emergency}


def stage(cfg: Config, conn, identifiers: list[str], actor: str, emergency: bool = False,
          brief_id: int | None = None) -> dict:
    """Draft a dispatch from an approved brief. Identifiers must resolve an approved exact matching brief (or pass
    an explicit `brief_id`); the brief's captured intent, not raw Linear prose, becomes the dispatch. Both entry
    points validate approved/current/unconsumed + readiness through `_load_approved_brief` and
    `_create_brief_draft`. Nothing is frozen or started until approved."""
    if not identifiers:
        raise StageError("name at least one ticket")
    if len(identifiers) > max_tickets(cfg):
        raise StageError(f"{len(identifiers)} tickets > stage.max_tickets {max_tickets(cfg)}; keep dispatches small")
    if len(set(identifiers)) != len(identifiers):
        raise StageError("duplicate ticket")
    if brief_id is None:
        brief_id = _resolve_brief(conn, identifiers)
    brief = _load_approved_brief(conn, brief_id)
    return _create_brief_draft(cfg, conn, identifiers, actor, emergency, brief_id, brief)


def stage_brief(cfg: Config, conn, brief_id: int, actor: str) -> dict:
    """Stage an approved brief by id (CLI/proposer entrypoint). Verifies the brief's captured sources against the
    pinned snapshot and the exact-version verification association, then drafts a brief-backed dispatch."""
    brief = _load_approved_brief(conn, brief_id)
    identifiers = [s["identifier"] for s in brief.get("sources", [])]
    if not identifiers:
        raise StageError(f"brief #{brief_id} has no sources")
    if len(identifiers) > max_tickets(cfg):
        raise StageError(f"brief #{brief_id} has {len(identifiers)} sources > stage.max_tickets {max_tickets(cfg)}")
    if len(set(identifiers)) != len(identifiers):
        raise StageError(f"brief #{brief_id} names a source twice")
    return _create_brief_draft(cfg, conn, identifiers, actor, False, brief_id, brief)


PLAN_QUESTIONS_MAX = 2  # each is a decision someone has to read; decide the rest in the plan
QUESTION_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _line(where: str, v, n: int = 300) -> str:
    """A required short text field."""
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= n:
        raise StageError(f"{where}: 1-{n} chars of text")
    return v.strip()


def _rel(where: str, path) -> str:
    if not isinstance(path, str) or not path.strip() or path.startswith("/") or ".." in path.split("/"):
        raise StageError(f"{where}: {path!r} is not a repo-relative path")
    return path


def _at_trunk(cfg: Config, where: str, path, at: list) -> str:
    """A repo-relative path that exists at the dispatch trunk of one of `at` [(repo, sha)] (as verdict evidence)."""
    _rel(where, path)
    if not any(repos.path_exists(cfg.mirror_path(r), sha, path) for r, sha in at):
        raise StageError(f"{where}: {path!r} does not exist at trunk ({', '.join(f'{r}@{s[:12]}' for r, s in at)})")
    return path


def _files(cfg: Config, where: str, files, at: list) -> list:
    """Files a step touches: "path" (exists at trunk) or {"path", "new": true} for one it creates."""
    if not isinstance(files, list):
        raise StageError(f"{where}: files must be a list")
    return [{"path": _rel(where, f["path"]), "new": True}
            if isinstance(f, dict) and f.get("new") is True and set(f) == {"path", "new"}
            else {"path": _at_trunk(cfg, where, f if isinstance(f, str) else None, at), "new": False} for f in files]


def _evidence(cfg: Config, where: str, ev, at: list) -> list:
    """Question evidence: "path:line" or {"path", "line", "note"?}; the path exists at trunk."""
    if not isinstance(ev, list):
        raise StageError(f"{where}: evidence must be a list")
    out = []
    for e in ev:
        if isinstance(e, str) and ":" in e:
            p, _, line = e.rpartition(":")
            e = {"path": p, "line": int(line) if line.isdigit() else None}
        if not isinstance(e, dict) or not set(e) <= {"path", "line", "note"} or not isinstance(e.get("line"), int) \
                or e["line"] < 1 or not isinstance(e.get("note", ""), str):
            raise StageError(f"{where}: evidence is \"path:line\" or {{path, line, note}}")
        out.append({"path": _at_trunk(cfg, where, e.get("path"), at), "line": e["line"],
                    **({"note": e["note"].strip()} if e.get("note", "").strip() else {})})
    return out


def _changes(cfg: Config, where: str, changes, steps: set, ids: set, kept: list, at) -> list:
    """What an option does to the plan: {"step", "becomes": new title | null (removed)} or {"add": step}."""
    if not isinstance(changes, list):
        raise StageError(f"{where}: changes must be a list (empty: the plan as written)")
    out, added = [], set()
    for c in changes:
        if isinstance(c, dict) and set(c) == {"step", "becomes"}:
            if c["step"] not in steps:
                raise StageError(f"{where}: changes names unknown step {c['step']!r}")
            out.append({"step": c["step"],
                        "becomes": None if c["becomes"] is None else _line(f"{where} {c['step']} becomes", c["becomes"], 200)})
        elif isinstance(c, dict) and set(c) == {"add"} and isinstance(c["add"], dict):
            a, i = c["add"], str(c["add"].get("id", ""))
            m = STEP_ID.match(i)
            if not m or m[1] not in kept or i in steps or i in added:
                raise StageError(f"{where}: add needs a new step id <TICKET>/<n> of a kept ticket, got {i!r}")
            deps = a.get("depends_on") or []
            if not isinstance(deps, list) or any(d not in ids | added for d in deps):
                raise StageError(f"{where}: add {i}: depends_on must list node ids of this dispatch")
            if not set(a) <= {"id", "title", "detail", "depends_on", "files"} or len(str(a.get("detail", ""))) > 2000:
                raise StageError(f"{where}: add {i}: {{id, title, detail, depends_on, files}}, detail <= 2000")
            added.add(i)
            out.append({"add": {"id": i, "title": _line(f"{where} add {i} title", a.get("title"), 200),
                                "detail": str(a.get("detail", "")).strip(), "depends_on": deps,
                                "files": _files(cfg, f"{where} add {i}", a.get("files") or [], at(i))}})
        else:
            raise StageError(f"{where}: each change is {{step, becomes}} or {{add: {{id, title, ...}}}}")
    return out


OPTION_KEYS = {"id", "label", "leads_to", "changes", "result"}


def _plan_questions(cfg: Config, qs: list, node_ids: set, steps: set, kept: list, at) -> list:
    """Planner questions: each a real choice with consequences and a recommendation, like every decision, plus what
    the configurator shows: what is true today (now, evidence) and per option the plan changes, result, cost, risk."""
    if len(qs) > PLAN_QUESTIONS_MAX:
        raise StageError(f"{len(qs)} questions; at most {PLAN_QUESTIONS_MAX}. Decide the rest in the plan")
    out = []
    for q in qs:
        on, text, opts = str(q.get("on") or "root"), str(q.get("question", "")).strip(), q.get("options")
        if on not in node_ids:
            raise StageError(f"question {text[:40]!r}: on must be root, a kept ticket or a step id")
        if not 1 <= len(text) <= 300 or not str(q.get("why", "")).strip():
            raise StageError("a question needs question (1-300 chars) and why (your reason for the recommendation)")
        where = f"question {text[:40]!r}"
        key = q.get("key")
        if not isinstance(key, str) or not QUESTION_KEY.match(key) or key in {x["key"] for x in out}:
            raise StageError(f"{where}: key must be a unique slug (a-z, 0-9, -)")
        if (not isinstance(opts, list) or not 2 <= len(opts) <= 5
                or not all(isinstance(o, dict) and OPTION_KEYS <= set(o) <= OPTION_KEYS | {"cost", "risk"}
                           and all(str(o[k]).strip() for k in ("id", "label", "leads_to")) for o in opts)):
            raise StageError(f"{where}: 2-5 options, each {{id, label, leads_to, changes, result}} (+ cost, risk)")
        if q.get("recommend") not in {o["id"] for o in opts}:
            raise StageError(f"{where}: recommend must be one of its option ids")
        extra = set(q) - {"question", "on", "key", "now", "evidence", "depends_on", "options", "recommend", "why"}
        if extra:
            raise StageError(f"{where}: unknown fields {sorted(extra)}")
        at_q = at(on)
        out.append({"on": on, "question": text, "why": str(q["why"]).strip(), "recommend": q["recommend"], "key": key,
                    "now": _line(f"{where} now", q.get("now"), 400),
                    "evidence": _evidence(cfg, where, q.get("evidence") or [], at_q),
                    "depends_on": q.get("depends_on"),
                    "options": [{**decide.option(str(o["id"]), str(o["label"]), str(o["leads_to"])),
                                 "changes": _changes(cfg, f"{where} option {o['id']}", o["changes"], steps,
                                                     steps | set(kept), kept, at),
                                 "result": _line(f"{where} option {o['id']} result", o["result"]),
                                 **{k: _line(f"{where} option {o['id']} {k}", o[k], 120) for k in ("cost", "risk")
                                    if k in o}} for o in opts]})
    options = {x["key"]: {o["id"] for o in x["options"]} for x in out}
    for x in out:  # "only matters under that answer" of another question in this plan
        dep = x["depends_on"]
        if dep is not None and not (isinstance(dep, dict) and set(dep) == {"question", "option"}
                                    and dep["question"] != x["key"] and dep["option"] in options.get(dep["question"], ())):
            raise StageError(f"question {x['key']}: depends_on is {{question: <another question's key>, "
                             "option: <one of its option ids>}")
    return out


def plan(cfg: Config, conn, run_id: str, nodes: list) -> dict:
    """Planner agent: write the draft's plan tree once. Entries (JSON list):
      {"id": "root", "title": theme, "detail": why these tickets belong in one dispatch, "result": what lands,
       "recommend": "approve"|"hold"|"reject", "why": your review recommendation}             result required
      {"id": "FIN-1", "detail": its role, "under": "FIN-2", "depends_on": ["FIN-3"], "result"} per kept ticket
      {"id": "FIN-1", "exclude": "why it does not fit this dispatch"}                         drops the ticket
      {"id": "FIN-1/2" or "FIN-1/2.1", "title", "detail", "depends_on": [node ids],
       "files": ["path" | {"path", "new": true}], "result"?}                                  steps, 1-12 per ticket
      {"question": text, "key": slug, "on": node id, "now": what is true today, "evidence": ["path:line" |
       {"path", "line", "note"}], "depends_on"?: {"question": key, "option": id}, "options": [{"id", "label",
       "leads_to", "changes": [{"step", "becomes": title|null} | {"add": step}], "result", "cost"?, "risk"?}],
       "recommend": option id, "why": reason}                                              a choice for the reviewer
    `under` nests a ticket under another (a tree); depends_on is ordering (a DAG). Both must be acyclic.
    Paths (files, evidence) must exist at the dispatch trunk of the node's repo, except files marked new.
    Questions the reviewer leaves open take their recommendation at approval. When Jev is enabled, each question
    and the review get judgment guidance (persisted in the jev_advice table, never in detail_json), and a question
    Jev is sure (>= 0.85) asks for pure missing investigation is refused here — check the code and decide it in
    the plan instead."""
    d = _draft(conn, run_id)
    if d["planned_at"]:
        raise StageError(f"{run_id} already has a plan")
    repo_of = dict(conn.execute("SELECT t.identifier, v.repo FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                                "WHERE t.run_id=? ORDER BY t.rowid", (run_id,)).fetchall())
    tickets = list(repo_of)
    trunk = {x["repo"]: x["trunk_sha"] for x in json.loads(d["repos_json"])}
    # (repo, trunk) a node's paths live in: its ticket's repo; root and its questions: any of the dispatch's
    at = lambda i: [(r, trunk[r]) for r in sorted({repo_of[i.split("/")[0]]} if i.split("/")[0] in repo_of
                                                   else set(repo_of.values())) if r in trunk]
    if not isinstance(nodes, list) or not nodes or not all(isinstance(n, dict) for n in nodes):
        raise StageError("the plan must be a non-empty JSON list of objects")
    questions = [n for n in nodes if "question" in n]
    nodes = [n for n in nodes if "question" not in n]
    seen, text_ok = set(), lambda n, t: len(str(n.get("title", ""))) <= 200 and len(str(n.get("detail", ""))) <= 2000
    for n in nodes:
        i = str(n.get("id", ""))
        m = STEP_ID.match(i)
        if not (i == "root" or i in tickets or (m and m[1] in tickets)):
            raise StageError(f"bad id {i!r}: want root, a ticket of this dispatch ({', '.join(tickets)}), or "
                             "<TICKET>/<n>[.<n>...]")
        if i in seen:
            raise StageError(f"duplicate id {i}")
        if not text_ok(n, i) or (m and not str(n.get("title", "")).strip()):
            raise StageError(f"{i}: title 1-200 chars (steps need one), detail <= 2000")
        seen.add(i)
    excluded = {n["id"]: str(n["exclude"]).strip() for n in nodes if n["id"] in tickets and n.get("exclude")}
    if any(not r for r in excluded.values()):
        raise StageError("exclude needs a reason")
    kept = [t for t in tickets if t not in excluded]
    if not kept:
        raise StageError("a dispatch keeps at least one ticket")
    steps = [n for n in nodes if "/" in n["id"]]
    if any(n["id"].split("/")[0] in excluded for n in steps):
        raise StageError("no steps for an excluded ticket")
    step_ids = {n["id"] for n in steps}
    for n in steps:
        if "." in n["id"] and n["id"].rsplit(".", 1)[0] not in step_ids:
            raise StageError(f"{n['id']}: parent step {n['id'].rsplit('.', 1)[0]} missing")
    for t in kept:
        c = sum(n["id"].startswith(t + "/") for n in steps)
        if not 1 <= c <= 12:
            raise StageError(f"{t}: {c} steps; want 1-12")
    ids = step_ids | set(kept)
    by_id = {n["id"]: n for n in nodes}
    results = {i: _line(f"{i} result (one line: what a user or system notices once it lands)",
                        by_id.get(i, {}).get("result")) for i in ["root", *kept]}
    results |= {n["id"]: _line(f"{n['id']} result", n["result"]) for n in steps if n.get("result") is not None}
    files = {n["id"]: _files(cfg, n["id"], n["files"], at(n["id"])) for n in steps if "files" in n}
    under = {n["id"]: n.get("under") for n in nodes if n["id"] in kept and n.get("under")}
    for t, u in under.items():
        if u not in kept or u == t:
            raise StageError(f"{t}: under must name another kept ticket")
    edges = {n["id"]: list(n.get("depends_on") or []) for n in nodes if n["id"] in ids}
    for i, deps in edges.items():
        if not isinstance(deps, list) or any(d not in ids or d == i for d in deps):
            raise StageError(f"{i}: depends_on must list other node ids of this dispatch (not excluded ones)")
    qs = _plan_questions(cfg, questions, ids | {"root"}, step_ids, kept, at)
    for graph, what in ((edges, "dependency cycle"), ({k: [v] for k, v in under.items()}, "tickets nested in a loop"),
                        ({q["key"]: [q["depends_on"]["question"]] for q in qs if q["depends_on"]},
                         "questions depending on each other")):
        state = {}

        def visit(x):
            if state.get(x) == 1:
                raise StageError(f"{what} through {x}")
            if state.get(x) != 2:
                state[x] = 1
                for y in graph.get(x, []):
                    visit(y)
                state[x] = 2
        for x in graph:
            visit(x)
    root = next((n for n in nodes if n["id"] == "root"), {})
    recommend = root.get("recommend", "approve")
    if recommend not in ("approve", "hold", "reject"):
        raise StageError("root recommend must be approve, hold or reject")
    review_why = str(root.get("why") or "").strip() or (
        f"The plan covers {len(kept)} ticket(s) in {len(steps)} step(s)"
        + (f"; dropped {', '.join(excluded)} as misfits" if excluded else "") + ". Nothing has changed since the draft.")
    # Jev guidance (and the pure-investigation gate) before anything is written; network, so outside the
    # transaction. A disabled or failing Jev changes nothing about the plan.
    guidance = jev.assess_plan(cfg, conn, run_id, qs, recommend, review_why)
    with db.tx(conn):
        # Refuse a stale plan: the draft may have moved while Jev judged it.
        d2 = conn.execute("SELECT state, planned_at FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d2 is None or d2["state"] != "draft" or d2["planned_at"]:
            raise StageError(f"{run_id} changed while the plan was being judged; nothing was written")
        for t, why in excluded.items():
            conn.execute("DELETE FROM dispatch_ticket WHERE run_id=? AND identifier=?", (run_id, t))
            conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                         (run_id, "root", "agent:factory-plan", f"Dropped {t} from this dispatch: {why}", db.now()))
        conn.executemany(
            "INSERT INTO dispatch_step(run_id, step_id, title, detail, depends_on_json, parent, result, files_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, n["id"], str(n.get("title") or "").strip() or n["id"], str(n.get("detail", "")).strip(),
              json.dumps(edges.get(n["id"], [])), under.get(n["id"]), results.get(n["id"]),
              json.dumps(files[n["id"]]) if n["id"] in files else None)
             for n in nodes if n["id"] == "root" or n["id"] in ids])
        if excluded:  # repos_json follows the kept tickets
            conn.execute("UPDATE dispatch SET repos_json=? WHERE run_id=?", (json.dumps(
                [r for r in json.loads(d["repos_json"]) if r["repo"] in {repo_of[t] for t in kept}]), run_id))
        conn.execute("UPDATE dispatch SET planned_at=?, planning_error=NULL WHERE run_id=?", (db.now(), run_id))
        learn.cite(conn, *(f"{n.get('title', '')} {n.get('detail', '')} {n.get('why', '')}" for n in nodes))
        rdid = decide.review(conn, run_id, recommend, review_why, "agent:factory-plan")
        if guidance and guidance["review"]:
            jev.store(conn, rdid, guidance["review"])
        for q, g in zip(qs, guidance["questions"] if guidance else (None,) * len(qs)):
            did = decide.open_(conn, "plan", q["question"], q["options"], q["recommend"], q["why"],
                               "agent:factory-plan", run_id=run_id, node_id=q["on"],
                               detail={k: q[k] for k in ("key", "now", "evidence", "depends_on")})
            if g:
                jev.store(conn, did, g)
    return {"run_id": run_id, "tickets": kept, "excluded": excluded, "steps": len(steps), "questions": len(qs),
            "recommend": recommend}


def note(conn, run_id: str, node: str, body: str, actor: str) -> dict:
    _draft(conn, run_id)
    if node not in {n["id"] for n in tree(conn, run_id)}:
        raise StageError(f"no node {node!r} in {run_id}; use root, a ticket id or a step id")
    if not body.strip() or len(body) > 4000:
        raise StageError("note must be 1-4000 characters")
    with db.tx(conn):
        cur = conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                           (run_id, node, actor, body.strip(), db.now()))
    return {"run_id": run_id, "node": node, "note": cur.lastrowid}


def hold(conn, run_id: str, reason: str, actor: str) -> dict:
    _draft(conn, run_id)
    if not reason.strip():
        raise StageError("say why it is held")
    with db.tx(conn):
        conn.execute("UPDATE dispatch SET held_reason=?, last_actor=? WHERE run_id=?",
                     (f"{reason.strip()} ({actor})", actor, run_id))
    return {"run_id": run_id, "review": "held"}


def replan(conn, run_id: str, reason: str, actor: str) -> dict:
    """Send a planned draft back to the planner: the reason becomes a binding root note, the plan and its open
    review/plan decisions go, and planning is requested again (the Plan stage) until the plan gate picks the draft up.
    Answered questions and notes stay."""
    d = _draft(conn, run_id)
    if not d["planned_at"]:
        raise StageError(f"{run_id} has no plan yet")
    reason = reason.strip()
    if not reason or len(reason) > 3900:
        raise StageError("say why it is replanned (1-3900 characters)")
    with db.tx(conn):
        conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                     (run_id, "root", actor, f"Replan: {reason}", db.now()))
        steps = conn.execute("DELETE FROM dispatch_step WHERE run_id=?", (run_id,)).rowcount
        conn.execute("UPDATE ask SET status='failed', error='replanned', answered_at=? WHERE status='pending' AND "
                     "decision_id IN (SELECT id FROM decision WHERE run_id=? AND kind IN ('review','plan') "
                     f"AND {decide.OPEN})", (db.now(), run_id))
        voided = decide.void(conn, "run_id=? AND kind IN ('review','plan')", (run_id,), "replanned")
        conn.execute("UPDATE dispatch SET planned_at=NULL, held_reason=NULL, planning_requested_at=?, last_actor=? "
                     "WHERE run_id=?", (db.now(), actor, run_id))
    return {"run_id": run_id, "review": "planning", "steps_cleared": steps, "decisions_voided": voided}


def _tickets_for_render(cfg: Config, conn, run_id: str, check: bool) -> tuple[list, dict]:
    """Ticket data as drafted (snapshot + verdict pinned in dispatch_ticket). A brief-backed dispatch uses the
    brief's compiled intent as the ticket description/title (never the raw Linear prose, which live source wording
    must not override); the pinned snapshot/verdict still supply url/repo/context/evidence. check=True refuses when
    the ticket or its verdict moved during review: the reviewed plan would no longer match."""
    d = conn.execute("SELECT brief_id FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    brief_id = d["brief_id"] if d else None
    tickets, bad = [], []
    for r in conn.execute("SELECT * FROM dispatch_ticket WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall():
        s = conn.execute("SELECT * FROM linear_snapshot WHERE issue_id=? AND updated_at=?",
                         (r["issue_id"], r["snapshot_updated_at"])).fetchone()
        v = conn.execute("SELECT * FROM verdict WHERE id=?", (r["verdict_id"],)).fetchone()
        raw = json.loads(s["raw_json"])
        if check:
            latest = prune.latest(conn, r["identifier"])
            ctx, _ = prune.map_context(cfg, latest)
            if v["superseded_at"] or prune.staleness(cfg, conn, latest, ctx):
                bad.append(r["identifier"])
        t = {"identifier": r["identifier"], "issue_id": r["issue_id"], "title": raw["title"],
             "url": raw["url"], "state": raw["state"]["name"],
             "assignee": (raw["assignee"] or {}).get("email"), "repo": v["repo"], "context": v["context"],
             "reason": v["reason"], "evidence": json.loads(v["evidence_json"]),
             "description": raw.get("description") or ""}
        if brief_id:
            tc = _ticket_context(conn, brief_id, r["identifier"]) or {}
            t["description"] = tc.get("description") or t["description"]
            t["title"] = tc.get("title") or t["title"]
        tickets.append(t)
    if bad:
        raise StageError(f"{', '.join(bad)} changed since the draft (ticket or verdict); reject it and draft again")
    return tickets, {x["repo"]: x["trunk_sha"] for x in json.loads(
        conn.execute("SELECT repos_json FROM dispatch WHERE run_id=?", (run_id,)).fetchone()[0])}


def _answers(conn, run_id: str) -> list:
    return [a for a in decide.rows(conn, run_id, open_only=False) if a["kind"] == "plan" and a["chosen"]]


def approve(cfg: Config, conn, run_id: str, actor: str) -> dict:
    """Review done (the review decision's `approve`): freeze the draft (plan, notes, answered questions) into an
    immutable dispatch.md. `start` then hands it to the executor."""
    _draft(conn, run_id)
    tickets, trunks = _tickets_for_render(cfg, conn, run_id, check=True)
    route = _route(cfg, conn, tickets)
    now = db.now()
    body = _render(run_id, now, actor, trunks, tickets, tree(conn, run_id), None, _answers(conn, run_id),
                   route, learn.pitfalls(conn, trunks)).encode()
    path = cfg.dispatches / run_id / "dispatch.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with db.tx(conn):
        path.write_bytes(body)
        conn.execute("UPDATE dispatch SET state='staged', body_sha256=?, staged_at=?, approved_by=?, last_actor=?, "
                     "route=? WHERE run_id=?", (hashlib.sha256(body).hexdigest(), now, actor, actor, route, run_id))
    os.chflags(path, stat.UF_IMMUTABLE)
    return {"run_id": run_id, "path": str(path), "route": route}


def _route(cfg: Config, conn, tickets: list) -> str | None:
    """The one factory-fleet home that owns every ticket (context route by Domain), else None: the captain routes."""
    owners = {cfg.context(t["context"]).owner(prune.issue_fields(prune.latest(conn, t["identifier"]))[0])
              for t in tickets}
    return owners.pop() if len(owners) == 1 else None


def start(cfg: Config, conn, run_id: str) -> dict:
    """After approval: optional Kanban mirror, then hand off. A busy or starting executor is not an error here;
    factory-propose retries the handoff every run."""
    tickets, _ = _tickets_for_render(cfg, conn, run_id, check=False)
    res = {"handoff": None, "handoff_error": None,
           "kanban": mirror_cards(cfg, conn, run_id, {t["identifier"]: t for t in tickets})}
    try:
        res["handoff"] = handoff(cfg, conn, run_id)
    except StageError as e:
        res["handoff_error"] = str(e)
    return res


def reject(cfg: Config, conn, run_id: str, reason: str, actor: str) -> dict:
    """Discard a draft (the review decision's `reject`). It is rendered (plan, notes, answers) into _archived/ as
    the record; its tickets are not drafted again until their verdict changes."""
    _draft(conn, run_id)
    if not reason.strip():
        raise StageError("say why it is rejected")
    tickets, trunks = _tickets_for_render(cfg, conn, run_id, check=False)
    now = db.now()
    body = _render(run_id, now, actor, trunks, tickets, tree(conn, run_id), reason.strip(),
                   _answers(conn, run_id)).encode()
    path = cfg.dispatches / "_archived" / run_id / "dispatch.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with db.tx(conn):
        path.write_bytes(body)
        conn.execute("UPDATE dispatch SET state='archived', body_sha256=?, archived_at=?, rejected_reason=?, "
                     "last_actor=? WHERE run_id=?", (hashlib.sha256(body).hexdigest(), now, reason.strip(), actor, run_id))
        scheduler.release_claims(conn, run_id)  # a rejected draft never runs: no leaked claims
    _commit_archived(cfg, run_id, f"reject draft {run_id}")
    return {"run_id": run_id, "state": "archived", "rejected": reason.strip(), "path": str(path)}


def mirror_cards(cfg: Config, conn, run_id: str, tickets: dict) -> list:
    """Optional Kanban mirror. Failure never unstages; it is reported in the result (the mirror is not a record)."""
    if not cfg.kanban.get("enabled"):
        return []
    board, res = cfg.kanban.get("board", "factory"), []
    for ident, t in tickets.items():
        try:
            r = subprocess.run(
                [HERMES, "kanban", "--board", board, "create", f"{ident}: {t['title']}",
                 "--body", f"Dispatch {run_id} ({cfg.dispatches / run_id / 'dispatch.md'})\n{t['url']}",
                 "--idempotency-key", f"factory:{run_id}:{ident}", "--completion-contract", t["repo"],
                 "--created-by", "factory", "--json"],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess([], 1, "", "hermes kanban create timed out")
        try:
            card = json.loads(r.stdout)["id"] if r.returncode == 0 else None
        except (json.JSONDecodeError, KeyError, TypeError):
            card = None
        if card is not None:
            conn.execute("UPDATE dispatch_ticket SET kanban_card_id=? WHERE run_id=? AND identifier=?",
                         (card, run_id, ident))
        res.append({"identifier": ident, "card": card, **({} if card else {"error": r.stderr[-400:]})})
    return res


def _herdr(*args: str) -> dict:
    try:
        r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StageError(f"herdr {' '.join(args)}: {e}") from None
    if r.returncode:
        raise StageError(f"herdr {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}


LAUNCH = Path(__file__).resolve().parents[1] / "fleet" / "launch-factory-primary.sh"


def _executor(want: str, wait: float = 180) -> dict:
    """The one omp pane in workspace `want`. Missing workspace or agent (reboot, crash, closed pane): create it
    and start factory-primary with the launch script, then wait until omp reports idle."""
    def omp_panes():
        ws = [w["workspace_id"] for w in _herdr("workspace", "list")["result"]["workspaces"] if w["label"] == want]
        panes = [p for p in _herdr("pane", "list")["result"]["panes"] if p["workspace_id"] in ws]
        return ws, panes, [p for p in panes if p.get("agent") == "omp"]
    ws, panes, omp = omp_panes()
    if len(ws) > 1 or len(omp) > 1:
        raise StageError(f"expected one workspace {want!r} with one omp pane, found {len(ws)} and {len(omp)}")
    if omp:
        return omp[0]
    if not ws:
        panes = [_herdr("workspace", "create", "--label", want, "--cwd", str(Path.home()), "--no-focus")["result"]["root_pane"]]
    _herdr("pane", "run", panes[0]["pane_id"], str(LAUNCH))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(5)
        if (omp := omp_panes()[2]) and omp[0].get("agent_status") in ("idle", "done"):
            return omp[0]
    raise StageError(f"started factory-primary in {panes[0]['pane_id']} but omp was not idle within {wait:.0f}s")


def _send(pane_id: str, texts: list[str]) -> None:
    for text in texts:
        _herdr("pane", "send-text", pane_id, text)
        _herdr("pane", "send-keys", pane_id, "enter")
        time.sleep(3)


FLEET_HOMES = Path.home() / ".local" / "share" / "factory-fleet" / "homes"


def _lead_pane_id(route: str) -> str | None:
    """The herdr pane factory-primary spawned the domain lead into (state/<route>.meta), if it was ever spawned."""
    meta = FLEET_HOMES / "factory-primary" / "state" / f"{route}.meta"
    if not meta.exists():
        return None
    return dict(x.split("=", 1) for x in meta.read_text().splitlines() if "=" in x).get("herdr_pane_id")


def _target(cfg: Config, d) -> tuple[dict, str]:
    """Who runs a dispatch: its route's domain lead when its omp pane is alive, else the captain (factory-primary),
    which routes each card itself and spawns or wakes the lead. Returns (pane, who)."""
    if d["route"] and (pid := _lead_pane_id(d["route"])):
        pane = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}.get(pid)
        if pane and pane.get("agent") == "omp":
            return pane, d["route"]
    return _executor(cfg.raw.get("executor", {}).get("workspace", "factory")), "factory-primary"


def _home_summary(route: str) -> dict | None:
    """The supervising home's summary as positive proof, or None when the file is missing, unreadable, malformed,
    or its proof fields are absent/wrong-typed. None is never proof of idle (terminal cleanup fails closed)."""
    f = FLEET_HOMES / route / "state" / "home-summary.json"
    try:
        s = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(s, dict) or not isinstance(s.get("active_children"), list) \
            or not isinstance(s.get("decisions_open"), list):
        return None
    return s


def _lead_busy(route: str) -> bool:
    """The lead still supervises crews or holds decisions: a /new now would drop what it has not written down.
    A missing/unreadable summary is not proof of activity here — handoff separately verifies the pane is idle."""
    s = _home_summary(route)
    return bool(s and (s["active_children"] or s["decisions_open"]))


def _home_idle(route: str) -> bool:
    """Positive proof the supervising home is idle: summary present, both proof fields lists, and both empty.
    Missing, malformed, unreadable or wrong-typed is NOT proof of idle (fail closed)."""
    s = _home_summary(route)
    return s is not None and not s["active_children"] and not s["decisions_open"]


def _launch_home(route: str | None, pane_id: str) -> str:
    """The real supervising home for a launch's pane: the route lead only when its spawned herdr pane matches the
    reserved pane, else the captain (handoff may have fallen back to the captain)."""
    if route and _lead_pane_id(route) == pane_id:
        return route
    return "factory-primary"


def _safety_check(cfg: Config, conn, run_id: str) -> None:
    """Refresh trunk for the dispatch's repos and refuse when any pinned ticket/verdict (or brief association) went
    stale since stage: recheck/replan, never a blanket stale bypass. Repo/evidence refresh only, no Linear read."""
    d = conn.execute("SELECT repos_json FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    repos_set = {r["repo"] for r in json.loads(d["repos_json"])}
    if repos_set:
        repos.sync_all(cfg, conn, only=repos_set)  # refresh code/data evidence trunks
    _tickets_for_render(cfg, conn, run_id, check=True)  # raises on stale ticket/verdict/superseded association


def handoff(cfg: Config, conn, run_id: str) -> dict:
    """Hand a staged dispatch to whoever runs it (`_target`): a safety freshness refresh, then an atomic launch
    reservation (capacity, resource, route and pane) before the external `/new`, then `run dispatch-intake
    <run_id>`. A send that fails partway is marked `uncertain` and is never auto-replayed; an already-reserved/sent
    dispatch is not re-sent."""
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "staged":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    _safety_check(cfg, conn, run_id)  # stale code/data evidence refuses before any external /new
    pane, who = _target(cfg, d)
    if pane.get("agent_status") not in ("idle", "done"):
        raise StageError(f"{who} pane {pane['pane_id']} is {pane.get('agent_status')}; not resetting a busy session")
    if who != "factory-primary" and _lead_busy(who):
        raise StageError(f"{who} still has crews or decisions open; not resetting its session")
    if existing := scheduler.launch(conn, run_id):
        if existing["state"] == "uncertain":
            raise StageError(f"{run_id} has an uncertain send to {existing['pane_id']}; confirm it is unsent "
                             "(release-unsent) before handing off again")
        return {"run_id": run_id, "executor_pane": existing["pane_id"], "via": who, "sent": None,
                "already": existing["state"]}
    if blocker := scheduler.reserve_if_free(conn, run_id, pane["pane_id"], os.getpid()):
        raise StageError(blocker)
    sent = ["/new", f"run dispatch-intake {run_id}"]  # session reset at every dispatch boundary
    try:
        _send(pane["pane_id"], sent)
    except Exception as e:  # any transport failure (StageError, OSError, timeout, JSON) may have partially landed
        scheduler.mark_uncertain(conn, run_id, str(e))
        raise StageError(f"send to {pane['pane_id']} is uncertain and will not be auto-replayed: {e}")
    scheduler.mark_sent(conn, run_id)
    return {"run_id": run_id, "executor_pane": pane["pane_id"], "via": who, "sent": sent}


def resume(cfg: Config, conn, run_id: str) -> dict:
    """The "restart the executor" choice on an executing dispatch. The restart is claimed atomically in a
    transaction (launch -> `uncertain`, the durable in-flight marker) before any reset/send, so a concurrent resume
    refuses on `uncertain` and the initial handoff is never replayed. After the marker, our own stuck executor is
    reset, then `/new` + intake; success re-marks `sent`, any transport failure retains `uncertain`. Never resets a
    pane another dispatch holds (launch or executing), a busy fallback pane, or replays an `uncertain` send."""
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "executing":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not executing")
    pane, who = _target(cfg, d)
    if other := conn.execute("SELECT run_id FROM dispatch_launch WHERE pane_id=? AND run_id<>?",
                             (pane["pane_id"], run_id)).fetchone():
        raise StageError(f"pane {pane['pane_id']} is reserved for dispatch {other['run_id']}; not resetting it")
    if other := conn.execute("SELECT run_id FROM dispatch WHERE executor_pane=? AND state='executing' AND run_id<>?",
                             (pane["pane_id"], run_id)).fetchone():
        raise StageError(f"pane {pane['pane_id']} is running dispatch {other['run_id']}; not resetting it")
    if pane.get("agent_status") not in ("idle", "done") and pane["pane_id"] != d["executor_pane"]:
        raise StageError(f"{who} pane {pane['pane_id']} is busy with unrelated work; not resetting it")
    with db.tx(conn):  # durable restart marker + atomic pane claim BEFORE any reset or external send
        d2 = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d2 is None or d2["state"] != "executing":
            raise StageError(f"{run_id} is no longer executing")
        held = conn.execute("SELECT 1 FROM dispatch_launch WHERE pane_id=? AND run_id<>?",
                            (pane["pane_id"], run_id)).fetchone() or conn.execute(
            "SELECT 1 FROM dispatch WHERE executor_pane=? AND state='executing' AND run_id<>?",
            (pane["pane_id"], run_id)).fetchone()
        if held:
            raise StageError(f"pane {pane['pane_id']} changed while restarting; not resetting it")
        l = scheduler.launch(conn, run_id)
        if l is not None and l["state"] == "uncertain":
            raise StageError(f"{run_id} has an uncertain send; resolve it before restarting (never auto-replay)")
        if l is not None:  # sent/reserved -> uncertain: the durable restart marker (second resume refuses)
            conn.execute("UPDATE dispatch_launch SET state='uncertain', pane_id=?, owner_pid=? WHERE run_id=?",
                         (pane["pane_id"], os.getpid(), run_id))
            conn.execute("UPDATE dispatch SET executor_pane=?, last_actor='factory:resume' WHERE run_id=?",
                         (pane["pane_id"], run_id))
        else:
            # Legacy run bootstrap (no launch yet): pin the replacement executor_pane first so the reserve-state
            # pane match passes, then insert the launch in the same transaction (a failed insert rolls both back).
            conn.execute("UPDATE dispatch SET executor_pane=?, last_actor='factory:resume' WHERE run_id=?",
                         (pane["pane_id"], run_id))
            conn.execute("INSERT INTO dispatch_launch(run_id, pane_id, state, owner_pid, claimed_at) "
                         "VALUES (?,?,?,?,?)", (run_id, pane["pane_id"], "uncertain", os.getpid(), db.now()))
    if pane.get("agent_status") not in ("idle", "done"):  # our own stuck executor, after the marker is durable
        _herdr("pane", "send-keys", pane["pane_id"], "esc")
        time.sleep(2)
    sent = ["/new", f"run dispatch-intake {run_id}"]
    try:
        _send(pane["pane_id"], sent)
    except Exception as e:  # any transport failure: retain uncertain, record the error truthfully
        conn.execute("UPDATE dispatch_launch SET error=? WHERE run_id=?", (str(e)[:400], run_id))
        raise StageError(f"restart send to {pane['pane_id']} is uncertain and will not be auto-replayed: {e}")
    scheduler.mark_sent(conn, run_id)  # uncertain -> sent on success
    return {"run_id": run_id, "executor_pane": pane["pane_id"], "via": who, "sent": sent}


def tell_executor(conn, run_id: str, text: str) -> str:
    """Type a message (an answer to its question) into the executor's pane; omp queues it if mid-turn."""
    d = conn.execute("SELECT executor_pane FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if not d or not d["executor_pane"]:
        raise StageError(f"dispatch {run_id} has no executor pane")
    _send(d["executor_pane"], [text])
    return d["executor_pane"]


def execute(cfg: Config, conn, run_id: str, actor: str) -> dict:
    """staged -> executing, only from the pane that runs it (the route's domain lead, or a pane in the captain's
    herdr workspace), only on an intact file. On an executing dispatch (after "restart the executor") it re-attaches
    this pane, unless another live omp owns it."""
    want = cfg.raw.get("executor", {}).get("workspace", "factory")
    ws, pane = os.environ.get("HERDR_WORKSPACE_ID"), os.environ.get("HERDR_PANE_ID")
    if not ws or not pane:
        raise StageError("execute runs only inside the executor's herdr pane (no HERDR_WORKSPACE_ID)")
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] not in ("staged", "executing"):
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    if not (d["route"] and pane == _lead_pane_id(d["route"])):
        r = subprocess.run(["herdr", "workspace", "get", ws], capture_output=True, text=True, timeout=10)
        label = json.loads(r.stdout)["result"]["workspace"]["label"] if r.returncode == 0 else None
        if label != want:
            raise StageError(f"caller pane {pane} is neither {d['route'] or 'a route'}'s lead nor in the executor "
                             f"workspace {want!r} (it is in {label!r})")
    path = cfg.dispatches / run_id / "dispatch.md"
    if hashlib.sha256(path.read_bytes()).hexdigest() != d["body_sha256"]:
        raise StageError(f"{path} does not match its staged sha256; refusing")
    if d["state"] == "executing":
        owner = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}.get(d["executor_pane"]) or {}
        if d["executor_pane"] != pane and owner.get("agent") == "omp":
            raise StageError(f"{run_id} is executing in live pane {d['executor_pane']}; not taking it over")
        with db.tx(conn):  # re-verify DB ownership under the lock; never take another run's pane
            d2 = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
            if d2 is None or d2["state"] != "executing":
                raise StageError(f"{run_id} is no longer executing")
            held = conn.execute("SELECT 1 FROM dispatch_launch WHERE pane_id=? AND run_id<>?",
                                (pane, run_id)).fetchone() or conn.execute(
                "SELECT 1 FROM dispatch WHERE executor_pane=? AND state='executing' AND run_id<>?",
                (pane, run_id)).fetchone()
            if held:
                raise StageError(f"pane {pane} is held by another dispatch; not taking it over")
            conn.execute("UPDATE dispatch SET executor_pane=?, last_actor=? WHERE run_id=?", (pane, actor, run_id))
    else:
        # Direct staged -> executing: refresh and re-check evidence freshness (same as handoff), then require its own
        # launch (a matching pane, reserved/sent) so the reservation admission guards still hold. A legacy NULL-brief
        # dispatch (no handoff yet) reserves its pane safely instead of bypassing the admission guards.
        _safety_check(cfg, conn, run_id)
        launch = scheduler.launch(conn, run_id)
        if launch is None:
            if d["brief_id"] is not None:
                raise StageError(f"{run_id} has no launch reservation; handoff must reserve it before it can execute")
            if blocker := scheduler.reserve_if_free(conn, run_id, pane, os.getpid()):
                raise StageError(blocker)
        else:
            if launch["pane_id"] != pane:
                raise StageError(f"{run_id} is reserved for pane {launch['pane_id']}, not {pane}")
            if launch["state"] not in ("reserved", "sent"):
                raise StageError(f"{run_id}'s launch is {launch['state']}; resolve it before executing")
        with db.tx(conn):
            conn.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane=?, last_actor=? "
                         "WHERE run_id=?", (db.now(), pane, actor, run_id))
    return {"run_id": run_id, "path": str(path), "executor_pane": pane,
            "tickets": [dict(r) for r in conn.execute(
                "SELECT t.identifier, v.repo, t.card_status FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                "WHERE t.run_id=?", (run_id,))]}


def release_unsent(cfg: Config, conn, run_id: str, actor: str, reason: str) -> dict:
    """Explicit, confirmed human recovery for a launch whose send may not have happened. Staged or terminal
    (done/reconciled/archived) dispatch, launch `reserved` or `uncertain`, nonempty human actor + reason, matching
    pane known + idle. Revalidated in a transaction, then uncertain->reserved->DELETE (the schema edge). Never
    auto-replayed: the caller confirms unsent at the CLI/API boundary (`--confirm-unsent` / `confirm_unsent:true`)."""
    if not actor or not actor.startswith("user:"):
        raise StageError("release-unsent needs a human actor (user:...); agents cannot confirm an unsent send")
    if not (reason or "").strip():
        raise StageError("say why the send is known unsent")
    l = scheduler.launch(conn, run_id)
    if l is None:
        raise StageError(f"dispatch {run_id} has no launch reservation to release")
    if l["state"] == "sent":
        raise StageError(f"{run_id} was already sent to {l['pane_id']}; not releasing a sent launch")
    if l["state"] not in ("reserved", "uncertain"):
        raise StageError(f"{run_id}'s launch is {l['state']}; nothing to release")
    panes = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}
    pane = panes.get(l["pane_id"])
    if pane is None:
        raise StageError(f"pane {l['pane_id']} is unknown; refusing to release an unverified send")
    if pane.get("agent_status") not in ("idle", "done"):
        raise StageError(f"pane {l['pane_id']} is {pane.get('agent_status')}; not releasing a live executor")
    route = conn.execute("SELECT route FROM dispatch WHERE run_id=?", (run_id,)).fetchone()["route"]
    if not _home_idle(_launch_home(route, l["pane_id"])):  # busy/unknown supervising home: refuse (fail closed)
        raise StageError("the supervising home is busy or its summary is unknown; not releasing an unverified send")
    with db.tx(conn):  # revalidate DB ownership/state under the lock; never release a changed/raced launch
        d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d is None or d["state"] not in ("staged", "done", "reconciled", "archived"):
            raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}; not releasing")
        l2 = scheduler.launch(conn, run_id)
        if l2 is None or l2["pane_id"] != l["pane_id"] or l2["state"] == "sent":
            raise StageError(f"{run_id}'s launch changed while verifying; not releasing")
        if l2["state"] == "uncertain":
            conn.execute("UPDATE dispatch_launch SET state='reserved', owner_pid=NULL WHERE run_id=?", (run_id,))
        scheduler.release_launch(conn, run_id)  # a reserved launch deletes anytime (definitely unsent)
    return {"run_id": run_id, "released": l["state"], "pane_id": l["pane_id"]}


def release_safe_terminal(cfg: Config, conn) -> list[str]:
    """Conservative terminal cleanup (proposer surface): release a terminal dispatch's `sent`/`reserved` launch only
    when its pane is observed idle AND the real supervising home has positive proof of no children and no decisions.
    A missing/malformed/busy summary or an `uncertain` launch is never auto-released. Returns the run ids released."""
    try:
        panes = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}
    except (StageError, OSError, subprocess.TimeoutExpired):
        return []  # cannot verify panes: never release blind
    released = []
    for l in conn.execute("SELECT l.run_id, l.pane_id, d.route FROM dispatch_launch l JOIN dispatch d USING (run_id) "
                          "WHERE d.state IN ('done','reconciled','archived') AND l.state IN ('reserved','sent')"
                          ).fetchall():
        pane = panes.get(l["pane_id"])
        if pane is None or pane.get("agent_status") not in ("idle", "done"):
            continue  # busy/unknown pane: keep the reservation
        if not _home_idle(_launch_home(l["route"], l["pane_id"])):  # missing/malformed/busy home: fail closed, keep
            continue
        with db.tx(conn):
            d2 = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (l["run_id"],)).fetchone()
            if d2 is None or d2["state"] not in ("done", "reconciled", "archived"):
                continue
            l2 = conn.execute("SELECT state FROM dispatch_launch WHERE run_id=?", (l["run_id"],)).fetchone()
            if l2 is None or l2["state"] not in ("reserved", "sent"):
                continue
            conn.execute("DELETE FROM dispatch_launch WHERE run_id=?", (l["run_id"],))
        released.append(l["run_id"])
    return released


def archive(cfg: Config, conn, run_id: str) -> dict:
    """reconciled -> archived: unlock, move to _archived/, commit it to the factory repo, and release the resource
    claims (terminal frees capacity via launch_active; the pane reservation is retained until release_safe_terminal)."""
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "reconciled":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not reconciled")
    src, dst = cfg.dispatches / run_id, cfg.dispatches / "_archived" / run_id
    if src.exists() or not dst.exists():  # re-run after a crash between rename and DB update: just finish the DB
        for p in [src, *src.rglob("*")]:
            os.chflags(p, 0)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    with db.tx(conn):
        conn.execute("UPDATE dispatch SET state='archived', archived_at=?, last_actor='factory:archive' WHERE run_id=?",
                     (db.now(), run_id))
        scheduler.release_claims(conn, run_id)  # archived: claims no longer conflict (claim_holders excludes it)
        # The pane reservation is retained through archive: release_safe_terminal releases it only after positive
        # proof the pane is idle and its home free of children/decisions.
    return {"run_id": run_id, "path": str(dst), "committed": _commit_archived(cfg, run_id, f"archive dispatch {run_id}")}


def _commit_archived(cfg: Config, run_id: str, message: str) -> bool:
    git = ["git", "-C", str(cfg.dispatches / "_archived")]
    subprocess.run([*git, "add", "--", run_id], capture_output=True)  # not a git checkout (tests): no commit
    return subprocess.run([*git, "commit", "-q", "-m", message, "--", run_id], capture_output=True).returncode == 0


CARD_TO = {"claim": "running", "done": "done", "block": "blocked"}


def _hermes_kanban(cfg: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HERMES, "kanban", "--board", cfg.kanban.get("board", "factory"), *args],
                          capture_output=True, text=True, timeout=120)


def card(cfg: Config, conn, run_id: str, ident: str, kind: str, actor: str,
         body: str | None = None, pr: str | None = None, ask: bool = True) -> dict:
    """claim|comment|done|block one card. Legal edges + auto-done are triggers; the CI gate is here. A block asks
    the user what next (`ask=False` when the user already decided, e.g. stopping the dispatch)."""
    t = conn.execute("SELECT t.*, d.state, v.repo FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
                     "JOIN verdict v ON v.id = t.verdict_id WHERE t.run_id=? AND t.identifier=?",
                     (run_id, ident)).fetchone()
    if t is None:
        raise StageError(f"{ident} is not in dispatch {run_id}")
    if t["state"] != "executing":
        raise StageError(f"dispatch {run_id} is {t['state']}, not executing")
    if kind in ("comment", "block") and not body:
        raise StageError(f"{kind} needs --body")
    if kind in CARD_TO and t["card_status"] not in ("ready", "running"):
        raise StageError(f"{ident} is already {t['card_status']}")
    if kind == "claim" and t["card_status"] != "ready":
        raise StageError(f"{ident} is already claimed")
    meta = None
    if kind == "done":
        # finks-ddd: Ready for QA only for a landed outcome = merged PR, green checks, known commit.
        if not pr or not re.fullmatch(rf"https://github\.com/{re.escape(t['repo'])}/pull/\d+", pr):
            raise StageError(f"done needs --pr https://github.com/{t['repo']}/pull/N (the ticket's repo)")
        if not body:
            raise StageError("done needs --body: the landed outcome, one or two sentences")
        os.environ.setdefault("GITHUB_TOKEN", secret(cfg, "GITHUB_TOKEN"))
        # A lead's pane may not have the nix profile on PATH; look where the fleet's launch scripts put gh.
        gh = shutil.which("gh") or shutil.which("gh", path=os.pathsep.join(
            [f"/etc/profiles/per-user/{os.environ.get('USER', '')}/bin", "/run/current-system/sw/bin",
             "/opt/homebrew/bin", str(Path.home() / ".local" / "bin")]))
        if not gh:
            raise StageError("gh not found on PATH or in the nix/homebrew profiles; done needs it to check the PR")
        r = subprocess.run([gh, "pr", "view", pr, "--json", "state,mergeCommit"],
                           capture_output=True, text=True, timeout=60)
        view = json.loads(r.stdout) if r.returncode == 0 else {}
        if view.get("state") != "MERGED":
            raise StageError(f"{pr} is {view.get('state', 'unreadable')}, not merged; done means landed")
        r = subprocess.run([gh, "pr", "checks", pr], capture_output=True, text=True, timeout=120)
        if r.returncode:  # 1 = failing, 8 = pending
            raise StageError(f"{pr} checks are not green (gh pr checks exit {r.returncode}); done refused")
        meta = {"pr": pr, "commit": view["mergeCommit"]["oid"], "checks": "gh pr checks: all passed"}
    with db.tx(conn):
        if kind in CARD_TO:
            conn.execute("UPDATE dispatch_ticket SET card_status=?, pr_url=coalesce(?, pr_url) "
                         "WHERE run_id=? AND identifier=?", (CARD_TO[kind], pr, run_id, ident))
        conn.execute("INSERT INTO card_event(run_id, issue_id, kind, actor, body, metadata_json, at) "
                     "VALUES (?,?,?,?,?,?,?)", (run_id, t["issue_id"], kind, actor, body,
                                               json.dumps(meta) if meta else None, db.now()))
        learn.cite(conn, body)
        if kind == "block" and ask:
            decide.blocked(conn, run_id, ident, t["issue_id"], body, actor)
        d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d["state"] != "executing":  # last card closed the dispatch: run-time questions no longer apply
            decide.void(conn, "run_id=? AND kind IN ('executor-gone','dispatch-stuck','ask')", (run_id,),
                        "the dispatch finished")
            # Terminal frees capacity (launch_active) automatically; the pane reservation is retained until
            # release_safe_terminal positively proves the pane idle and its home free of children/decisions.
    res = {"run_id": run_id, "identifier": ident, "event": kind,
           "card_status": conn.execute("SELECT card_status FROM dispatch_ticket WHERE run_id=? AND identifier=?",
                                       (run_id, ident)).fetchone()[0], "dispatch_state": d["state"]}
    if cfg.kanban.get("enabled") and t["kanban_card_id"]:  # optional mirror: a failure is reported, not recorded
        cid = t["kanban_card_id"]
        r = _hermes_kanban(cfg, *{
            "claim": ["claim", cid],
            "comment": ["comment", "--author", actor, cid, body or ""],
            "done": ["complete", cid, "--result", body or pr or "", "--metadata", json.dumps({"published_pr": pr})],
            "block": ["block", cid, body or ""],
        }[kind])
        if r.returncode:
            res["kanban_error"] = r.stderr[-400:]
    return res


PROPOSE = "factory:propose"


def _cohort(cfg: Config, conn, auto: list) -> list[str]:
    """Assemble one dispatch around the top candidate: the candidates that share its Linear Domain project first,
    then those in its repo with the same fleet owner (so one home runs it), up to stage.max_tickets. The planner may
    still drop a ticket that doesn't fit (it judges atomicity and ease); grouping itself is by facts, not guesses."""
    domain = lambda c: prune.issue_fields(prune.latest(conn, c["identifier"]))[0]
    seed = auto[0]
    same_domain = [c for c in auto[1:] if domain(c) == domain(seed)]
    same_repo = [c for c in auto[1:] if c["repo"] == seed["repo"] and c["route"] == seed["route"]
                 and c not in same_domain]
    return [c["identifier"] for c in [seed, *same_domain, *same_repo]][:max_tickets(cfg)]


def propose(cfg: Config, conn) -> dict:
    """Cron: consume approved briefs into bounded execution. Stages ready briefs up to a cap of 3 nonexecuting
    (draft/staged) dispatches — independently of whatever is executing or reconciling — hands off staged dispatches
    while capacity allows, and never lets a blocked first job starve a later independent one. No unapproved automatic
    brief: a human publishes/approves briefs; propose only stages approved ones. Legacy NULL-brief dispatches already
    in flight still finish unchanged. Draft review stays a decision: `decide.notify` + `decide.sweep` take ★ when the
    clock runs out."""
    cap = scheduler.sync_policy(conn, cfg.raw.get("executor", {}).get("max_parallel"))
    queue_cap = cfg.raw.get("executor", {}).get("queue_cap", 3)
    live = conn.execute("SELECT * FROM dispatch WHERE state <> 'archived' ORDER BY created_at").fetchall()
    staged = [d for d in live if d["state"] == "staged"]
    drafts = [d for d in live if d["state"] == "draft"]
    running = [d for d in live if d["state"] == "executing"]
    result = {"action": "idle",
              "capacity": {"max_parallel": cap, "used": scheduler.capacity_used(conn)},
              "running": [d["run_id"] for d in running],
              "queued": [d["run_id"] for d in staged] + [d["run_id"] for d in drafts],
              "handoffs": [], "prepared": [], "blocked": []}

    # Hand off staged dispatches, each independently: a blocked first must not starve a later independent one.
    for d in staged:
        try:
            result["handoffs"].append({"run_id": d["run_id"], "handoff": handoff(cfg, conn, d["run_id"])})
            result["action"] = "handoff"
        except StageError as e:
            result["handoffs"].append({"run_id": d["run_id"], "blocked": str(e)})

    # Prepare more approved briefs while the nonexecuting queue is below its cap; a blocked brief is skipped, not
    # a stop: an independent later brief can still be prepared.
    queued = len(staged) + len(drafts)
    if queued < queue_cap:
        for brief in _ready(cfg, conn):
            if queued >= queue_cap:
                break
            bid = brief.get("id")
            if not brief.get("ready"):
                result["blocked"].append({"brief_id": bid, "blockers": brief.get("blockers", [])})
                continue
            try:
                d = stage_brief(cfg, conn, bid, PROPOSE)
                result["prepared"].append({"brief_id": bid, "run_id": d["run_id"]})
                result["action"] = "prepared"
                queued += 1
            except StageError as e:
                result["blocked"].append({"brief_id": bid, "blocker": str(e)})
    # Conservative terminal cleanup: release terminal sent/reserved launches whose pane is observed idle and whose
    # real supervising home has positive proof of no children/decisions.
    result["released_terminal"] = release_safe_terminal(cfg, conn)
    return result


def _last_activity(conn, run_id: str) -> tuple[str | None, str | None]:
    """(when, what) of the latest real sign of work on a dispatch: a card event, its start, or an answer that reached
    its executor. Never an ask, a choice or a failed send."""
    return tuple(conn.execute(
        "SELECT at, what FROM (SELECT e.at, t.identifier || ' ' || e.kind what FROM card_event e "
        "JOIN dispatch_ticket t USING (run_id, issue_id) WHERE e.run_id=:r "
        "UNION ALL SELECT x.sent_at, 'answer to #' || x.decision_id || ' delivered' FROM executor_delivery x "
        "JOIN decision q ON q.id=x.decision_id WHERE q.run_id=:r AND x.state='sent' "
        "UNION ALL SELECT executing_at, 'dispatch started' FROM dispatch WHERE run_id=:r AND executing_at IS NOT NULL) "
        "ORDER BY at DESC LIMIT 1", {"r": run_id}).fetchone() or (None, None))


def _waiting_on(conn, run_id: str) -> tuple[int, str | None] | None:
    """(decision, delivery state) an executing dispatch's work waits on a person for: the oldest answer that has not
    reached the executor (only an explicit resend sends it), else the oldest open executor question (state None).
    A plain read: no sender recovery and no transaction, so `decide.choose` can ask it mid-answer."""
    row = conn.execute(
        "SELECT q.id, x.state FROM decision q LEFT JOIN executor_delivery x ON x.decision_id=q.id WHERE q.run_id=? "
        f"AND (x.state <> 'sent' OR (q.kind='ask' AND {decide.OPEN})) ORDER BY x.state IS NULL, q.id LIMIT 1",
        (run_id,)).fetchone()
    return tuple(row) if row else None


def watch(cfg: Config, conn) -> list:
    """Ask about an executing dispatch whose executor pane is gone, or with no activity (`_last_activity`) for
    executor.stuck_hours (default 6) while nothing waits on a person (`_waiting_on`; a wait never pauses the pane
    check): one open decision per dispatch and kind, not re-asked for stuck_hours after "wait", withdrawn once the
    condition clears or the dispatch stops executing."""
    stuck_h = cfg.raw.get("executor", {}).get("stuck_hours", 6)
    try:
        panes = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}
    except (StageError, OSError, subprocess.TimeoutExpired):
        panes = None  # herdr down: the pane check can't tell, the idle check still runs
    raised = []
    with db.tx(conn):
        decide.void(conn, "kind IN ('executor-gone','dispatch-stuck','ask') AND run_id NOT IN "
                          "(SELECT run_id FROM dispatch WHERE state='executing')", (), "the dispatch is no longer executing")
    for d in conn.execute("SELECT * FROM dispatch WHERE state='executing'").fetchall():
        at, what = _last_activity(conn, d["run_id"])
        idle_h = (datetime.now(UTC) - datetime.fromisoformat(at)).total_seconds() / 3600
        waiting = _waiting_on(conn, d["run_id"])
        checks = {"executor-gone": None if panes is None else
                  (panes.get(d["executor_pane"]) or {}).get("agent") != "omp",
                  "dispatch-stuck": idle_h > stuck_h and waiting is None}
        for kind, bad in checks.items():
            if bad is None:
                continue
            asked = conn.execute(f"SELECT 1 FROM decision WHERE run_id=? AND kind=? AND {decide.OPEN}",
                                 (d["run_id"], kind)).fetchone()
            with db.tx(conn):
                if not bad and asked:
                    decide.void(conn, "run_id=? AND kind=?", (d["run_id"], kind),
                                f"it waits on a person: decision #{waiting[0]}" if kind == "dispatch-stuck" and waiting
                                else "no longer the case")
                elif bad and not asked and not decide.snoozed(conn, d["run_id"], kind, stuck_h):
                    reason = (f"executor pane {d['executor_pane']} no longer runs omp; the dispatch cannot finish"
                              if kind == "executor-gone" else
                              f"no activity for {idle_h:.1f}h since {what} (limit {stuck_h}h)")
                    decide.executor(conn, d["run_id"], kind, reason, stuck_h)
                    raised.append({"run_id": d["run_id"], "kind": kind, "reason": reason})
    return raised


def runtime(conn, d) -> dict:
    """The Run tab's line on a staged or executing dispatch, from the database alone: no pane check, so a missing
    alert is no proof of health. Blocker by priority: executor gone, an answer that never arrived, an open executor
    question, gone quiet. `decision_id` names the decision it is about; nothing is answered for the user."""
    at, what = _last_activity(conn, d["run_id"])
    alert = {r["kind"]: r for r in conn.execute(
        "SELECT kind, id, coalesce(json_extract(detail_json, '$.reason'), question) reason FROM decision "
        f"WHERE run_id=? AND kind IN ('executor-gone', 'dispatch-stuck') AND {decide.OPEN}", (d["run_id"],))}
    waiting = _waiting_on(conn, d["run_id"])
    if gone := alert.get("executor-gone"):
        did, blocker, step = gone["id"], gone["reason"], f"answer decision #{gone['id']}"
    elif waiting and waiting[1]:
        did, state = waiting
        blocker = f"your answer to #{did} has not reached the executor ({state})"
        step = "wait for the send in progress" if state == "sending" else f"check the executor, then resend #{did}"
    elif waiting:
        did = waiting[0]
        blocker, step = f"the executor's question #{did} waits for your answer", f"answer decision #{did}"
    elif stuck := alert.get("dispatch-stuck"):
        did, blocker, step = stuck["id"], stuck["reason"], f"answer decision #{stuck['id']}"
    else:
        did, blocker = None, None
        step = "next card update from the executor" if d["state"] == "executing" else "the executor starts it"
    return {"last_activity_at": at, "last_activity_kind": what, "blocker": blocker, "next_step": step,
            "decision_id": did}

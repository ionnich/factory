"""Dispatch lifecycle: candidates, stage (draft -> staged). Invariants live in schema.sql triggers."""
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db, decide, prune
from .config import Config, secret

HERMES = str(Path.home() / ".local/bin/hermes")


class StageError(Exception):
    pass


def _foreign_holds(cfg: Config) -> dict[str, str]:
    """Where other fleets record their work (read-only): backlog files, plus live herdr workspace labels
    (nix-fleet crews name their worktree after the ticket, e.g. `fin3733-...` or `fin-2068-...`)."""
    pats = cfg.raw.get("stage", {}).get("foreign_backlogs", [])
    holds = {p: Path(p).read_text() for pat in pats for p in glob.glob(os.path.expanduser(pat))}
    own = cfg.raw.get("executor", {}).get("workspace", "factory")
    r = subprocess.run(["herdr", "workspace", "list"], capture_output=True, text=True, timeout=10)
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
        why = ("unmapped" if ctx is None
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
                   "context": ctx.name, "priority": raw["priority"], "state": raw["state"]["name"],
                   "reason": v["reason"]})
    ok.sort(key=lambda c: (c["priority"] or 5, c["repo"]))
    return {"max_tickets": max_tickets(cfg), "candidates": ok, "skipped": skipped,
            "suggested": _cohort(cfg, conn, ok) if ok else []}  # what the factory would group next


def max_tickets(cfg: Config) -> int:
    return cfg.raw.get("stage", {}).get("max_tickets", 3)


def _render(run_id: str, when: str, actor: str, trunks: dict, tickets: list, tree: list, rejected: str | None,
            answers: list) -> str:
    notes = lambda node: [f"- {n['author']} ({n['at'][:16]}Z): {n['body'].strip()}" for n in node["notes"]]
    root = tree[0]
    lines = [f"# Dispatch {run_id}", ""]
    lines += ([f"**REJECTED in review** by {actor} at {when}: {rejected}", ""] if rejected else
              [f"Approved {when} by {actor}. This file is immutable (`chflags uchg`); factory.db holds its sha256.", ""])
    lines += ["## Repos", ""] + [f"- {repo} @ trunk `{sha}`" for repo, sha in sorted(trunks.items())]
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
    if root["title"] != run_id or root["detail"]:
        lines += [f"## Theme: {root['title']}", "", root["detail"], ""]
    if root["notes"]:
        lines += ["## Operator notes (whole dispatch)", "", *notes(root), ""]
    if answers:
        label = lambda a: next(o for o in a["options"] if o["id"] == a["chosen"])
        lines += ["## Answered questions (binding)", ""]
        lines += [f"- On {a['node_id']}: {a['question']} **{label(a)['label']}**: {label(a)['leads_to']}"
                  + (f" Note: {a['chosen_note']}" if a["chosen_note"] else "") + f" ({a['chosen_by']})"
                  for a in answers]
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
        "depends_on": json.loads(r["depends_on_json"]) if r else [], "notes": notes.get(i, [])}
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


def review(d) -> str | None:
    if d["state"] != "draft":
        return None
    if d["planned_at"] is None:
        return "planning"
    if d["held_reason"]:
        return "held"
    return "in-review" if d["review_until"] else "waiting-approval"


def stage(cfg: Config, conn, identifiers: list[str], actor: str, emergency: bool = False) -> dict:
    """Draft a dispatch for review. Nothing is frozen or started until it is approved. Caller runs ingest first."""
    if not identifiers:
        raise StageError("name at least one ticket")
    if len(identifiers) > max_tickets(cfg):
        raise StageError(f"{len(identifiers)} tickets > stage.max_tickets {max_tickets(cfg)}; keep dispatches small")
    if len(set(identifiers)) != len(identifiers):
        raise StageError("duplicate ticket")
    cands = candidates(cfg, conn)
    ok = {c["identifier"] for c in cands["candidates"]}
    why = {s["identifier"]: s["reason"] for s in cands["skipped"]}
    bad = [f"{i}: {why.get(i, 'not an owned in-scope ticket')}" for i in identifiers if i not in ok]
    if bad:
        raise StageError("not stageable: " + "; ".join(bad))
    rows = []
    for ident in identifiers:
        s = prune.latest(conn, ident)
        ctx, _ = prune.map_context(cfg, s)
        v = conn.execute("SELECT id FROM verdict WHERE issue_id=? AND superseded_at IS NULL", (s["issue_id"],)).fetchone()
        rows.append((s["issue_id"], ident, s["updated_at"], v["id"], ctx.repo))
    trunks = {r[4]: conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (r[4],)).fetchone()["sha"] for r in rows}
    run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{identifiers[0].lower()}"
    with db.tx(conn):
        conn.execute("INSERT INTO dispatch(run_id, state, repos_json, last_actor, created_at, drafted_by, emergency) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (run_id, "draft", json.dumps([{"repo": r, "trunk_sha": s} for r, s in sorted(trunks.items())]),
                      actor, db.now(), actor, int(emergency)))
        conn.executemany("INSERT INTO dispatch_ticket(run_id, issue_id, identifier, snapshot_updated_at, verdict_id) "
                         "VALUES (?,?,?,?,?)", [(run_id, *r[:4]) for r in rows])
        for issue_id, ident, _, verdict_id, _ in rows:  # a retry after a block carries the user's guidance along
            g = conn.execute("SELECT x.run_id, x.chosen_note, x.chosen_by, json_extract(x.detail_json, '$.reason') "
                             "reason FROM decision x JOIN dispatch_ticket t ON t.run_id=x.run_id AND "
                             "t.issue_id=x.issue_id WHERE x.issue_id=? AND x.kind='blocked' AND x.chosen='retry' AND "
                             "t.verdict_id=? ORDER BY x.id DESC LIMIT 1", (issue_id, verdict_id)).fetchone()
            if g:
                conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                             (run_id, ident, g["chosen_by"], f"Retry of {g['run_id']}, which blocked: {g['reason']}\n"
                                                             f"Guidance: {g['chosen_note']}", db.now()))
    return {"run_id": run_id, "state": "draft", "tickets": identifiers, "emergency": emergency}


PLAN_QUESTIONS_MAX = 6


def _plan_questions(qs: list, node_ids: set) -> list:
    """Planner questions: each a real choice with consequences and a recommendation, like every decision."""
    if len(qs) > PLAN_QUESTIONS_MAX:
        raise StageError(f"{len(qs)} questions; at most {PLAN_QUESTIONS_MAX}. Decide the rest in the plan")
    out = []
    for q in qs:
        on, text, opts = str(q.get("on") or "root"), str(q.get("question", "")).strip(), q.get("options")
        if on not in node_ids:
            raise StageError(f"question {text[:40]!r}: on must be root, a kept ticket or a step id")
        if not 1 <= len(text) <= 300 or not str(q.get("why", "")).strip():
            raise StageError("a question needs question (1-300 chars) and why (your reason for the recommendation)")
        if (not isinstance(opts, list) or not 2 <= len(opts) <= 5
                or not all(isinstance(o, dict) and set(o) == {"id", "label", "leads_to"}
                           and all(str(o[k]).strip() for k in o) for o in opts)):
            raise StageError(f"question {text[:40]!r}: 2-5 options, each exactly {{id, label, leads_to}}")
        if q.get("recommend") not in {o["id"] for o in opts}:
            raise StageError(f"question {text[:40]!r}: recommend must be one of its option ids")
        out.append({"on": on, "question": text, "why": str(q["why"]).strip(), "recommend": q["recommend"],
                    "options": [decide.option(str(o["id"]), str(o["label"]), str(o["leads_to"])) for o in opts]})
    return out


def plan(conn, run_id: str, nodes: list) -> dict:
    """Planner agent: write the draft's plan tree once. Entries (JSON list):
      {"id": "root", "title": theme, "detail": why these tickets belong in one dispatch,
       "recommend": "approve"|"hold"|"reject", "why": your review recommendation}             optional
      {"id": "FIN-1", "detail": its role, "under": "FIN-2", "depends_on": ["FIN-3"]}        optional per ticket
      {"id": "FIN-1", "exclude": "why it does not fit this dispatch"}                         drops the ticket
      {"id": "FIN-1/2" or "FIN-1/2.1", "title", "detail", "depends_on": [node ids]}           steps, 1-12 per ticket
      {"question": text, "on": node id, "options": [{"id", "label", "leads_to"}, ...], "recommend": option id,
       "why": reason}                                                                        a choice for the reviewer
    `under` nests a ticket under another (a tree); depends_on is ordering (a DAG). Both must be acyclic.
    Questions the reviewer leaves open take their recommendation at approval."""
    d = _draft(conn, run_id)
    if d["planned_at"]:
        raise StageError(f"{run_id} already has a plan")
    tickets = [r[0] for r in conn.execute("SELECT identifier FROM dispatch_ticket WHERE run_id=? ORDER BY rowid",
                                          (run_id,))]
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
    under = {n["id"]: n.get("under") for n in nodes if n["id"] in kept and n.get("under")}
    for t, u in under.items():
        if u not in kept or u == t:
            raise StageError(f"{t}: under must name another kept ticket")
    edges = {n["id"]: list(n.get("depends_on") or []) for n in nodes if n["id"] in ids}
    for i, deps in edges.items():
        if not isinstance(deps, list) or any(d not in ids or d == i for d in deps):
            raise StageError(f"{i}: depends_on must list other node ids of this dispatch (not excluded ones)")
    for graph, what in ((edges, "dependency cycle"), ({k: [v] for k, v in under.items()}, "tickets nested in a loop")):
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
    qs = _plan_questions(questions, ids | {"root"})
    root = next((n for n in nodes if n["id"] == "root"), {})
    recommend = root.get("recommend", "approve")
    if recommend not in ("approve", "hold", "reject"):
        raise StageError("root recommend must be approve, hold or reject")
    review_why = str(root.get("why") or "").strip() or (
        f"The plan covers {len(kept)} ticket(s) in {len(steps)} step(s)"
        + (f"; dropped {', '.join(excluded)} as misfits" if excluded else "") + ". Nothing has changed since the draft.")
    with db.tx(conn):
        for t, why in excluded.items():
            conn.execute("DELETE FROM dispatch_ticket WHERE run_id=? AND identifier=?", (run_id, t))
            conn.execute("INSERT INTO dispatch_note(run_id, node_id, author, body, at) VALUES (?,?,?,?,?)",
                         (run_id, "root", "agent:factory-plan", f"Dropped {t} from this dispatch: {why}", db.now()))
        conn.executemany(
            "INSERT INTO dispatch_step(run_id, step_id, title, detail, depends_on_json, parent) VALUES (?,?,?,?,?,?)",
            [(run_id, n["id"], str(n.get("title") or "").strip() or n["id"], str(n.get("detail", "")).strip(),
              json.dumps(edges.get(n["id"], [])), under.get(n["id"]))
             for n in nodes if n["id"] == "root" or n["id"] in ids])
        if excluded:  # repos_json follows the kept tickets
            repos = {r[0] for r in conn.execute("SELECT v.repo FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                                                "WHERE t.run_id=?", (run_id,))}
            old = json.loads(conn.execute("SELECT repos_json FROM dispatch WHERE run_id=?", (run_id,)).fetchone()[0])
            conn.execute("UPDATE dispatch SET repos_json=? WHERE run_id=?",
                         (json.dumps([r for r in old if r["repo"] in repos]), run_id))
        conn.execute("UPDATE dispatch SET planned_at=? WHERE run_id=?", (db.now(), run_id))
        decide.review(conn, run_id, recommend, review_why, "agent:factory-plan")
        for q in qs:
            decide.open_(conn, "plan", q["question"], q["options"], q["recommend"], q["why"], "agent:factory-plan",
                         run_id=run_id, node_id=q["on"])
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


def _tickets_for_render(cfg: Config, conn, run_id: str, check: bool) -> tuple[list, dict]:
    """Ticket data as drafted (snapshot + verdict pinned in dispatch_ticket). check=True refuses when the ticket or
    its verdict moved during review: the reviewed plan would no longer match."""
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
        tickets.append({"identifier": r["identifier"], "issue_id": r["issue_id"], "title": raw["title"],
                        "url": raw["url"], "state": raw["state"]["name"],
                        "assignee": (raw["assignee"] or {}).get("email"), "repo": v["repo"], "context": v["context"],
                        "reason": v["reason"], "evidence": json.loads(v["evidence_json"]),
                        "description": raw.get("description") or ""})
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
    now = db.now()
    body = _render(run_id, now, actor, trunks, tickets, tree(conn, run_id), None, _answers(conn, run_id)).encode()
    path = cfg.dispatches / run_id / "dispatch.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with db.tx(conn):
        path.write_bytes(body)
        conn.execute("UPDATE dispatch SET state='staged', body_sha256=?, staged_at=?, approved_by=?, last_actor=? "
                     "WHERE run_id=?", (hashlib.sha256(body).hexdigest(), now, actor, actor, run_id))
    os.chflags(path, stat.UF_IMMUTABLE)
    return {"run_id": run_id, "path": str(path)}


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
    _commit_archived(cfg, run_id, f"reject draft {run_id}")
    return {"run_id": run_id, "state": "archived", "rejected": reason.strip(), "path": str(path)}


def mirror_cards(cfg: Config, conn, run_id: str, tickets: dict) -> list:
    """Optional Kanban mirror. Failure never unstages; it is reported in the result (the mirror is not a record)."""
    if not cfg.kanban.get("enabled"):
        return []
    board, res = cfg.kanban.get("board", "factory"), []
    for ident, t in tickets.items():
        r = subprocess.run(
            [HERMES, "kanban", "--board", board, "create", f"{ident}: {t['title']}",
             "--body", f"Dispatch {run_id} ({cfg.dispatches / run_id / 'dispatch.md'})\n{t['url']}",
             "--idempotency-key", f"factory:{run_id}:{ident}", "--completion-contract", t["repo"],
             "--created-by", "factory", "--json"],
            capture_output=True, text=True, timeout=60)
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
    r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=10)
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


def handoff(cfg: Config, conn, run_id: str) -> dict:
    """Hand a staged dispatch to the executor: fresh omp session (/new), then `run dispatch-intake <run_id>`."""
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "staged":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    if busy := conn.execute("SELECT run_id FROM dispatch WHERE state='executing'").fetchone():
        raise StageError(f"dispatch {busy[0]} is still executing")
    pane = _executor(cfg.raw.get("executor", {}).get("workspace", "factory"))
    if pane.get("agent_status") not in ("idle", "done"):
        raise StageError(f"executor pane {pane['pane_id']} is {pane.get('agent_status')}; not resetting a busy session")
    sent = ["/new", f"run dispatch-intake {run_id}"]  # session reset at every dispatch boundary
    _send(pane["pane_id"], sent)
    return {"run_id": run_id, "executor_pane": pane["pane_id"], "sent": sent}


def resume(cfg: Config, conn, run_id: str) -> dict:
    """The "restart the executor" choice on an executing dispatch: start factory-primary if it is gone, interrupt it if it is
    stuck mid-turn, then a fresh session runs intake again (`execute` re-attaches; finished cards stay finished)."""
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "executing":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not executing")
    pane = _executor(cfg.raw.get("executor", {}).get("workspace", "factory"))
    if pane.get("agent_status") not in ("idle", "done"):
        _herdr("pane", "send-keys", pane["pane_id"], "esc")
        time.sleep(2)
    sent = ["/new", f"run dispatch-intake {run_id}"]
    _send(pane["pane_id"], sent)
    return {"run_id": run_id, "executor_pane": pane["pane_id"], "sent": sent}


def tell_executor(conn, run_id: str, text: str) -> str:
    """Type a message (an answer to its question) into the executor's pane; omp queues it if mid-turn."""
    d = conn.execute("SELECT executor_pane FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if not d or not d["executor_pane"]:
        raise StageError(f"dispatch {run_id} has no executor pane")
    _send(d["executor_pane"], [text])
    return d["executor_pane"]


def execute(cfg: Config, conn, run_id: str, actor: str) -> dict:
    """staged -> executing, only from a pane in the executor's herdr workspace, only on an intact file. On an
    executing dispatch (after "restart the executor") it re-attaches this pane, unless another live omp owns it."""
    want = cfg.raw.get("executor", {}).get("workspace", "factory")
    ws, pane = os.environ.get("HERDR_WORKSPACE_ID"), os.environ.get("HERDR_PANE_ID")
    if not ws or not pane:
        raise StageError("execute runs only inside the executor's herdr pane (no HERDR_WORKSPACE_ID)")
    r = subprocess.run(["herdr", "workspace", "get", ws], capture_output=True, text=True, timeout=10)
    label = json.loads(r.stdout)["result"]["workspace"]["label"] if r.returncode == 0 else None
    if label != want:
        raise StageError(f"caller workspace {ws} is {label!r}, not the executor workspace {want!r}")
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] not in ("staged", "executing"):
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    path = cfg.dispatches / run_id / "dispatch.md"
    if hashlib.sha256(path.read_bytes()).hexdigest() != d["body_sha256"]:
        raise StageError(f"{path} does not match its staged sha256; refusing")
    if d["state"] == "executing":
        owner = {p["pane_id"]: p for p in _herdr("pane", "list")["result"]["panes"]}.get(d["executor_pane"]) or {}
        if d["executor_pane"] != pane and owner.get("agent") == "omp":
            raise StageError(f"{run_id} is executing in live pane {d['executor_pane']}; not taking it over")
        with db.tx(conn):
            conn.execute("UPDATE dispatch SET executor_pane=?, last_actor=? WHERE run_id=?", (pane, actor, run_id))
    else:
        with db.tx(conn):  # one_executing unique index refuses a second executing dispatch
            conn.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane=?, last_actor=? "
                         "WHERE run_id=?", (db.now(), pane, actor, run_id))
    return {"run_id": run_id, "path": str(path), "executor_pane": pane,
            "tickets": [dict(r) for r in conn.execute(
                "SELECT t.identifier, v.repo, t.card_status FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                "WHERE t.run_id=?", (run_id,))]}


def archive(cfg: Config, conn, run_id: str) -> dict:
    """reconciled -> archived: unlock, move to _archived/, commit it to the factory repo."""
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "reconciled":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not reconciled")
    src, dst = cfg.dispatches / run_id, cfg.dispatches / "_archived" / run_id
    for p in [src, *src.rglob("*")]:
        os.chflags(p, 0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    with db.tx(conn):
        conn.execute("UPDATE dispatch SET state='archived', archived_at=?, last_actor='factory:archive' WHERE run_id=?",
                     (db.now(), run_id))
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
        r = subprocess.run(["gh", "pr", "view", pr, "--json", "state,mergeCommit"],
                           capture_output=True, text=True, timeout=60)
        view = json.loads(r.stdout) if r.returncode == 0 else {}
        if view.get("state") != "MERGED":
            raise StageError(f"{pr} is {view.get('state', 'unreadable')}, not merged; done means landed")
        r = subprocess.run(["gh", "pr", "checks", pr], capture_output=True, text=True, timeout=120)
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
        if kind == "block" and ask:
            decide.blocked(conn, run_id, ident, t["issue_id"], body, actor)
        d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        if d["state"] != "executing":  # last card closed the dispatch: run-time questions no longer apply
            decide.void(conn, "run_id=? AND kind IN ('executor-gone','dispatch-stuck','ask')", (run_id,),
                        "the dispatch finished")
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


REVIEW = timedelta(hours=2)
EMERGENCY_MAX_PATHS = 3


def _emergency(cfg: Config, conn, ident: str) -> str | None:
    """Skip review only on facts, never on an agent's say-so: a person marked the ticket Urgent in Linear, and the
    verdict's evidence touches at most EMERGENCY_MAX_PATHS files (small blast radius). One ticket, auto repo."""
    s = prune.latest(conn, ident)
    v = conn.execute("SELECT evidence_paths_json FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                     (s["issue_id"],)).fetchone()
    paths = json.loads(v["evidence_paths_json"]) if v else []
    if json.loads(s["raw_json"])["priority"] == 1 and 0 < len(paths) <= EMERGENCY_MAX_PATHS:
        return f"Urgent in Linear, touches {len(paths)} file(s): {', '.join(paths)}"
    return None


def _cohort(cfg: Config, conn, auto: list) -> list[str]:
    """Assemble one dispatch around the top candidate: the candidates that share its Linear Domain project first,
    then those in its repo, up to stage.max_tickets. The planner may still drop a ticket that doesn't fit (it
    judges atomicity and ease); grouping itself is by facts, not guesses."""
    domain = lambda c: prune.issue_fields(prune.latest(conn, c["identifier"]))[0]
    seed = auto[0]
    same_domain = [c for c in auto[1:] if domain(c) == domain(seed)]
    same_repo = [c for c in auto[1:] if c["repo"] == seed["repo"] and c not in same_domain]
    return [c["identifier"] for c in [seed, *same_domain, *same_repo]][:max_tickets(cfg)]


def propose(cfg: Config, conn) -> dict:
    """Cron: move one dispatch at a time through review. Drafts a cohort of related `auto`-repo candidates; once the
    planner has written its plan, announces it (result["announce"], delivered to the factory Bot Chat / Hermex)
    and, after REVIEW, takes its review decision's recommendation unless someone answered or held it first.
    Emergency drafts start as soon as they are planned. A person's draft is never decided here. Staged (approved)
    dispatches are handed off, retried every run."""
    live = conn.execute("SELECT * FROM dispatch WHERE state <> 'archived' ORDER BY created_at").fetchall()
    if any(d["state"] in ("executing", "done", "reconciled") for d in live):
        return {"action": "wait", "dispatches": [{"run_id": d["run_id"], "state": d["state"]} for d in live]}
    if staged := [d for d in live if d["state"] == "staged"]:
        try:
            return {"action": "handoff", "handoff": handoff(cfg, conn, staged[0]["run_id"])}
        except StageError as e:
            return {"action": "staged", "run_id": staged[0]["run_id"], "handoff_error": str(e)}
    drafts = [d for d in live if d["state"] == "draft"]
    if not drafts:
        auto = [c for c in candidates(cfg, conn)["candidates"] if cfg.repos.get(c["repo"], {}).get("auto")]
        if not auto:
            return {"action": "idle", "reason": "no candidate in an auto repo"}
        why = _emergency(cfg, conn, auto[0]["identifier"])
        idents = [auto[0]["identifier"]] if why else _cohort(cfg, conn, auto)
        d = stage(cfg, conn, idents, PROPOSE, emergency=bool(why))
        return {"action": "drafted", "run_id": d["run_id"], "tickets": idents, "emergency": why}
    own = [d for d in drafts if d["drafted_by"] == PROPOSE]
    if not own:
        return {"action": "wait", "reason": "a person's draft is in review", "run_id": drafts[0]["run_id"]}
    d, run_id = own[0], own[0]["run_id"]
    if d["planned_at"] is None:
        return {"action": "planning", "run_id": run_id}
    if d["held_reason"]:
        return {"action": "held", "run_id": run_id, "reason": d["held_reason"]}
    rd = decide.open_review(conn, run_id)
    if rd is None:
        return {"action": "wait", "reason": "no open review decision", "run_id": run_id}
    what = "; ".join(f"{n['id']} {n['title']}" for n in tree(conn, run_id) if n["kind"] == "ticket")
    url = cfg.raw.get("notify", {}).get("url", "")
    rec = next(o for o in rd["options"] if o["id"] == rd["recommended"])
    now = datetime.now(UTC)
    if not d["emergency"] and d["review_until"] is None:
        until = now + REVIEW
        with db.tx(conn):
            conn.execute("UPDATE dispatch SET notified_at=?, review_until=? WHERE run_id=?",
                         (db.now(), until.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), run_id))
        return {"action": "in-review", "run_id": run_id, "review_until": until.isoformat(),
                "announce": f"Dispatch {run_id} is ready for review: {what}. Recommended: {rec['label']} "
                            f"({rd['why']}). At {until.astimezone():%H:%M} the factory takes that recommendation "
                            f"unless you choose first. {url}"}
    if not d["emergency"] and now < datetime.fromisoformat(d["review_until"]):
        return {"action": "in-review", "run_id": run_id, "review_until": d["review_until"]}
    choice = "approve" if d["emergency"] else rd["recommended"]
    actor = f"{PROPOSE} ({'emergency: no review window' if d['emergency'] else 'review window elapsed; took the recommendation'})"
    try:
        res = decide.choose(cfg, conn, rd["id"], choice, actor, note=f"review window elapsed: {rd['why']}")
    except StageError as e:  # ticket or verdict moved during review: the reviewed plan is void
        decide.choose(cfg, conn, rd["id"], "reject", PROPOSE, note=f"not started: {e}")
        return {"action": "rejected", "run_id": run_id, "reason": str(e),
                "announce": f"Dispatch {run_id} was dropped instead of started: {e}."}
    if choice != "approve":
        return {"action": choice, "run_id": run_id, **res,
                "announce": f"Dispatch {run_id}: the review window ended and the factory took its recommendation, "
                            f"{rec['label']} ({rd['why']}). {url}"}
    head = "Emergency dispatch" if d["emergency"] else "Dispatch"
    tail = "without review" if d["emergency"] else "after the review window"
    return {"action": "started", "run_id": run_id, **res,
            "announce": f"{head} {run_id} started {tail}: {what}. {url}"}


def watch(cfg: Config, conn) -> list:
    """Ask about an executing dispatch whose executor pane is gone, or with no card activity for
    executor.stuck_hours (default 6): one open decision per dispatch and kind, not re-asked for stuck_hours after
    "wait", withdrawn once the condition clears or the dispatch stops executing."""
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
        last = conn.execute("SELECT max(at) FROM card_event WHERE run_id=?", (d["run_id"],)).fetchone()[0]
        idle_h = (datetime.now(UTC) - datetime.fromisoformat(max(filter(None, (last, d["executing_at"]))))
                  ).total_seconds() / 3600
        checks = {"executor-gone": None if panes is None else
                  (panes.get(d["executor_pane"]) or {}).get("agent") != "omp",
                  "dispatch-stuck": idle_h > stuck_h}
        for kind, bad in checks.items():
            if bad is None:
                continue
            asked = conn.execute(f"SELECT 1 FROM decision WHERE run_id=? AND kind=? AND {decide.OPEN}",
                                 (d["run_id"], kind)).fetchone()
            with db.tx(conn):
                if not bad and asked:
                    decide.void(conn, "run_id=? AND kind=?", (d["run_id"], kind), "no longer the case")
                elif bad and not asked and not decide.snoozed(conn, d["run_id"], kind, stuck_h):
                    reason = (f"executor pane {d['executor_pane']} no longer runs omp; the dispatch cannot finish"
                              if kind == "executor-gone" else f"no card activity for {idle_h:.1f}h (limit {stuck_h}h)")
                    decide.executor(conn, d["run_id"], kind, reason, stuck_h)
                    raised.append({"run_id": d["run_id"], "kind": kind, "reason": reason})
    return raised

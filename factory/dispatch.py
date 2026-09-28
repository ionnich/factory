"""Dispatch lifecycle: candidates, stage (draft -> staged). Invariants live in schema.sql triggers."""
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from . import db, prune
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
        why = ("unmapped" if ctx is None
               else "no verdict" if v is None
               else f"verdict {v['kind']}" if v["kind"] != "valid"
               else f"verdict stale ({r})" if (r := prune.staleness(cfg, conn, s, ctx))
               else "in a live dispatch" if s["issue_id"] in live
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
    return {"max_tickets": max_tickets(cfg), "candidates": ok, "skipped": skipped}


def max_tickets(cfg: Config) -> int:
    return cfg.raw.get("stage", {}).get("max_tickets", 3)


def _render(run_id: str, staged_at: str, actor: str, trunks: dict, tickets: list) -> str:
    lines = [f"# Dispatch {run_id}", "",
             f"Staged {staged_at} by {actor}. This file is immutable (`chflags uchg`); factory.db holds its sha256.",
             "", "## Repos", ""]
    lines += [f"- {repo} @ trunk `{sha}`" for repo, sha in sorted(trunks.items())]
    lines += ["", "## Rules", "",
              "- Work only the tickets below. One PR per ticket, against the repo's trunk.",
              "- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL.",
              "- Never write to Linear. Reconcile does that after the dispatch closes.",
              "- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, "
              "`factory card block` with the evidence instead of forcing a change.", ""]
    for t in tickets:
        ev = "\n".join(f"  - `{json.dumps(e, sort_keys=True)}`" for e in t["evidence"])
        lines += [f"## {t['identifier']}: {t['title']}", "",
                  f"- Linear: {t['url']} ({t['state']}, assignee {t['assignee'] or 'none'})",
                  f"- Repo: {t['repo']} (context {t['context']}), trunk `{trunks[t['repo']]}`",
                  f"- Verdict: valid — {t['reason']}",
                  f"- Evidence:\n{ev}", "",
                  "### Ticket body (claim, not truth)", "", t["description"].strip() or "_(empty)_", ""]
    return "\n".join(lines)


def stage(cfg: Config, conn, identifiers: list[str], actor: str) -> dict:
    """Freeze the chosen tickets into an immutable dispatch. Caller runs ingest first."""
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

    tickets = []
    for ident in identifiers:
        s = prune.latest(conn, ident)
        raw = json.loads(s["raw_json"])
        ctx, _ = prune.map_context(cfg, s)
        v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                         (s["issue_id"],)).fetchone()
        tickets.append({"identifier": ident, "issue_id": s["issue_id"], "updated_at": s["updated_at"],
                        "verdict_id": v["id"], "title": raw["title"], "url": raw["url"],
                        "state": raw["state"]["name"], "assignee": (raw["assignee"] or {}).get("email"),
                        "repo": ctx.repo, "context": ctx.name, "reason": v["reason"],
                        "evidence": json.loads(v["evidence_json"]), "description": raw.get("description") or ""})
    trunks = {t["repo"]: conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (t["repo"],)).fetchone()["sha"]
              for t in tickets}
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%d-%H%M%S}-{identifiers[0].lower()}"
    body = _render(run_id, db.now(), actor, trunks, tickets).encode()
    path = cfg.dispatches / run_id / "dispatch.md"

    with db.tx(conn):
        conn.execute("INSERT INTO dispatch(run_id, state, repos_json, last_actor, created_at) VALUES (?,?,?,?,?)",
                     (run_id, "draft", json.dumps([{"repo": r, "trunk_sha": s} for r, s in sorted(trunks.items())]),
                      actor, db.now()))
        conn.executemany(
            "INSERT INTO dispatch_ticket(run_id, issue_id, identifier, snapshot_updated_at, verdict_id) "
            "VALUES (?,?,?,?,?)",
            [(run_id, t["issue_id"], t["identifier"], t["updated_at"], t["verdict_id"]) for t in tickets])
        path.parent.mkdir(parents=True)
        path.write_bytes(body)
        conn.execute("UPDATE dispatch SET state='staged', body_sha256=?, staged_at=?, last_actor=? WHERE run_id=?",
                     (hashlib.sha256(body).hexdigest(), db.now(), actor, run_id))
    os.chflags(path, stat.UF_IMMUTABLE)
    return {"run_id": run_id, "path": str(path), "tickets": identifiers,
            "kanban": mirror_cards(cfg, conn, run_id, {t["identifier"]: t for t in tickets})}


def mirror_cards(cfg: Config, conn, run_id: str, tickets: dict) -> list:
    """Optional Kanban mirror. Failure never unstages: it raises a flag instead."""
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
        if card is None:
            flag(conn, run_id, t["issue_id"], "kanban-mirror", {"op": "create", "stderr": r.stderr[-400:]})
        else:
            conn.execute("UPDATE dispatch_ticket SET kanban_card_id=? WHERE run_id=? AND identifier=?",
                         (card, run_id, ident))
        res.append({"identifier": ident, "card": card})
    return res


def _herdr(*args: str) -> dict:
    r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise StageError(f"herdr {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}


def handoff(cfg: Config, conn, run_id: str) -> dict:
    """Hand a staged dispatch to the executor: fresh omp session (/new), then `run dispatch-intake <run_id>`."""
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "staged":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    if busy := conn.execute("SELECT run_id FROM dispatch WHERE state='executing'").fetchone():
        raise StageError(f"dispatch {busy[0]} is still executing")
    want = cfg.raw.get("executor", {}).get("workspace", "factory")
    ws = [w["workspace_id"] for w in _herdr("workspace", "list")["result"]["workspaces"] if w["label"] == want]
    panes = [p for p in _herdr("pane", "list")["result"]["panes"] if p["workspace_id"] in ws and p.get("agent") == "omp"]
    if len(panes) != 1:
        raise StageError(f"expected one omp pane in workspace {want!r}, found {len(panes)}")
    pane = panes[0]
    if pane.get("agent_status") not in ("idle", "done"):
        raise StageError(f"executor pane {pane['pane_id']} is {pane.get('agent_status')}; not resetting a busy session")
    for text in ("/new", f"run dispatch-intake {run_id}"):  # session reset at every dispatch boundary
        _herdr("pane", "send-text", pane["pane_id"], text)
        _herdr("pane", "send-keys", pane["pane_id"], "enter")
        time.sleep(3)
    return {"run_id": run_id, "executor_pane": pane["pane_id"], "sent": ["/new", f"run dispatch-intake {run_id}"]}


def execute(cfg: Config, conn, run_id: str, actor: str) -> dict:
    """staged -> executing, only from a pane in the executor's herdr workspace, only on an intact file."""
    want = cfg.raw.get("executor", {}).get("workspace", "factory")
    ws, pane = os.environ.get("HERDR_WORKSPACE_ID"), os.environ.get("HERDR_PANE_ID")
    if not ws or not pane:
        raise StageError("execute runs only inside the executor's herdr pane (no HERDR_WORKSPACE_ID)")
    r = subprocess.run(["herdr", "workspace", "get", ws], capture_output=True, text=True, timeout=10)
    label = json.loads(r.stdout)["result"]["workspace"]["label"] if r.returncode == 0 else None
    if label != want:
        raise StageError(f"caller workspace {ws} is {label!r}, not the executor workspace {want!r}")
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "staged":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not staged")
    path = cfg.dispatches / run_id / "dispatch.md"
    if hashlib.sha256(path.read_bytes()).hexdigest() != d["body_sha256"]:
        raise StageError(f"{path} does not match its staged sha256; refusing")
    with db.tx(conn):  # one_executing unique index refuses a second executing dispatch
        conn.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane=?, last_actor=? "
                     "WHERE run_id=?", (db.now(), pane, actor, run_id))
    return {"run_id": run_id, "path": str(path), "executor_pane": pane,
            "tickets": [dict(r) for r in conn.execute(
                "SELECT t.identifier, v.repo, t.card_status FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                "WHERE t.run_id=?", (run_id,))]}


def archive(cfg: Config, conn, run_id: str) -> dict:
    """reconciled -> archived: unlock, move to _archived/, commit it to the planner repo."""
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
    git = ["git", "-C", str(dst.parent)]
    subprocess.run([*git, "add", "--", run_id], check=True, capture_output=True)
    r = subprocess.run([*git, "commit", "-q", "-m", f"archive dispatch {run_id}", "--", run_id],
                       capture_output=True, text=True)
    return {"run_id": run_id, "path": str(dst), "committed": r.returncode == 0}


CARD_TO = {"claim": "running", "done": "done", "block": "blocked"}


def _hermes_kanban(cfg: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HERMES, "kanban", "--board", cfg.kanban.get("board", "factory"), *args],
                          capture_output=True, text=True, timeout=120)


def card(cfg: Config, conn, run_id: str, ident: str, kind: str, actor: str,
         body: str | None = None, pr: str | None = None) -> dict:
    """claim|comment|done|block one card. Legal edges + auto-done are triggers; the CI gate is here."""
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
    if cfg.kanban.get("enabled") and t["kanban_card_id"]:
        cid = t["kanban_card_id"]
        r = _hermes_kanban(cfg, *{
            "claim": ["claim", cid],
            "comment": ["comment", "--author", actor, cid, body or ""],
            "done": ["complete", cid, "--result", body or pr or "", "--metadata", json.dumps({"published_pr": pr})],
            "block": ["block", cid, body or ""],
        }[kind])
        if r.returncode:
            flag(conn, run_id, t["issue_id"], "kanban-mirror", {"op": kind, "stderr": r.stderr[-400:]})
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    return {"run_id": run_id, "identifier": ident, "event": kind,
            "card_status": conn.execute("SELECT card_status FROM dispatch_ticket WHERE run_id=? AND identifier=?",
                                        (run_id, ident)).fetchone()[0], "dispatch_state": d["state"]}


def flag(conn, run_id: str | None, issue_id: str | None, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO flag(run_id, issue_id, kind, detail_json, created_at) VALUES (?,?,?,?,?)",
                 (run_id, issue_id, kind, json.dumps(detail), db.now()))

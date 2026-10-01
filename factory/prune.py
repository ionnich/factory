"""Verdict freshness, the prune gate, and validated verdict writes."""
import json
import re
from datetime import UTC, datetime, timedelta

from . import db, learn, repos, witness
from .config import Config, Context

KINDS = ("valid", "already-done", "stale", "duplicate-of", "invalid-references", "needs-clarification")
NO_WITNESS_KINDS = {"valid", "needs-clarification"}
PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
MAP_ACTOR = "factory:map"
VALID_TTL = timedelta(days=7)


class VerdictError(Exception):
    pass


_DOMAIN = re.compile(r"^\s*\**Domain\**:\**\s*(?:\[([^\]]+)\]|(.+?))(?:\(.*)?\s*$", re.M)
_REPO = re.compile(r"^\s*\**Repos?:?\**:?\s*(.+)$", re.M)
_DOMAIN_URL = re.compile(r"^\s*\**Domain\**:[^\n]*?linear\.app/[^/\s]+/project/([\w-]+)", re.M)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class NotOwned(Exception):
    pass


def domain_project(conn, snapshot):
    """The canonical Domain project row (linear_project), resolved from the Domain: link, else its name."""
    body = json.loads(snapshot["raw_json"]).get("description") or ""
    m = _DOMAIN_URL.search(body)
    if m:
        tail = m.group(1)
        u = _UUID.search(tail)
        row = conn.execute("SELECT * FROM linear_project WHERE " + ("id=?" if u else "slug_id=?"),
                           (u.group(0) if u else tail.rsplit("-", 1)[-1],)).fetchone()
        if row:
            return row
    domain = issue_fields(snapshot)[0]
    return conn.execute("SELECT * FROM linear_project WHERE name=?", (domain,)).fetchone() if domain else None


def owned(cfg: Config, conn, snapshot) -> bool:
    """Only tickets whose Domain project is led by linear.lead are the factory's concern, minus linear.ignore."""
    if snapshot["identifier"] in cfg.linear.get("ignore", []):
        return False
    p = domain_project(conn, snapshot)
    return p is not None and p["lead_email"] == cfg.linear["lead"]


def owned_in_scope(cfg: Config, conn) -> list:
    """Owned active tickets, minus those in their team's review_state: those wait on a human (QA), and the factory
    neither re-judges, restages nor closes them."""
    if not conn.execute("SELECT 1 FROM linear_project LIMIT 1").fetchone():
        raise NotOwned("linear_project is empty; run factory ingest")
    review = {k: t.get("review_state") for k, t in cfg.linear.get("team", {}).items()}
    # Tickets in a staged/executing/finished dispatch are frozen; drafts still get re-judged (staleness on approve).
    busy = {r[0] for r in conn.execute(
        "SELECT t.issue_id FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
        "WHERE d.state IN ('staged','executing','done','reconciled')")}
    return [s for s in conn.execute("SELECT * FROM linear_latest WHERE in_scope=1")
            if s["issue_id"] not in busy and owned(cfg, conn, s)
            and (raw := json.loads(s["raw_json"]))["state"]["name"] != review.get(raw["team"]["key"])]


def issue_fields(snapshot) -> tuple[str | None, list[str], list[str]]:
    """(canonical Domain: project, labels, Repo: names). Domain comes from the body line, never issue.project."""
    raw = json.loads(snapshot["raw_json"])
    body = raw.get("description") or ""
    m = _DOMAIN.search(body)
    domain = (m.group(1) or m.group(2)).strip() if m else None
    repo_lines = [r.split("/")[-1] for line in _REPO.findall(body)
                  for r in re.findall(r"[\w.-]+(?:/[\w.-]+)?", line.replace("`", " "))]
    return domain, [label["name"] for label in raw["labels"]["nodes"]], repo_lines


def map_context(cfg: Config, snapshot) -> tuple[Context | None, str | None]:
    """Exactly one configured context, else (None, reason). The agent never infers."""
    domain, labels, repo_lines = issue_fields(snapshot)
    found = cfg.context_for(domain, labels, repo_lines)
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, "no bounded context" + ("" if domain else " (no Domain: line)")
    return None, "ambiguous bounded context: " + ", ".join(c.name for c in found)


def latest(conn, identifier: str):
    row = conn.execute("SELECT * FROM linear_latest WHERE identifier=?", (identifier,)).fetchone()
    if row is None:
        raise VerdictError(f"no snapshot for {identifier}; run factory ingest")
    return row


def verdict_staleness(cfg: Config, conn, snapshot, ctx: Context | None, v) -> str | None:
    """Freshness of a specific verdict `v` against the current snapshot/trunk (the shared staleness rules). Used by
    staleness() for the current verdict, and by brief-backed staging for the exact associated verdict."""
    if v is None:
        return "new"
    if v["snapshot_updated_at"] != snapshot["updated_at"] and not conn.execute(
            "SELECT 1 FROM linear_own_write WHERE issue_id=? AND updated_at=?",
            (snapshot["issue_id"], snapshot["updated_at"])).fetchone():
        return "ticket-changed"
    if v["context"] != (ctx.name if ctx else None):
        return "context-changed"
    # Evidence paths catch trunk changes that touch what the verdict read; a fix landing elsewhere doesn't.
    # So a `valid` verdict (the one that gets staged) is also redone once it is a week old.
    if v["kind"] == "valid" and datetime.fromisoformat(v["created_at"]) < datetime.now(UTC) - VALID_TTL:
        return "aged"
    paths = set(json.loads(v["evidence_paths_json"]))
    if ctx is None or not paths:
        return None
    trunk = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (ctx.repo,)).fetchone()
    if trunk is None or trunk["sha"] == v["trunk_sha"]:
        return None
    changed = repos.changed_paths(cfg.mirror_path(ctx.repo), v["trunk_sha"], trunk["sha"])
    if changed is None or paths & changed:
        return "evidence-changed"
    return None


def staleness(cfg: Config, conn, snapshot, ctx: Context | None) -> str | None:
    """None when the current verdict still holds, else why it must be redone."""
    v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                     (snapshot["issue_id"],)).fetchone()
    return verdict_staleness(cfg, conn, snapshot, ctx, v)


def _approved_head_brief(conn, brief_id: int) -> dict:
    """An approved, current (head-of-lineage) brief: not held, not superseded by a newer published revision."""
    row = conn.execute("SELECT * FROM work_brief WHERE id=?", (brief_id,)).fetchone()
    if row is None:
        raise VerdictError(f"no brief #{brief_id}")
    if row["state"] != "approved":
        raise VerdictError(f"brief #{brief_id} is {row['state']}, not approved")
    if conn.execute("SELECT 1 FROM work_brief WHERE parent_id=? AND state IN ('approved','held')",
                    (brief_id,)).fetchone():
        raise VerdictError(f"brief #{brief_id} was superseded by a newer revision; use the current one")
    return {"id": row["id"], "sources": json.loads(row["sources_json"])}


def _associated_stale(cfg: Config, conn, issue_id: str, verdict_id: int) -> bool:
    """True when a brief_verdict association's verdict is no longer current-valid-fresh (superseded, non-valid, or
    stale evidence/aged). A stale current association must force fresh brief verification, never a raw fallback."""
    v = conn.execute("SELECT * FROM verdict WHERE id=?", (verdict_id,)).fetchone()
    if v is None or v["superseded_at"] is not None or v["kind"] != "valid":
        return True
    cur = conn.execute("SELECT * FROM linear_latest WHERE issue_id=?", (issue_id,)).fetchone()
    if cur is None:
        return True
    ctx, _ = map_context(cfg, cur)
    return verdict_staleness(cfg, conn, cur, ctx, v) is not None


def _brief_source_issues(conn) -> set:
    """issue ids of every source of a published (approved|held) brief, so generic raw-narrative jobs never
    contradict a brief-backed verification."""
    out = set()
    for (sources_json,) in conn.execute("SELECT sources_json FROM work_brief WHERE state IN ('approved','held')"):
        out |= {s["issue_id"] for s in json.loads(sources_json) if s.get("issue_id")}
    return out


def _brief_verify_targets(cfg: Config, conn) -> list[dict]:
    """Sources of intent-ready briefs that need exact-version verification: an unverified source (no/non-valid
    association for this version) or one whose associated verdict went stale. Uses strategy.ready intent_ready, so
    held / source-drifted / superseded / dependency-unmet briefs are already excluded."""
    from . import strategy
    out = []
    for b in strategy.ready(cfg, conn):
        if not b.get("intent_ready"):
            continue
        verdict_by_issue = {v["issue_id"]: v for v in b.get("verdicts", [])}
        for s in b["sources"]:
            v = verdict_by_issue.get(s["issue_id"])
            if v is None or not v.get("valid") or _associated_stale(cfg, conn, s["issue_id"], v["verdict_id"]):
                out.append({"brief_id": b["id"], "identifier": s["identifier"], "issue_id": s["issue_id"],
                            "narrative": strategy.render(conn, b["id"])})
    return out


def gate(cfg: Config, conn) -> dict:
    """Hermes pre-check over owned tickets: auto-verdict unmapped ones, list the rest for the agent. A brief-backed
    source is verified from the compiled brief narrative (never raw Linear prose) and carries its exact brief_id; an
    unverified or stale-associated source forces brief verification even when the source already has a fresh generic
    verdict. Published brief sources never get a contradictory generic raw-narrative job."""
    batch = cfg.raw.get("prune", {}).get("batch", 2)
    todo, auto = [], 0
    brief_sources = _brief_source_issues(conn)
    rows = sorted(owned_in_scope(cfg, conn), key=lambda s: s["updated_at"], reverse=True)
    rows.sort(key=lambda s: json.loads(s["raw_json"])["priority"] or 5)  # stable: priority, then newest
    for s in rows:
        if s["issue_id"] in brief_sources:
            continue  # a published brief source is verified brief-backed, never raw
        ctx, why = map_context(cfg, s)
        reason = staleness(cfg, conn, s, ctx)
        if reason is None:
            continue
        if ctx is None:
            domain, labels, repo_lines = issue_fields(s)
            put(cfg, conn, s["identifier"], "needs-clarification", why,
                [{"type": "linear", "ref": s["identifier"],
                  "note": f"Domain={domain!r} labels={labels} Repo={repo_lines}"}], actor=MAP_ACTOR)
            auto += 1
        elif len(todo) < batch:
            trunk = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (ctx.repo,)).fetchone()
            raw = json.loads(s["raw_json"])
            v = conn.execute("SELECT evidence_paths_json FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                             (s["issue_id"],)).fetchone()
            todo.append({"identifier": s["identifier"], "title": raw["title"],
                         "why": reason, "context": ctx.name, "repo": ctx.repo,
                         "mirror": str(cfg.mirror_path(ctx.repo)), "trunk_sha": trunk["sha"] if trunk else None,
                         "witnesses": ctx.witnesses, **_recheck(cfg, conn, s, ctx, reason, trunk),
                         "learnings": learn.relevant(conn, {ctx.repo}, json.loads(v[0]) if v else (),
                                                     f"{raw['title']}\n{raw.get('description') or ''}")})
    for t in _brief_verify_targets(cfg, conn):  # exact-version brief verification (brief_id + compiled intent)
        if len(todo) >= batch:
            break
        s = conn.execute("SELECT * FROM linear_latest WHERE issue_id=?", (t["issue_id"],)).fetchone()
        if s is None:
            continue
        raw = json.loads(s["raw_json"])
        ctx, _ = map_context(cfg, s)
        if ctx is None:
            continue  # unmapped: handled by the generic path / needs-clarification
        trunk = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (ctx.repo,)).fetchone()
        todo.append({"identifier": t["identifier"], "title": raw["title"],
                     "why": f"needs verification for brief #{t['brief_id']}", "context": ctx.name, "repo": ctx.repo,
                     "mirror": str(cfg.mirror_path(ctx.repo)), "trunk_sha": trunk["sha"] if trunk else None,
                     "witnesses": ctx.witnesses, "brief_id": t["brief_id"], "brief": t["narrative"],
                     "learnings": learn.relevant(conn, {ctx.repo}, (),
                                                 f"{raw['title']}\n{t['narrative']}")})
    return {"wakeAgent": bool(todo), "context": {"tickets": todo, "auto_needs_clarification": auto}}


RECHECK_DIFF_CHARS = 8000


def _recheck(cfg: Config, conn, s, ctx: Context, reason: str, trunk) -> dict:
    """Only trunk moved (or the verdict aged): hand the agent the prior verdict and the diff of just the files it
    cited, so a confirm is a short read instead of a fresh investigation."""
    if reason not in ("evidence-changed", "aged") or trunk is None:
        return {}
    v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL", (s["issue_id"],)).fetchone()
    paths = json.loads(v["evidence_paths_json"])
    mirror = cfg.mirror_path(ctx.repo)
    if not paths or repos.changed_paths(mirror, v["trunk_sha"], trunk["sha"]) is None:
        return {}  # prior trunk unknown to the mirror: no diff to give, investigate from scratch
    diff = repos.git(mirror, "diff", v["trunk_sha"], trunk["sha"], "--", *paths) if v["trunk_sha"] != trunk["sha"] else ""
    return {"prior": {"kind": v["kind"], "target": v["target"], "reason": v["reason"],
                      "evidence": json.loads(v["evidence_json"]), "trunk_sha": v["trunk_sha"]},
            "cited_diff": (diff[:RECHECK_DIFF_CHARS] + "\n… (truncated)" if len(diff) > RECHECK_DIFF_CHARS else diff)
            or "(no change to the cited files)"}


def _check_evidence(cfg: Config, conn, ctx: Context | None, trunk_sha: str | None, ev: list) -> list[str]:
    if not isinstance(ev, list) or not ev:
        raise VerdictError("evidence must be a non-empty JSON list")
    paths = []
    for i, e in enumerate(ev):
        t = e.get("type") if isinstance(e, dict) else None
        where = f"evidence[{i}]"
        if t == "file":
            if ctx is None:
                raise VerdictError(f"{where}: file evidence needs a mapped repo")
            if e.get("sha", trunk_sha) != trunk_sha:
                raise VerdictError(f"{where}: file evidence must be at current trunk {trunk_sha}")
            if not repos.path_exists(cfg.mirror_path(ctx.repo), trunk_sha, e.get("path", "")):
                raise VerdictError(f"{where}: {e.get('path')!r} does not exist at {ctx.repo}@{trunk_sha[:12]}")
            e["sha"] = trunk_sha
            paths.append(e["path"])
        elif t in ("sql", "dagster"):
            log = conn.execute("SELECT * FROM witness_log WHERE id=?", (e.get("witness_log_id"),)).fetchone()
            if log is None or not log["ok"]:
                raise VerdictError(f"{where}: witness_log_id must cite a successful `factory witness` call")
            if (log["kind"] == "clickhouse") != (t == "sql"):
                raise VerdictError(f"{where}: type {t} does not match witness kind {log['kind']}")
            if ctx is None or log["witness"] not in ctx.witnesses:
                raise VerdictError(f"{where}: witness {log['witness']} is not mapped to this context")
            if t == "sql" and not witness.sql_statement_ok(log["query"]):
                raise VerdictError(f"{where}: sql evidence must be SELECT/SHOW/DESCRIBE")
            e.update(witness=log["witness"], query=log["query"], result_sha256=log["result_sha256"])
        elif t == "linear":
            if not conn.execute("SELECT 1 FROM linear_snapshot WHERE identifier=?", (e.get("ref"),)).fetchone():
                raise VerdictError(f"{where}: unknown Linear ref {e.get('ref')!r}")
        elif t == "pr":
            if not PR_URL.match(e.get("url", "")):
                raise VerdictError(f"{where}: pr evidence needs a GitHub pull request URL")
        else:
            raise VerdictError(f"{where}: type must be file|sql|dagster|linear|pr")
    return paths


def put(cfg: Config, conn, identifier: str, kind: str, reason: str, evidence: list,
        target: str | None = None, actor: str = "agent", brief_id: int | None = None) -> int:
    if kind not in KINDS:
        raise VerdictError(f"kind must be one of {', '.join(KINDS)}")
    if not reason.strip():
        raise VerdictError("reason is required")
    with db.tx(conn):
        s = latest(conn, identifier)
        if brief_id is not None:  # exact-version verification: the verdict is written for this brief version
            b = _approved_head_brief(conn, brief_id)
            src = next((x for x in b["sources"] if x.get("identifier") == identifier), None)
            if src is None:
                raise VerdictError(f"{identifier} is not a source of brief #{brief_id}")
            if src["issue_id"] != s["issue_id"]:
                raise VerdictError(f"{identifier}: brief #{brief_id} pins issue {src['issue_id']}, not {s['issue_id']}")
            if s["updated_at"] != src["snapshot_updated_at"]:
                raise VerdictError(f"{identifier} changed since brief #{brief_id} captured it; amend the brief")
        if not owned(cfg, conn, s):
            raise VerdictError(f"{identifier}: not the factory's concern (on linear.ignore, or its Domain project "
                               f"is not led by {cfg.linear['lead']})")
        ctx, why = map_context(cfg, s)
        if ctx is None and kind != "needs-clarification":
            raise VerdictError(f"{identifier}: {why}; only needs-clarification is allowed")
        if ctx is not None and not ctx.witnesses and kind not in NO_WITNESS_KINDS:
            raise VerdictError(f"context {ctx.name} has no witness; only valid or needs-clarification is allowed")
        trunk = None
        if ctx is not None:
            row = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (ctx.repo,)).fetchone()
            if row is None:
                raise VerdictError(f"mirror for {ctx.repo} not synced; run factory ingest")
            trunk = row["sha"]
        paths = _check_evidence(cfg, conn, ctx, trunk, evidence)
        types = {e["type"] for e in evidence}
        if kind == "valid" and "file" not in types:
            raise VerdictError("valid needs at least one file evidence at trunk")
        if kind == "already-done" and not types & {"file", "sql", "dagster"}:
            raise VerdictError("already-done needs file or witness evidence")
        if kind == "duplicate-of":
            if not target or target == identifier:
                raise VerdictError("duplicate-of needs --target <other identifier>")
            if not any(e["type"] == "linear" and e.get("ref") == target for e in evidence):
                raise VerdictError("duplicate-of needs linear evidence citing the target")
        if kind == "invalid-references" and not target:
            raise VerdictError("invalid-references needs --target <what is referenced but missing>")
        if kind not in ("duplicate-of", "invalid-references"):
            target = None
        now = db.now()
        conn.execute("UPDATE verdict SET superseded_at=? WHERE issue_id=? AND superseded_at IS NULL",
                     (now, s["issue_id"]))
        cur = conn.execute(
            "INSERT INTO verdict(issue_id, snapshot_updated_at, context, repo, trunk_sha, kind, target, reason, "
            "evidence_json, evidence_paths_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["issue_id"], s["updated_at"], ctx.name if ctx else None, ctx.repo if ctx else None, trunk,
             kind, target, reason.strip(), json.dumps(evidence), json.dumps(sorted(set(paths))), now, actor))
        if brief_id is not None:  # the exact version -> verdict association (never a generic borrowed verdict)
            # Re-verify upserts the same (version, source) key; only verdict_id is re-bound (schema key_frozen).
            conn.execute("INSERT INTO brief_verdict(brief_id, issue_id, verdict_id) VALUES (?,?,?) "
                         "ON CONFLICT(brief_id, issue_id) DO UPDATE SET verdict_id=excluded.verdict_id",
                         (brief_id, s["issue_id"], cur.lastrowid))
        learn.cite(conn, reason)
        return cur.lastrowid

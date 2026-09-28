"""Verdict freshness, the prune gate, and validated verdict writes."""
import json
import re

from . import db, repos, witness
from .config import Config, Context

KINDS = ("valid", "already-done", "stale", "duplicate-of", "invalid-references", "needs-clarification")
NO_WITNESS_KINDS = {"valid", "needs-clarification"}
PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
MAP_ACTOR = "factory:map"


class VerdictError(Exception):
    pass


_DOMAIN = re.compile(r"^\s*\**Domain\**:\**\s*(?:\[([^\]]+)\]|(.+?))(?:\(.*)?\s*$", re.M)
_REPO = re.compile(r"^\s*\**Repos?:?\**:?\s*(.+)$", re.M)


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


def staleness(cfg: Config, conn, snapshot, ctx: Context | None) -> str | None:
    """None when the current verdict still holds, else why it must be redone."""
    v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL",
                     (snapshot["issue_id"],)).fetchone()
    if v is None:
        return "new"
    if v["snapshot_updated_at"] != snapshot["updated_at"]:
        return "ticket-changed"
    if v["context"] != (ctx.name if ctx else None):
        return "context-changed"
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


def gate(cfg: Config, conn) -> dict:
    """Hermes pre-check: auto-verdict unmapped tickets, list the rest that need the agent."""
    batch = cfg.raw.get("prune", {}).get("batch", 10)
    todo, auto = [], 0
    rows = conn.execute(
        "SELECT * FROM linear_latest WHERE in_scope=1 ORDER BY "
        "CASE json_extract(raw_json,'$.priority') WHEN 0 THEN 5 ELSE json_extract(raw_json,'$.priority') END, "
        "updated_at DESC").fetchall()
    for s in rows:
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
            todo.append({"identifier": s["identifier"], "title": json.loads(s["raw_json"])["title"],
                         "why": reason, "context": ctx.name, "repo": ctx.repo,
                         "mirror": str(cfg.mirror_path(ctx.repo)), "trunk_sha": trunk["sha"] if trunk else None,
                         "witnesses": ctx.witnesses})
    return {"wakeAgent": bool(todo), "context": {"tickets": todo, "auto_needs_clarification": auto}}


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
        target: str | None = None, actor: str = "agent") -> int:
    if kind not in KINDS:
        raise VerdictError(f"kind must be one of {', '.join(KINDS)}")
    if not reason.strip():
        raise VerdictError("reason is required")
    with db.tx(conn):
        s = latest(conn, identifier)
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
        return cur.lastrowid

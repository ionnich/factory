"""Read-only DB/API witnesses. Every call is logged; evidence cites the log id."""
import base64
import hashlib
import json
import re
import urllib.parse
import urllib.request

from . import db
from .config import Config, ConfigError, secret

TIMEOUT_S = 5
ROW_CAP = 200
EXCERPT = 4000

_COMMENTS = re.compile(r"(--[^\n]*\n?)|(/\*.*?\*/)", re.S)
_SQL_ALLOWED = {"SELECT", "SHOW", "DESCRIBE", "DESC"}


class WitnessError(Exception):
    pass


def sql_statement_ok(query: str) -> bool:
    """Single SELECT/SHOW/DESCRIBE statement, no trailing statements, no INTO OUTFILE."""
    q = _COMMENTS.sub(" ", query).strip().rstrip(";").strip()
    if not q or ";" in q or re.search(r"\bINTO\s+OUTFILE\b", q, re.I):
        return False
    return q.split(None, 1)[0].upper() in _SQL_ALLOWED


def graphql_read_ok(query: str) -> bool:
    return not re.search(r"\b(mutation|subscription)\b", _COMMENTS.sub(" ", query), re.I)


def _cap(obj):
    if isinstance(obj, list):
        return [_cap(x) for x in obj[:ROW_CAP]]
    if isinstance(obj, dict):
        return {k: _cap(v) for k, v in obj.items()}
    return obj


def _clickhouse(cfg: Config, w: dict, query: str) -> tuple[list, int]:
    if not sql_statement_ok(query):
        raise WitnessError("only a single SELECT/SHOW/DESCRIBE statement is allowed")
    params = urllib.parse.urlencode({
        "readonly": 2, "max_execution_time": TIMEOUT_S, "max_result_rows": ROW_CAP,
        "result_overflow_mode": "break", "default_format": "JSON"})
    auth = base64.b64encode(f"{secret(cfg, w['user_env'])}:{secret(cfg, w['password_env'])}".encode()).decode()
    req = urllib.request.Request(f"{w['url'].rstrip('/')}/?{params}", data=query.encode(),
                                 headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S + 2) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:  # ClickHouse puts the reason (unknown table, syntax...) in the body
        raw = e.read().decode(errors="replace")
        try:
            raw = json.loads(raw).get("exception", raw)
        except ValueError:
            pass
        raise WitnessError(f"clickhouse HTTP {e.code}: {raw.strip()[:400]}") from None
    rows = body.get("data", [])[:ROW_CAP]
    return rows, len(rows)


def _dagster(cfg: Config, w: dict, query: str) -> tuple[dict, int]:
    if not query.strip().startswith(("{", "query")):
        raise WitnessError("dagster takes a GraphQL query (`{ ... }` or `query ...`), not SQL")
    if not graphql_read_ok(query):
        raise WitnessError("only GraphQL queries are allowed (no mutation/subscription)")
    req = urllib.request.Request(w["url"], data=json.dumps({"query": query}).encode(), headers={
        "Content-Type": "application/json", "Dagster-Cloud-Api-Token": secret(cfg, w["token_env"])})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:  # GraphQL validation errors come back as 400 with the reason in the body
        raw = e.read().decode(errors="replace")
        try:
            raw = json.loads(raw)["errors"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        raise WitnessError(f"dagster HTTP {e.code}: {raw.strip()[:400]}") from None
    if body.get("errors"):
        raise WitnessError(f"dagster: {body['errors'][0].get('message')}")
    data = _cap(body.get("data"))
    return data, sum(len(v) for v in _walk_lists(data))


def _walk_lists(obj):
    if isinstance(obj, list):
        yield obj
        for x in obj:
            yield from _walk_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_lists(v)


RUNNERS = {"clickhouse": _clickhouse, "dagster": _dagster}


def run(cfg: Config, conn, name: str, query: str) -> dict:
    w = cfg.witnesses.get(name)
    if w is None:
        raise ConfigError(f"unknown witness {name}")
    kind = w["kind"]
    try:
        result, rows = RUNNERS[kind](cfg, w, query)
        ok, err = 1, None
    except (WitnessError, OSError, ValueError) as e:
        result, rows, ok, err = None, None, 0, str(e)
    text = json.dumps(result, sort_keys=True, default=str)
    cur = conn.execute(
        "INSERT INTO witness_log(witness, kind, query, ok, rows, result_sha256, result_excerpt, at) VALUES (?,?,?,?,?,?,?,?)",
        (name, kind, query, ok, rows, hashlib.sha256(text.encode()).hexdigest() if ok else None,
         (text if ok else err)[:EXCERPT], db.now()))
    out = {"witness_log_id": cur.lastrowid, "ok": bool(ok)}
    out.update({"rows": rows, "result": result} if ok else {"error": err})
    return out

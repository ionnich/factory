"""Explicit Linear relationship snapshots: one complete graph per owned source.

The factory's concern is the canonical Domain-project sources it owns. This module
fetches, for each of those (non-archived) sources, the direct parent/children and the
explicit typed relations (blocks, duplicate, related) — never recursively. Each source's
complete graph is stored as a single row (absence = unknown, not empty) and read back by
snapshot() without any network. The existing immutable raw linear_snapshot is never
rewritten; linked endpoints never become snapshot rows.
"""
import hashlib
import json
import time
import urllib.error

from . import db, linear

BATCH = 10  # root issues aliased per GraphQL query
PAGE = 20   # keep ten aliased roots below Linear's query complexity cap.

# IssueRelation.type is a plain String!, not an enum. `similar` is a model-inferred
# cluster and is explicitly out of scope; any other unknown type fails loudly rather than
# claiming a complete graph while hiding links.
_RELATION_TYPES = ("blocks", "duplicate", "related")
_IGNORED_TYPES = {"similar"}

# Endpoint metadata: enough to describe a node, never enough to select a source.
ENDPOINT = "id identifier title url state { name type } assignee { email } project { id name }"


class RelationError(RuntimeError):
    """The relationship graph could not be completed; the previous cache must survive."""


def _edge_key(edge: dict) -> tuple:
    return (edge["kind"], edge["source"], edge["target"])


def _fingerprint(edges: list) -> str:
    # Semantic edges only: kind, direction, membership. Never observed_at or node metadata.
    return hashlib.sha256(
        json.dumps(edges, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _node(issue: dict) -> dict:
    """Node metadata from a Linear issue shape (raw snapshot or a relation endpoint)."""
    state = issue.get("state") or {}
    assignee = issue.get("assignee") or {}
    project = issue.get("project")
    return {
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "state": state.get("name"),
        "state_type": state.get("type"),
        "assignee": assignee.get("email"),
        "project": {"id": project.get("id"), "name": project.get("name")} if project else None,
    }


def snapshot(conn, identifier: str) -> dict:
    """The stored complete graph for `identifier`, or an explicitly incomplete result.

    A missing row means the source's links were never fetched (unknown), never an empty
    graph; a known-empty graph is a stored row with no edges. Edges/nodes order is
    deterministic from storage (edges by kind/source/target, nodes by identifier).
    """
    row = conn.execute("SELECT * FROM linear_relationship WHERE identifier=?", (identifier,)).fetchone()
    if row is None:
        cached = conn.execute("SELECT * FROM linear_latest WHERE identifier=?", (identifier,)).fetchone()
        nodes = [_node(json.loads(cached["raw_json"]))] if cached is not None else []
        return {"complete": False, "observed_at": None, "edges": [], "nodes": nodes, "fingerprint": None}
    return {"complete": True, "observed_at": row["observed_at"],
            "edges": json.loads(row["edges_json"]), "nodes": json.loads(row["nodes_json"]),
            "fingerprint": row["fingerprint"]}


_ALIAS_BODY = (
    "    ...Endpoint\n"
    "    parent { ...Endpoint }\n"
    "    children(first: %d, includeArchived: true) { nodes { ...Endpoint } pageInfo { hasNextPage endCursor } }\n"
    "    relations(first: %d) { nodes { type issue { ...Endpoint } relatedIssue { ...Endpoint } } "
    "pageInfo { hasNextPage endCursor } }\n"
    "    inverseRelations(first: %d) { nodes { type issue { ...Endpoint } relatedIssue { ...Endpoint } } "
    "pageInfo { hasNextPage endCursor } }\n"
    "  }"
) % (PAGE, PAGE, PAGE)


def _batch_query(count: int) -> str:
    args = ", ".join(f"$id{i}: String!" for i in range(count))
    body = "\n".join(f"  r{i}: issue(id: $id{i}) {{\n{_ALIAS_BODY}" for i in range(count))
    return f"query({args}) {{\n{body}\n}}\nfragment Endpoint on Issue {{ {ENDPOINT} }}"


def _page_query(conn_name: str) -> str:
    nodes = ("nodes { ...Endpoint }" if conn_name == "children"
             else "nodes { type issue { ...Endpoint } relatedIssue { ...Endpoint } }")
    archived = ", includeArchived: true" if conn_name == "children" else ""
    return ("query($id: String!, $after: String) {\n"
            "  issue(id: $id) { %s(first: %d%s, after: $after) { %s pageInfo { hasNextPage endCursor } }\n"
            "  }\n"
            "}\n"
            "fragment Endpoint on Issue { %s }" % (conn_name, PAGE, archived, nodes, ENDPOINT))


def _check_connection(conn_name: str, ident: str, conn):
    if not isinstance(conn, dict):
        raise RelationError(f"missing {conn_name} connection for {ident}")
    info = conn.get("pageInfo")
    if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
        raise RelationError(f"malformed pageInfo for {conn_name} on {ident}")
    nodes = conn.get("nodes")
    if not isinstance(nodes, list):
        raise RelationError(f"missing nodes for {conn_name} on {ident}")
    return info, nodes


def _process_nodes(conn_name: str, ident: str, nodes: list, edges: set, node_map: dict) -> None:
    for node in nodes:
        if conn_name == "children":
            if not isinstance(node, dict) or not node.get("identifier"):
                raise RelationError(f"malformed child of {ident}")
            node_map[node["identifier"]] = _node(node)
            edges.add(("parent", ident, node["identifier"]))
            continue
        kind = node.get("type") if isinstance(node, dict) else None
        if kind in _IGNORED_TYPES:
            continue
        if kind not in _RELATION_TYPES:
            raise RelationError(f"unknown Linear relation type {kind!r} on {ident}")
        src, dst = node.get("issue"), node.get("relatedIssue")
        if (not isinstance(src, dict) or not src.get("identifier")
                or not isinstance(dst, dict) or not dst.get("identifier")):
            raise RelationError(f"malformed relation on {ident}")
        node_map[src["identifier"]] = _node(src)
        node_map[dst["identifier"]] = _node(dst)
        if kind == "related":
            # Undirected: canonical sorted endpoints so direction never leaks into the graph.
            a, b = sorted((src["identifier"], dst["identifier"]))
            edges.add(("related", a, b))
        else:
            # blocks / duplicate: always source issue -> relatedIssue, never reversed.
            edges.add((kind, src["identifier"], dst["identifier"]))


def _collect(cfg, batch, graphs: dict) -> None:
    """Fetch first pages for `batch` and walk required follow-up pages, mutating `graphs`."""
    variables = {f"id{i}": s["issue_id"] for i, s in enumerate(batch)}
    data = linear.query(cfg, _batch_query(len(batch)), variables)
    if not isinstance(data, dict):
        raise RelationError("relationship fetch returned no data")
    for i, s in enumerate(batch):
        ident = s["identifier"]
        issue = data.get(f"r{i}")
        if not isinstance(issue, dict):
            raise RelationError(f"missing issue {ident} in relationship fetch")
        if issue.get("identifier") != ident:
            raise RelationError(f"root identifier mismatch for {ident}")
        edges, node_map = set(), {}
        if "parent" not in issue:
            raise RelationError(f"missing parent field for {ident}")
        parent = issue["parent"]
        if parent is not None:
            if not isinstance(parent, dict) or not parent.get("identifier"):
                raise RelationError(f"malformed parent of {ident}")
            node_map[parent["identifier"]] = _node(parent)
            edges.add(("parent", parent["identifier"], ident))
        pending = []
        for conn_name in ("children", "relations", "inverseRelations"):
            info, nodes = _check_connection(conn_name, ident, issue.get(conn_name))
            _process_nodes(conn_name, ident, nodes, edges, node_map)
            if info["hasNextPage"]:
                cursor = info.get("endCursor")
                if not isinstance(cursor, str) or not cursor:
                    raise RelationError(f"missing endCursor for {conn_name} on {ident}")
                pending.append((conn_name, cursor))
        for conn_name, after in pending:
            cursor = after
            seen = {cursor}
            while True:
                page = linear.query(cfg, _page_query(conn_name), {"id": s["issue_id"], "after": cursor})
                if not isinstance(page, dict) or not isinstance(page.get("issue"), dict):
                    raise RelationError(f"missing issue {ident} on {conn_name} page")
                info, nodes = _check_connection(conn_name, ident, page["issue"].get(conn_name))
                _process_nodes(conn_name, ident, nodes, edges, node_map)
                if not info["hasNextPage"]:
                    break
                nxt = info.get("endCursor")
                if not isinstance(nxt, str) or not nxt or nxt in seen:
                    raise RelationError(f"non-advancing cursor for {conn_name} on {ident}")
                seen.add(nxt)
                cursor = nxt
        node_map[ident] = _node(issue)  # self: metadata from the fresh root endpoint
        graphs[ident] = (edges, node_map)


def refresh(cfg, conn, budget_s: float = 45) -> dict:
    """Replace the per-source relationship cache for current owned active sources.

    Scope is exactly the canonical Domain-project sources prune.owned() accepts, regardless
    of state, minus archived ones. Ownership is proven from linear_project; an empty project
    cache means the project sync has not run, so refresh refuses rather than silently keeping
    a stale graph. Malformed graphs still raise and leave the previous cache intact. A
    transport timeout or exhausted `budget_s` skips the unfinished sources, upserts what
    completed, and never deletes a still-owned source that was not re-fetched this tick.
    """
    from . import prune

    if conn.in_transaction:
        raise RelationError("relationship refresh must run outside a write transaction")
    if not conn.execute("SELECT 1 FROM linear_project LIMIT 1").fetchone():
        raise RelationError("linear_project is empty; run factory ingest")

    roots = [s for s in conn.execute("SELECT * FROM linear_latest").fetchall()
             if json.loads(s["raw_json"]).get("archivedAt") is None and prune.owned(cfg, conn, s)]

    observed_at = db.now()
    graphs, skipped = {}, []
    deadline = time.monotonic() + max(0, budget_s)
    for start in range(0, len(roots), BATCH):
        batch = roots[start:start + BATCH]
        if time.monotonic() >= deadline:
            skipped.extend(s["identifier"] for s in roots[start:])
            break
        try:
            _collect(cfg, batch, graphs)
        except (TimeoutError, urllib.error.URLError):
            skipped.extend(s["identifier"] for s in batch)

    rows = []
    for s in roots:
        ident = s["identifier"]
        if ident not in graphs:
            if ident not in skipped:
                skipped.append(ident)
            continue
        edges, node_map = graphs[ident]
        sorted_edges = sorted(({"kind": k, "source": a, "target": b} for k, a, b in edges), key=_edge_key)
        sorted_nodes = sorted(node_map.values(), key=lambda n: n["identifier"])
        rows.append((ident, observed_at, json.dumps(sorted_edges, sort_keys=True),
                     json.dumps(sorted_nodes, sort_keys=True), _fingerprint(sorted_edges)))

    owned = tuple(s["identifier"] for s in roots)
    with db.tx(conn):
        if owned:
            removed = conn.execute(
                f"DELETE FROM linear_relationship WHERE identifier NOT IN ({','.join('?' * len(owned))})",
                owned).rowcount
        else:
            removed = conn.execute("DELETE FROM linear_relationship").rowcount
        conn.executemany(
            "INSERT INTO linear_relationship(identifier, observed_at, edges_json, nodes_json, fingerprint) "
            "VALUES (?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET "
            "observed_at=excluded.observed_at, edges_json=excluded.edges_json, "
            "nodes_json=excluded.nodes_json, fingerprint=excluded.fingerprint",
            rows)
    return {"sources": len(roots), "replaced": len(rows), "removed": removed, "skipped": skipped}

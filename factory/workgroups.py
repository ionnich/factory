"""Deterministic ticket workgroups from recorded relationships.

Pure: no DB, no network, no model. `build` consumes the Strategy source list (tickets) and the data slice's
relationship snapshots keyed by identifier, then returns bounded, stable groups for the Dashboard. Grouping is
deterministic and independent of ticket input order, priority or filters.

Group precedence: parent connected families, then dependency (blocks) connected chains, then ONE-HOP related seeds
with direct neighbors (never transitive related components), then buckets by project name, then context, then
Unmapped. Parent/dependency components carry external linked nodes as read-only context/bridges, but only source
identifiers are ever members. Cycle detection is per edge type (parent and blocks are never mixed).
"""
from __future__ import annotations

import heapq

MAX_MEMBERS = 12
EDGE_KINDS = ("parent", "blocks", "related", "duplicate")

_EMPTY_NODE = {"title": "", "url": "", "state": None, "state_type": None, "assignee": None, "project": None}


def build(tickets, snapshots):
    """Every source appears once in `members`; linked nodes outside each chunk remain read-only `context`."""
    snapshots = snapshots or {}
    by_id = {t["identifier"]: t for t in (tickets or []) if t.get("identifier")}
    source_ids = set(by_id)

    nodes, edges = {}, set()
    for ident in sorted(source_ids):
        snap = snapshots.get(ident) or {}
        for n in snap.get("nodes") or []:
            nid = n.get("identifier")
            if nid:
                nodes.setdefault(nid, n)
        for e in snap.get("edges") or []:
            kind, src, tgt = e.get("kind"), e.get("source"), e.get("target")
            if kind not in EDGE_KINDS or not src or not tgt:
                continue
            if kind == "related" and src > tgt:  # canonical undirected
                src, tgt = tgt, src
            edges.add((kind, src, tgt))
    edges = sorted(edges)
    for identifier, ticket in by_id.items():
        nodes[identifier] = {key: ticket.get(key) for key in ("identifier", *_EMPTY_NODE)}

    all_ids = set(nodes) | {s for (_, s, _) in edges} | {t for (_, _, t) in edges}
    external_ids = all_ids - source_ids

    title_of = {i: (t.get("title") or i) for i, t in by_id.items()}
    for i, n in nodes.items():
        title_of.setdefault(i, n.get("title") or i)

    groups = []
    remaining = set(source_ids)

    parent_adj = _forming_adj("parent", edges)
    for members, comp in _family_groups("parent", source_ids, external_ids, edges):
        ordered = _ordered("parent", members, comp, edges)
        _emit(groups, "parent", ordered, set(comp), parent_adj, _parent_title(comp, title_of, edges),
              edges, nodes, source_ids)
        remaining -= set(ordered)

    blocks_adj = _forming_adj("blocks", edges)
    for members, comp in _family_groups("blocks", remaining, all_ids - remaining, edges):
        ordered = _ordered("blocks", members, comp, edges)
        _emit(groups, "dependency", ordered, set(comp), blocks_adj, title_of[min(ordered)],
              edges, nodes, source_ids)
        remaining -= set(ordered)

    related_adj = _forming_adj("related", edges)
    for members, comp in _related_groups(remaining, edges):
        _emit(groups, "related", members, comp, related_adj, title_of[members[0]], edges, nodes, source_ids)
        remaining -= set(members)

    for members, comp, kind, base in _buckets(remaining, by_id):
        _emit(groups, kind, members, comp, {}, base, edges, nodes, source_ids)
    # Detect each directed edge type independently, even inside groups formed by another type.
    cyclic = set()
    for edge_kind in ("parent", "blocks"):
        adj = {}
        for kind, source, target in edges:
            if kind == edge_kind:
                adj.setdefault(source, set()).add(target)
        for component in _sccs(all_ids, adj):
            if len(component) > 1 or component[0] in adj.get(component[0], ()):
                cyclic.update(component)
    for group in groups:
        visible = set(group["members"]) | {node["identifier"] for node in group["context"]}
        group["cycles"] = sorted(cyclic & visible)

    return groups


def _forming_adj(kind, edges):
    adj = {}
    for k, s, t in edges:
        if k == kind:
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)
    return adj


def _components(pairs, allowed):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    involved = set()
    for a, b in pairs:
        if a in allowed and b in allowed:
            involved.add(a)
            involved.add(b)
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    comps = {}
    for x in involved:
        comps.setdefault(find(x), []).append(x)
    return list(comps.values())


def _family_groups(kind, allowed_sources, external_ids, edges):
    """Connected components over `kind` edges among allowed sources, with external nodes as bridges. Sorted by seed."""
    pairs = [(s, t) for (k, s, t) in edges if k == kind]
    allowed = set(allowed_sources) | external_ids
    out = []
    for comp in _components(pairs, allowed):
        members = sorted(i for i in comp if i in allowed_sources)
        if members:
            out.append((members, comp))
    out.sort(key=lambda mc: mc[0][0])
    return out


def _sccs(nodes, adj):
    """Iterative Kosaraju traversal: long real dependency chains must not hit Python's recursion limit."""
    visited, finished = set(), []
    for root in sorted(nodes):
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(sorted(adj.get(root, ()))))]
        while stack:
            node, neighbors = stack[-1]
            neighbor = next(neighbors, None)
            if neighbor is None:
                finished.append(node)
                stack.pop()
            elif neighbor not in visited:
                visited.add(neighbor)
                stack.append((neighbor, iter(sorted(adj.get(neighbor, ())))))
    reverse = {}
    for source, targets in adj.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    visited, components = set(), []
    for root in reversed(finished):
        if root in visited:
            continue
        visited.add(root)
        component, stack = [], [root]
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in reverse.get(node, ()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


def _ordered(edge_kind, members, comp, edges):
    """Parent/prerequisite order, with deterministic order inside genuine strongly connected components."""
    comp_set = set(comp)
    directed = [(s, t) for (k, s, t) in edges if k == edge_kind and s in comp_set and t in comp_set]
    adj = {}
    for s, t in directed:
        adj.setdefault(s, []).append(t)
    sccs = _sccs(comp_set, adj)
    comp_of = {}
    for i, c in enumerate(sccs):
        for v in c:
            comp_of[v] = i
    n = len(sccs)
    cond = [set() for _ in range(n)]
    indeg = [0] * n
    for s, t in directed:
        a, b = comp_of[s], comp_of[t]
        if a != b and b not in cond[a]:
            cond[a].add(b)
            indeg[b] += 1
    key = [min(c) for c in sccs]
    heap = [(key[i], i) for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        _, i = heapq.heappop(heap)
        order.append(i)
        for b in sorted(cond[i]):
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(heap, (key[b], b))
    ordered_nodes = []
    for i in order:
        ordered_nodes.extend(sorted(sccs[i]))
    member_set = set(members)
    return [v for v in ordered_nodes if v in member_set]


def _parent_title(comp, title_of, edges):
    """The actual topmost parent's title (lexically smallest root); the seed title when the family is a pure cycle."""
    comp_set = set(comp)
    incoming = {t for (k, s, t) in edges if k == "parent" and s in comp_set and t in comp_set}
    roots = sorted(i for i in comp if i not in incoming)
    pick = roots[0] if roots else min(comp)
    return title_of.get(pick, pick)


def _related_groups(remaining, edges):
    """ONE-HOP related groups: a lexical seed plus its direct related neighbors, never a transitive component."""
    adj = {}
    for k, s, t in edges:
        if k == "related":
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)
    rem = set(remaining)
    out = []
    while True:
        seeds = [m for m in rem if adj.get(m)]
        if not seeds:
            break
        seed = min(seeds)
        neighbors = adj[seed]
        members = sorted({seed} | {n for n in neighbors if n in rem})
        out.append((members, {seed} | neighbors))
        rem -= set(members)
    return out


def _buckets(remaining, by_id):
    """Remaining tickets bucketed by project name, then context, then Unmapped."""
    rem = sorted(remaining)
    out = []
    projects = {}
    for m in rem:
        p = (by_id[m].get("project") or {}).get("name")
        if p:
            projects.setdefault(p, []).append(m)
    for p in sorted(projects):
        out.append((sorted(projects[p]), None, "project", f"Project bucket: {p}"))
    contexts = {}
    for m in rem:
        if (by_id[m].get("project") or {}).get("name"):
            continue
        contexts.setdefault(by_id[m].get("context") or None, []).append(m)
    for c in sorted(k for k in contexts if k is not None):
        out.append((sorted(contexts[c]), None, "context", f"Context bucket: {c}"))
    if None in contexts:
        out.append((sorted(contexts[None]), None, "context", "Context bucket: Unmapped"))
    return out


def _context(members, comp, forming_adj, source_ids, edges):
    """All linked endpoints outside this chunk are context, never additional source authority."""
    ctx = set()
    for k, s, t in edges:
        if s in members and t not in members:
            ctx.add(t)
        elif t in members and s not in members:
            ctx.add(s)
    if comp:
        seen = set(members)
        stack = list(members)
        while stack:
            x = stack.pop()
            for y in forming_adj.get(x, ()):
                if y in comp and y not in seen:
                    seen.add(y)
                    if y not in source_ids:
                        ctx.add(y)
                    stack.append(y)
    return ctx


def _node(ident, nodes):
    n = nodes.get(ident)
    return n if n is not None else {"identifier": ident, **_EMPTY_NODE}


def _emit(groups, kind, ordered, comp, forming_adj, base_title, edges, nodes, source_ids):
    seed = min(ordered)
    for idx in range(0, len(ordered), MAX_MEMBERS):
        chunk = ordered[idx:idx + MAX_MEMBERS]
        ordinal = idx // MAX_MEMBERS + 1
        continued = ordinal > 1
        chunk_set = set(chunk)
        groups.append({
            "id": f"{kind}:{seed}:{ordinal}",
            "kind": kind,
            "title": base_title if ordinal == 1 else f"{base_title} (continued {ordinal})",
            "members": chunk,
            "edges": [{"kind": k, "source": s, "target": t} for (k, s, t) in edges
                      if s in chunk_set or t in chunk_set],
            "context": [_node(i, nodes) for i in sorted(_context(chunk_set, comp, forming_adj, source_ids, edges))],
            "continued": continued,
        })

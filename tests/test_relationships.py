"""Relationship ingestion: typed edges per owned source, complete per-source snapshots.

The fake gql serves Linear's relationship batch/page queries and the ingest issues fetch,
so refresh/ingest run without a network or live credentials.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, linear, relationships

EMPTY = {"parent": None, "children": [], "relations": [], "inverseRelations": []}


def endpoint(ident, iid=None, title=None, state="Todo", state_type="unstarted", assignee=None, project=None):
    return {"id": iid or f"id-{ident}", "identifier": ident,
            "title": title or f"Title {ident}", "url": f"https://linear.app/{ident}",
            "state": {"name": state, "type": state_type},
            "assignee": {"email": assignee} if assignee else None,
            "project": project}


def raw_snapshot(ident, iid=None, domain="Owned", state="Todo", state_type="unstarted",
                 assignee=None, archived=None):
    d = {"id": iid or f"id-{ident}", "identifier": ident, "title": f"Title {ident}",
         "url": f"https://linear.app/{ident}", "state": {"name": state, "type": state_type},
         "team": {"key": "FIN", "id": "team"},
         "assignee": {"email": assignee} if assignee else None,
         "project": None, "labels": {"nodes": []}, "description": f"Domain: {domain}"}
    if archived is not None:
        d["archivedAt"] = archived
    return d


def insert_snapshot(conn, raw, updated="2026-09-01T00:00:00.000Z"):
    conn.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,?,?)",
                 (raw["id"], raw["identifier"], updated, updated, raw["state"]["type"], 1,
                  json.dumps(raw, sort_keys=True)))


def insert_project(conn, pid="pid", name="Owned", lead="lead@x"):
    conn.execute("INSERT INTO linear_project VALUES (?,?,?,?,?)",
                 (pid, pid, name, lead, "2026-09-01T00:00:00.000Z"))


class FakeLinear:
    """Fake linear.gql: relationship batch/page queries plus the ingest issues fetch."""

    def __init__(self, rels=None, issues=None, on_page=None, on_batch=None, conn=None):
        self.rels = rels or {}      # issue_id -> {"parent", "children", "relations", "inverseRelations"}
        self.issues = issues or []  # served verbatim for the ingest ISSUES_QUERY
        self.on_page = on_page      # optional (root_id, conn_name, after) -> response
        self.on_batch = on_batch    # optional (variables) -> response
        self.conn = conn            # cached source rows, for root id -> identifier lookup
        self.requested = []         # root issue_ids the batch queries asked for

    def gql(self, cfg, query, variables):
        if "r0: issue" in query:
            if self.on_batch:
                return self.on_batch(variables)
            return self._batch(variables)
        if "issue(id: $id)" in query:
            conn_name = ("children" if "children(" in query else
                         "relations" if "relations(" in query else "inverseRelations")
            if self.on_page:
                return self.on_page(variables["id"], conn_name, variables.get("after"))
            return self._page(conn_name, variables)
        if "issues(filter:" in query:
            return {"issues": {"nodes": list(self.issues),
                               "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        raise AssertionError("unexpected query: %r" % query)

    def _batch(self, variables):
        data = {}
        ids = sorted((k for k in variables if k.startswith("id")), key=lambda k: int(k[2:]))
        for i, key in enumerate(ids):
            data[f"r{i}"] = self._root(variables[key])
        self.requested.extend(variables[key] for key in ids)
        return data

    def _root(self, issue_id):
        model = self.rels.get(issue_id)
        if model is None:
            return None
        row = self.conn.execute(
            "SELECT identifier FROM linear_latest WHERE issue_id=?", (issue_id,)).fetchone()
        base = endpoint(row[0], iid=issue_id)
        base["parent"] = model.get("parent")
        base["children"] = self._slice(model["children"])
        base["relations"] = self._slice(model["relations"])
        base["inverseRelations"] = self._slice(model["inverseRelations"])
        return base

    def _slice(self, items):
        return {"nodes": items[:relationships.PAGE],
                "pageInfo": {"hasNextPage": len(items) > relationships.PAGE, "endCursor": "c1"}}

    def _page(self, conn_name, variables):
        items = self.rels[variables["id"]][conn_name]
        n = int(variables.get("after")[1:])
        start = n * relationships.PAGE
        chunk = items[start:start + relationships.PAGE]
        return {"issue": {conn_name: {"nodes": chunk,
                                      "pageInfo": {"hasNextPage": start + relationships.PAGE < len(items),
                                                   "endCursor": f"c{n + 1}"}}}}


class Relationships(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(self.conn.close)
        self.cfg = SimpleNamespace(linear={"lead": "lead@x", "teams": ["FIN"], "team": {}, "ignore": []})

    def refresh(self, rels, **kwargs):
        fake = FakeLinear(rels=rels, conn=self.conn, **kwargs)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            relationships.refresh(self.cfg, self.conn)
        return fake

    def test_four_typed_edges_with_direction_dedupe_and_duplicate_direction(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": {
            "parent": endpoint("FIN-0", iid="ip"),
            "children": [endpoint("FIN-2", iid="ic1")],
            "relations": [
                {"type": "blocks", "issue": a, "relatedIssue": endpoint("FIN-10", iid="ix")},
                {"type": "related", "issue": a, "relatedIssue": endpoint("FIN-11", iid="iy")},
                {"type": "duplicate", "issue": a, "relatedIssue": endpoint("FIN-12", iid="iz")},
            ],
            "inverseRelations": [
                {"type": "blocks", "issue": endpoint("FIN-13", iid="iw"), "relatedIssue": a},
                {"type": "related", "issue": endpoint("FIN-11", iid="iy"), "relatedIssue": a},
                {"type": "duplicate", "issue": endpoint("FIN-14", iid="iv"), "relatedIssue": a},
            ],
        }}
        self.refresh(rels)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["edges"], [
            {"kind": "blocks", "source": "FIN-1", "target": "FIN-10"},
            {"kind": "blocks", "source": "FIN-13", "target": "FIN-1"},
            {"kind": "duplicate", "source": "FIN-1", "target": "FIN-12"},
            {"kind": "duplicate", "source": "FIN-14", "target": "FIN-1"},
            {"kind": "parent", "source": "FIN-0", "target": "FIN-1"},
            {"kind": "parent", "source": "FIN-1", "target": "FIN-2"},
            {"kind": "related", "source": "FIN-1", "target": "FIN-11"},
        ])
        self.assertEqual([n["identifier"] for n in snap["nodes"]],
                         ["FIN-0", "FIN-1", "FIN-10", "FIN-11", "FIN-12", "FIN-13", "FIN-14", "FIN-2"])

    def test_similar_ignored_and_unknown_type_fails(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": dict(EMPTY, relations=[{"type": "similar", "issue": a,
                                              "relatedIssue": endpoint("FIN-2", iid="ib")}])}
        self.refresh(rels)
        self.assertEqual(relationships.snapshot(self.conn, "FIN-1")["edges"], [])
        rels["ia"]["relations"] = [{"type": "foobar", "issue": a, "relatedIssue": endpoint("FIN-2", iid="ib")}]
        with self.assertRaises(relationships.RelationError):
            self.refresh(rels)

    def test_pagination_across_all_connections_and_root_batches(self):
        insert_project(self.conn)
        n_roots, n_items = 11, 55  # 11 roots cross one batch boundary; 55 items span three pages per connection
        rels = {}
        for r in range(n_roots):
            ident, iid = f"FIN-{r}", f"ir{r}"
            insert_snapshot(self.conn, raw_snapshot(ident, iid=iid))
            a = endpoint(ident, iid=iid)
            rels[iid] = {
                "parent": None,
                "children": [endpoint(f"FIN-{r}-c{j}", iid=f"{iid}-c{j}") for j in range(n_items)],
                "relations": [{"type": "blocks", "issue": a,
                               "relatedIssue": endpoint(f"FIN-{r}-b{j}", iid=f"{iid}-b{j}")}
                              for j in range(n_items)],
                "inverseRelations": [{"type": "duplicate",
                                      "issue": endpoint(f"FIN-{r}-d{j}", iid=f"{iid}-d{j}"), "relatedIssue": a}
                                     for j in range(n_items)],
            }
        fake = FakeLinear(rels=rels, conn=self.conn)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            res = relationships.refresh(self.cfg, self.conn)
        self.assertEqual(res["sources"], n_roots)
        self.assertEqual(res["replaced"], n_roots)
        for r in range(n_roots):
            snap = relationships.snapshot(self.conn, f"FIN-{r}")
            self.assertTrue(snap["complete"])
            self.assertEqual(sum(1 for e in snap["edges"] if e["kind"] == "parent"), n_items)
            self.assertEqual(sum(1 for e in snap["edges"] if e["kind"] == "blocks"), n_items)
            self.assertEqual(sum(1 for e in snap["edges"] if e["kind"] == "duplicate"), n_items)

    def test_outside_linked_endpoint_metadata_without_recursion(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a,
                                              "relatedIssue": endpoint("FIN-99", iid="ib", title="Outside ticket",
                                                                       assignee="someone@x")}])}
        fake = self.refresh(rels)
        snap = relationships.snapshot(self.conn, "FIN-1")
        bnode = next(n for n in snap["nodes"] if n["identifier"] == "FIN-99")
        self.assertEqual(bnode["title"], "Outside ticket")
        self.assertEqual(bnode["assignee"], "someone@x")
        # never appended to the snapshot universe, never crawled as a root
        self.assertIsNone(self.conn.execute("SELECT 1 FROM linear_snapshot WHERE identifier='FIN-99'").fetchone())
        self.assertFalse(relationships.snapshot(self.conn, "FIN-99")["complete"])
        self.assertEqual(fake.requested, ["ia"])

    def test_link_deletion_with_unchanged_updatedAt_surfaces_via_ingest(self):
        insert_project(self.conn)
        a = endpoint("FIN-1", iid="ia")
        issue_a = {"id": "ia", "identifier": "FIN-1", "updatedAt": "2026-09-01T00:00:00.000Z",
                   "state": {"name": "Todo", "type": "unstarted"}, "team": {"key": "FIN"},
                   "title": "FIN-1", "url": "https://linear/FIN-1", "dueDate": "2026-10-15",
                   "labels": {"nodes": []}, "description": "Domain: Owned", "assignee": None, "project": None}
        rels = {"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a,
                                              "relatedIssue": endpoint("FIN-2", iid="ib")}])}
        fake = FakeLinear(rels=rels, issues=[issue_a], conn=self.conn)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            res = linear.ingest(self.cfg, self.conn)
        self.assertEqual(res["relationships"], {"sources": 1, "replaced": 1, "removed": 0})
        raw = json.dumps(issue_a, sort_keys=True)
        self.assertEqual(self.conn.execute("SELECT raw_json FROM linear_snapshot WHERE issue_id='ia'").fetchone()[0], raw)
        self.assertEqual(relationships.snapshot(self.conn, "FIN-1")["edges"],
                         [{"kind": "blocks", "source": "FIN-1", "target": "FIN-2"}])
        fp_before = relationships.snapshot(self.conn, "FIN-1")["fingerprint"]
        # the link is deleted; issue updatedAt is identical, so the raw snapshot would be skipped by
        # updatedAt, but refresh still replaces the graph.
        rels["ia"]["relations"] = []
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            linear.ingest(self.cfg, self.conn)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["edges"], [])
        self.assertNotEqual(snap["fingerprint"], fp_before)
        # raw snapshot still byte-exact and single (unchanged updatedAt -> no rewrite)
        self.assertEqual(self.conn.execute("SELECT raw_json FROM linear_snapshot WHERE issue_id='ia'").fetchone()[0], raw)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM linear_snapshot WHERE issue_id='ia'").fetchone()[0], 1)

    def test_ingest_surfaces_relationship_refresh_error_and_keeps_sources(self):
        insert_project(self.conn)
        issue_a = {"id": "ia", "identifier": "FIN-1", "updatedAt": "2026-09-01T00:00:00.000Z",
                   "state": {"name": "Todo", "type": "unstarted"}, "team": {"key": "FIN"},
                   "title": "FIN-1", "url": "https://linear/FIN-1", "dueDate": None,
                   "labels": {"nodes": []}, "description": "Domain: Owned", "assignee": None, "project": None}
        raw = json.dumps(issue_a, sort_keys=True)
        fake = FakeLinear(issues=[issue_a], conn=self.conn)  # no relationship model -> the owned root is missing
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            with self.assertRaises(relationships.RelationError):
                linear.ingest(self.cfg, self.conn)
        # the source tx already committed; only the relationship refresh failed
        self.assertEqual(self.conn.execute("SELECT raw_json FROM linear_snapshot WHERE issue_id='ia'").fetchone()[0], raw)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM linear_snapshot WHERE issue_id='ia'").fetchone()[0], 1)
        # a subsequent ingest retries regardless of unchanged updatedAt and completes the graph
        fake.rels["ia"] = dict(EMPTY)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            res = linear.ingest(self.cfg, self.conn)
        self.assertEqual(res["relationships"], {"sources": 1, "replaced": 1, "removed": 0})
        self.assertTrue(relationships.snapshot(self.conn, "FIN-1")["complete"])

    def test_fingerprint_stable_for_descriptive_changes(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        b = endpoint("FIN-2", iid="ib", title="v1")
        rels = {"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a, "relatedIssue": b}])}
        self.refresh(rels)
        f1 = relationships.snapshot(self.conn, "FIN-1")["fingerprint"]
        rels["ia"]["relations"][0]["relatedIssue"]["title"] = "v2"  # descriptive-only change
        self.refresh(rels)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["fingerprint"], f1)
        self.assertEqual(next(n for n in snap["nodes"] if n["identifier"] == "FIN-2")["title"], "v2")

    def test_fingerprint_drifts_on_kind_direction_and_member_changes(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        b = endpoint("FIN-2", iid="ib")
        c = endpoint("FIN-3", iid="ic")

        def run():
            self.refresh(rels)
            return relationships.snapshot(self.conn, "FIN-1")["fingerprint"]

        rels = {"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a, "relatedIssue": b}])}
        f_blocks = run()
        rels["ia"]["relations"] = [{"type": "related", "issue": a, "relatedIssue": b}]  # kind change
        f_related = run()
        self.assertNotEqual(f_blocks, f_related)
        rels["ia"]["relations"] = []
        rels["ia"]["inverseRelations"] = [{"type": "blocks", "issue": b, "relatedIssue": a}]  # direction change
        f_dir = run()
        self.assertNotEqual(f_dir, f_blocks)
        rels["ia"]["inverseRelations"] = []
        rels["ia"]["relations"] = [{"type": "blocks", "issue": a, "relatedIssue": c}]  # member change
        f_member = run()
        self.assertNotEqual(f_member, f_blocks)

    def test_missing_vs_known_empty(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        rels = {"ia": dict(EMPTY)}
        self.refresh(rels)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["edges"], [])
        self.assertIsNotNone(snap["fingerprint"])
        self.assertIsNotNone(snap["observed_at"])
        self.assertEqual([n["identifier"] for n in snap["nodes"]], ["FIN-1"])
        fp = snap["fingerprint"]
        self.refresh(rels)  # known-empty stays complete with a stable fingerprint
        self.assertEqual(relationships.snapshot(self.conn, "FIN-1")["fingerprint"], fp)
        # missing row -> explicitly incomplete, self node from cached source metadata
        insert_snapshot(self.conn, raw_snapshot("FIN-2", iid="ib"))
        miss = relationships.snapshot(self.conn, "FIN-2")
        self.assertFalse(miss["complete"])
        self.assertIsNone(miss["fingerprint"])
        self.assertIsNone(miss["observed_at"])
        self.assertEqual(miss["edges"], [])
        self.assertEqual([n["identifier"] for n in miss["nodes"]], ["FIN-2"])
        self.assertEqual(relationships.snapshot(self.conn, "FIN-3")["nodes"], [])

    def _old_graph(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a,
                                              "relatedIssue": endpoint("FIN-2", iid="ib")}])}
        self.refresh(rels)
        return relationships.snapshot(self.conn, "FIN-1")

    def test_late_page_failure_keeps_old_graph(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        insert_snapshot(self.conn, raw_snapshot("FIN-2", iid="ib"))
        a = endpoint("FIN-1", iid="ia")
        b = endpoint("FIN-2", iid="ib")
        self.refresh({
            "ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a,
                                          "relatedIssue": endpoint("FIN-9", iid="ix")}]),
            "ib": dict(EMPTY, relations=[{"type": "blocks", "issue": b,
                                          "relatedIssue": endpoint("FIN-8", iid="iy")}]),
        })
        old1 = relationships.snapshot(self.conn, "FIN-1")
        old2 = relationships.snapshot(self.conn, "FIN-2")
        rels = {
            "ia": {"parent": None, "children": [],
                   "relations": [{"type": "blocks", "issue": a,
                                  "relatedIssue": endpoint(f"FIN-{j}", iid=f"ib{j}")} for j in range(55)],
                   "inverseRelations": []},
            "ib": {"parent": None, "children": [],
                   "relations": [{"type": "blocks", "issue": b,
                                  "relatedIssue": endpoint("FIN-8", iid="iy")}],
                   "inverseRelations": []},
        }
        def on_page(root_id, conn_name, after):
            raise RuntimeError("network down on page 2")
        with self.assertRaises(RuntimeError):
            self.refresh(rels, on_page=on_page)
        for ident, old in (("FIN-1", old1), ("FIN-2", old2)):
            snap = relationships.snapshot(self.conn, ident)
            self.assertEqual(snap["edges"], old["edges"])
            self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_nonadvancing_cursor_keeps_old_graph(self):
        old = self._old_graph()
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": {"parent": None, "children": [],
                       "relations": [{"type": "blocks", "issue": a,
                                      "relatedIssue": endpoint(f"FIN-{j}", iid=f"ib{j}")} for j in range(55)],
                       "inverseRelations": []}}
        def on_page(root_id, conn_name, after):
            return {"issue": {conn_name: {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": after}}}}
        with self.assertRaises(relationships.RelationError):
            self.refresh(rels, on_page=on_page)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["edges"], old["edges"])
        self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_missing_root_issue_keeps_old_graph(self):
        old = self._old_graph()
        with self.assertRaises(relationships.RelationError):
            self.refresh({})  # the owned root is gone from Linear
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["edges"], old["edges"])
        self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_cursor_cycle_keeps_old_graph(self):
        old = self._old_graph()
        a = endpoint("FIN-1", iid="ia")
        rels = {"ia": {"parent": None, "children": [],
                       "relations": [{"type": "blocks", "issue": a,
                                      "relatedIssue": endpoint(f"FIN-{j}", iid=f"ib{j}")} for j in range(55)],
                       "inverseRelations": []}}
        def on_page(root_id, conn_name, after):
            return {"issue": {conn_name: {"nodes": [],
                                          "pageInfo": {"hasNextPage": True,
                                                       "endCursor": "c2" if after == "c1" else "c1"}}}}
        with self.assertRaises(relationships.RelationError):
            self.refresh(rels, on_page=on_page)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["edges"], old["edges"])
        self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_missing_parent_shape_keeps_old_graph(self):
        old = self._old_graph()
        def on_batch(variables):
            empty = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"r0": {"identifier": "FIN-1", "children": empty,
                           "relations": empty, "inverseRelations": empty}}  # no "parent" key
        with self.assertRaises(relationships.RelationError):
            self.refresh({}, on_batch=on_batch)
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["edges"], old["edges"])
        self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_scopes_owned_sources_regardless_state_and_assignee(self):
        insert_project(self.conn, "pid-own", "Owned", "lead@x")
        insert_project(self.conn, "pid-foreign", "Foreign", "other@x")
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="i1", state="Done", state_type="completed"))
        insert_snapshot(self.conn, raw_snapshot("FIN-2", iid="i2", state="Backlog", state_type="backlog"))
        insert_snapshot(self.conn, raw_snapshot("FIN-3", iid="i3", assignee="foreign@x"))
        insert_snapshot(self.conn, raw_snapshot("FIN-4", iid="i4", domain="Foreign"))
        insert_snapshot(self.conn, raw_snapshot("FIN-5", iid="i5", archived="2026-09-01T00:00:00.000Z"))
        rels = {iid: dict(EMPTY) for iid in ("i1", "i2", "i3")}
        fake = FakeLinear(rels=rels, conn=self.conn)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            res = relationships.refresh(self.cfg, self.conn)
        self.assertEqual(res["sources"], 3)
        rows = {r[0] for r in self.conn.execute("SELECT identifier FROM linear_relationship")}
        self.assertEqual(rows, {"FIN-1", "FIN-2", "FIN-3"})

    def test_removes_rows_no_longer_in_owned_scope(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="i1"))
        insert_snapshot(self.conn, raw_snapshot("FIN-2", iid="i2"))
        rels = {"i1": dict(EMPTY), "i2": dict(EMPTY)}
        fake = FakeLinear(rels=rels, conn=self.conn)
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            relationships.refresh(self.cfg, self.conn)
        rows = {r[0] for r in self.conn.execute("SELECT identifier FROM linear_relationship")}
        self.assertEqual(rows, {"FIN-1", "FIN-2"})
        # FIN-2 archived in a newer snapshot version -> no longer owned active
        insert_snapshot(self.conn, raw_snapshot("FIN-2", iid="i2", archived="2026-09-02T00:00:00.000Z"),
                        updated="2026-09-02T00:00:00.000Z")
        with mock.patch("factory.linear.gql", side_effect=fake.gql):
            relationships.refresh(self.cfg, self.conn)
        rows = {r[0] for r in self.conn.execute("SELECT identifier FROM linear_relationship")}
        self.assertEqual(rows, {"FIN-1"})

    def test_no_project_cache_is_explicit_error(self):
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="i1"))  # no linear_project rows
        with mock.patch("factory.linear.gql") as gql:
            with self.assertRaisesRegex(relationships.RelationError, "linear_project is empty"):
                relationships.refresh(self.cfg, self.conn)
        gql.assert_not_called()

    def test_observed_at_only_refresh_keeps_fingerprint(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        rels = {"ia": dict(EMPTY)}
        with mock.patch("factory.db.now", side_effect=["2026-09-01T00:00:00.000Z", "2026-09-02T00:00:00.000Z"]):
            self.refresh(rels)
            first = relationships.snapshot(self.conn, "FIN-1")
            self.refresh(rels)
            second = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(first["observed_at"], "2026-09-01T00:00:00.000Z")
        self.assertEqual(second["observed_at"], "2026-09-02T00:00:00.000Z")
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_refresh_rejects_open_transaction_without_network(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        a = endpoint("FIN-1", iid="ia")
        self.refresh({"ia": dict(EMPTY, relations=[{"type": "blocks", "issue": a,
                                                    "relatedIssue": endpoint("FIN-2", iid="ib")}])})
        old = relationships.snapshot(self.conn, "FIN-1")
        with mock.patch("factory.linear.gql") as gql:
            with db.tx(self.conn):
                with self.assertRaisesRegex(relationships.RelationError, "transaction"):
                    relationships.refresh(self.cfg, self.conn)
        gql.assert_not_called()
        snap = relationships.snapshot(self.conn, "FIN-1")
        self.assertEqual(snap["edges"], old["edges"])
        self.assertEqual(snap["fingerprint"], old["fingerprint"])

    def test_network_runs_outside_the_write_transaction(self):
        insert_project(self.conn)
        insert_snapshot(self.conn, raw_snapshot("FIN-1", iid="ia"))
        seen = []

        def gql(cfg, query, variables):
            seen.append(self.conn.in_transaction)
            return FakeLinear(rels={"ia": dict(EMPTY)}, conn=self.conn).gql(cfg, query, variables)

        with mock.patch("factory.linear.gql", side_effect=gql):
            relationships.refresh(self.cfg, self.conn)
        self.assertTrue(seen)
        self.assertFalse(any(seen))


# A minimal genuine v22 database: migrations 23 and 24 add only tables and indexes.
V22 = """
CREATE TABLE linear_snapshot (
  issue_id    TEXT NOT NULL,
  identifier  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  state_type  TEXT NOT NULL,
  in_scope    INTEGER NOT NULL CHECK (in_scope IN (0, 1)),
  raw_json    TEXT NOT NULL CHECK (json_valid(raw_json)),
  PRIMARY KEY (issue_id, updated_at)
);
CREATE TRIGGER linear_snapshot_immutable BEFORE UPDATE ON linear_snapshot
BEGIN SELECT RAISE(ABORT, 'linear_snapshot is append-only'); END;
CREATE TRIGGER linear_snapshot_no_delete BEFORE DELETE ON linear_snapshot
BEGIN SELECT RAISE(ABORT, 'linear_snapshot is append-only'); END;
CREATE TABLE linear_due (
  issue_id            TEXT NOT NULL,
  snapshot_updated_at TEXT NOT NULL,
  due_date            TEXT,
  PRIMARY KEY (issue_id, snapshot_updated_at),
  FOREIGN KEY (issue_id, snapshot_updated_at) REFERENCES linear_snapshot(issue_id, updated_at)
);
CREATE TABLE work_brief (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES work_brief(id),
  state TEXT NOT NULL
);
CREATE TABLE writeback (
  run_id TEXT NOT NULL, issue_id TEXT NOT NULL,
  op TEXT NOT NULL, payload_json TEXT NOT NULL, decision TEXT NOT NULL,
  rule TEXT NOT NULL, reason TEXT, status TEXT NOT NULL, linear_ref TEXT, approved_by TEXT,
  PRIMARY KEY (run_id, issue_id, op)
);
"""


class Migration(unittest.TestCase):
    def test_v22_to_current_preserves_snapshots_and_adds_new_tables(self):
        path = Path(tempfile.mkdtemp()) / "t.db"
        raw = sqlite3.connect(path)
        raw.executescript(V22 + "PRAGMA user_version=22;")
        raw.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1','t','t','unstarted',1,?)", ('{"a":1}',))
        raw.execute("INSERT INTO work_brief VALUES (1,NULL,'draft')")
        raw.commit()
        raw.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT raw_json FROM linear_snapshot WHERE issue_id='i1'").fetchone()[0], '{"a":1}')
        self.assertEqual(conn.execute("SELECT count(*) FROM linear_relationship").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM brief_investigation").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM brief_dismissal").fetchone()[0], 0)
        conn.execute("INSERT INTO brief_dismissal VALUES (1,'not needed','user:cli','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE work_brief SET state='approved' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE linear_snapshot SET identifier='X' WHERE issue_id='i1'")


if __name__ == "__main__":
    unittest.main()

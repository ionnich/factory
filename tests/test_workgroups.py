"""Deterministic workgroup construction from recorded relationships: bounded, stable, non-transitive."""
import unittest

from factory import workgroups


def _ticket(ident, title=None, project=None, context=None):
    return {"identifier": ident, "title": title or ident,
            "project": {"name": project} if project else None, "context": context}


def _snap(edges=(), nodes=(), complete=True, fingerprint="fp", observed_at="2026-09-01T00:00:00Z"):
    return {"complete": complete, "observed_at": observed_at, "edges": list(edges),
            "nodes": list(nodes), "fingerprint": fingerprint}


def _node(ident, title=None, **kw):
    n = {"identifier": ident, "title": title or ident, "url": "", "state": None,
         "state_type": None, "assignee": None, "project": None}
    n.update(kw)
    return n


def _kinds(groups):
    return [(g["kind"], g["members"]) for g in groups]


class Build(unittest.TestCase):
    def test_stable_unique_bounded_grouping(self):
        idents = [f"FIN-{i}" for i in range(1, 14)]
        tickets = [_ticket(i, project="P") for i in idents]
        snaps = {i: _snap() for i in idents}
        g1 = workgroups.build(tickets, snaps)
        g2 = workgroups.build(list(reversed(tickets)), snaps)
        self.assertEqual(g1, g2)  # independent of ticket input order
        members = [m for g in g1 for m in g["members"]]
        self.assertEqual(sorted(members), sorted(idents))  # each source exactly once
        self.assertTrue(all(len(g["members"]) <= 12 for g in g1))  # bounded
        self.assertEqual(len(g1), 2)  # 13 -> 12 + 1 continuation
        self.assertFalse(g1[0]["continued"])
        self.assertTrue(g1[1]["continued"])
        self.assertEqual(g1[0]["title"], "Project bucket: P")
        self.assertEqual(g1[1]["title"], "Project bucket: P (continued 2)")
        self.assertEqual(len({g["id"] for g in g1}), len(g1))

    def test_no_transitive_related(self):
        tickets = [_ticket(i) for i in ("A", "B", "C")]
        snaps = {
            "A": _snap(edges=[{"kind": "related", "source": "A", "target": "B"}],
                       nodes=[_node("A"), _node("B")]),
            "B": _snap(edges=[{"kind": "related", "source": "B", "target": "C"}],
                       nodes=[_node("B"), _node("C")]),
            "C": _snap(nodes=[_node("C")]),
        }
        self.assertEqual(_kinds(workgroups.build(tickets, snaps)),
                         [("related", ["A", "B"]), ("related", ["C"])])  # C never pulled transitively

    def test_external_context_not_source(self):
        tickets = [_ticket("A")]
        snaps = {"A": _snap(edges=[{"kind": "parent", "source": "EP", "target": "A"}],
                            nodes=[_node("A"), _node("EP", "Epic")])}
        (g,) = workgroups.build(tickets, snaps)
        self.assertEqual(g["members"], ["A"])
        self.assertEqual([n["identifier"] for n in g["context"]], ["EP"])  # outside source only
        self.assertNotIn("EP", g["members"])
        self.assertNotIn("A", [n["identifier"] for n in g["context"]])

    def test_parent_title_is_actual_parent(self):
        tickets = [_ticket("A"), _ticket("B")]
        snaps = {
            "A": _snap(edges=[{"kind": "parent", "source": "EP", "target": "A"}],
                       nodes=[_node("A"), _node("EP", "Root Epic")]),
            "B": _snap(edges=[{"kind": "parent", "source": "A", "target": "B"}],
                       nodes=[_node("B"), _node("A")]),
        }
        (g,) = workgroups.build(tickets, snaps)
        self.assertEqual(g["kind"], "parent")
        self.assertEqual(g["title"], "Root Epic")  # topmost parent, not the seed
        self.assertEqual(g["members"], ["A", "B"])  # deterministic parent-first order

    def test_cycles_actual_not_downstream(self):
        # blocks: A -> B, B -> C, C -> B (cycle B/C), C -> D (D is a downstream dependent, not a cycle member)
        tickets = [_ticket(i) for i in ("A", "B", "C", "D")]
        snaps = {
            "A": _snap(edges=[{"kind": "blocks", "source": "A", "target": "B"}], nodes=[_node("A"), _node("B")]),
            "B": _snap(edges=[{"kind": "blocks", "source": "B", "target": "C"},
                              {"kind": "blocks", "source": "C", "target": "B"}], nodes=[_node("B"), _node("C")]),
            "C": _snap(edges=[{"kind": "blocks", "source": "C", "target": "D"}], nodes=[_node("C"), _node("D")]),
            "D": _snap(nodes=[_node("D")]),
        }
        (dep,) = [g for g in workgroups.build(tickets, snaps) if g["kind"] == "dependency"]
        self.assertEqual(sorted(dep["cycles"]), ["B", "C"])
        self.assertNotIn("D", dep["cycles"])

    def test_mixed_edge_types_do_not_manufacture_cycle(self):
        tickets = [_ticket("A"), _ticket("B")]
        snaps = {
            "A": _snap(edges=[{"kind": "parent", "source": "A", "target": "B"}], nodes=[_node("A"), _node("B")]),
            "B": _snap(edges=[{"kind": "blocks", "source": "B", "target": "A"}], nodes=[_node("B"), _node("A")]),
        }
        (fam,) = [g for g in workgroups.build(tickets, snaps) if g["kind"] == "parent"]
        self.assertEqual(fam["cycles"], [])  # parent and blocks are never mixed into a fake cycle

    def test_buckets_precedence_project_then_context_then_unmapped(self):
        tickets = [_ticket("P1", project="Alpha", context="c1"), _ticket("C1", context="c1"),
                   _ticket("U1"), _ticket("P2", project="Beta")]
        snaps = {t["identifier"]: _snap(nodes=[_node(t["identifier"])]) for t in tickets}
        self.assertEqual([(g["kind"], g["title"], g["members"]) for g in workgroups.build(tickets, snaps)], [
            ("project", "Project bucket: Alpha", ["P1"]),
            ("project", "Project bucket: Beta", ["P2"]),
            ("context", "Context bucket: c1", ["C1"]),
            ("context", "Context bucket: Unmapped", ["U1"]),
        ])

    def test_long_dependency_chain_splits_in_prerequisite_order(self):
        identifiers = [f"T-{i:04}" for i in range(1100)]
        edges = [{"kind": "blocks", "source": a, "target": b}
                 for a, b in zip(identifiers, identifiers[1:])]
        groups = workgroups.build([_ticket(i) for i in reversed(identifiers)],
                                  {identifiers[0]: _snap(edges=edges)})
        self.assertEqual([m for group in groups for m in group["members"]], identifiers)
        self.assertTrue(all(len(group["members"]) <= 12 for group in groups))
        self.assertEqual(groups[0]["cycles"], [])
        self.assertIn(identifiers[12], [node["identifier"] for node in groups[0]["context"]])

    def test_parent_precedence_keeps_cross_group_edges_and_dependency_cycles(self):
        tickets = [_ticket(i) for i in ("A", "B", "C")]
        edges = [{"kind": "parent", "source": "A", "target": "B"},
                 {"kind": "blocks", "source": "A", "target": "B"},
                 {"kind": "blocks", "source": "B", "target": "A"},
                 {"kind": "blocks", "source": "B", "target": "C"}]
        groups = workgroups.build(tickets, {"A": _snap(edges=edges)})
        self.assertEqual(_kinds(groups), [("parent", ["A", "B"]), ("dependency", ["C"])])
        self.assertEqual(groups[0]["cycles"], ["A", "B"])
        self.assertIn("B", [node["identifier"] for node in groups[1]["context"]])
        self.assertIn(edges[-1], groups[1]["edges"])


if __name__ == "__main__":
    unittest.main()

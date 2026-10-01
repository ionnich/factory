"""The cost ledger counts factory work only (not nix-fleet crews sharing the worktrees), attributes execution to
the right dispatch, and turns spend into per-unit cost."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from factory import costs, db

RUN = "20260929-090339-fin-3661"


def omp_log(path: Path, started: str, usd: float, text: str = "") -> None:
    path.write_text("\n".join([
        json.dumps({"type": "session", "timestamp": started}, separators=(",", ":")),
        json.dumps({"message": {"content": text}}),
        json.dumps({"message": {"usage": {"input": 10, "output": 5, "cacheRead": 100, "cost": {"total": usd}}}})]))


def seed_executing(c, run_id: str, route: str, pane: str, claims: list[str], start: str) -> None:
    """Stage then execute a legacy (brief_id NULL) dispatch at its own pane with real independent resource claims.

    Claims are pinned while the dispatch is draft (`dispatch_resource` is immutable afterwards), and the
    staged->executing guard re-checks capacity, pane, route and resources — so each dispatch must hold at least one
    claim and its route, pane and resource keys must not conflict with any other live dispatch."""
    c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
              "VALUES (?,'draft','[]','x',?)", (run_id, start))
    for res in sorted(set(claims)):
        c.execute("INSERT INTO dispatch_resource(run_id, resource) VALUES (?,?)", (run_id, res))
    c.execute("UPDATE dispatch SET state='staged', route=?, body_sha256=?, approved_by='u', last_actor='p' "
              "WHERE run_id=?", (route, run_id, run_id))
    c.execute("UPDATE dispatch SET state='executing', executor_pane=?, executing_at=?, last_actor='e' "
              "WHERE run_id=?", (pane, start, run_id))


class Ledger(unittest.TestCase):
    def test_counts_factory_work_only_and_attributes_it_to_its_dispatch(self):
        tmp = Path(tempfile.mkdtemp())
        home, sessions = tmp / "hermes", tmp / "sessions"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "aa11", "name": "factory-prune"},
                                                                     {"id": "bb22", "name": "someone-else"}]}))
        h = sqlite3.connect(home / "state.db")
        h.execute("CREATE TABLE sessions (id, started_at, ended_at, input_tokens, output_tokens, reasoning_tokens, "
                  "cache_read_tokens, actual_cost_usd, estimated_cost_usd)")
        h.executemany("INSERT INTO sessions VALUES (?,?,?,1,1,0,1,NULL,?)",
                      [("cron_aa11_x", 1790713515.0, 1790713590.0, 0.5), ("cron_bb22_x", 1790713515.0, None, 9.0)])
        h.commit()
        captain = sessions / (costs.FLEET_DIR + "factory-primary")
        tree = sessions / "-.treehouse-finks-dagster-1-finks-dagster"
        captain.mkdir(parents=True), tree.mkdir()
        omp_log(captain / "a.jsonl", "2026-09-29T10:00:00.000Z", 2.0, f"run dispatch-intake {RUN}")
        omp_log(tree / "factory.jsonl", "2026-09-29T10:05:00.000Z", 1.0, "brief from factory-fleet fx-news-pipeline")
        omp_log(tree / "nix.jsonl", "2026-09-29T10:06:00.000Z", 7.0, "nix-fleet crew on FIN-9")
        c = db.connect(tmp / "f.db")
        seed_executing(c, RUN, "fx-news-pipeline", "pane-captain",
                       ["repo:Finks-ai/finks-dagster", "route:fx-news-pipeline"], "2026-09-29T09:54:00Z")
        costs.sync(c, home, sessions)
        s = costs.summary(c, 36500)
        self.assertEqual(s["by_stage"], {"prune": 0.5, "captain": 2.0, "crew": 1.0})  # no nix crew, no other cron
        self.assertEqual(s["per_dispatch"], {RUN: 3.0})  # named in the log, or running when the crew started
        self.assertEqual(s["per_unit"]["dispatch"], 3.0)
        n = costs.sync(c, home, sessions)
        self.assertEqual(n, 1)  # only the Hermes row: unchanged omp logs (the nix one too) are not re-read

    def test_ambiguous_parallel_overlap_keeps_run_id_null_and_spend_visible(self):
        tmp = Path(tempfile.mkdtemp())
        home, sessions = tmp / "hermes", tmp / "sessions"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        h = sqlite3.connect(home / "state.db")
        h.execute("CREATE TABLE sessions (id, started_at, ended_at, input_tokens, output_tokens, reasoning_tokens, "
                  "cache_read_tokens, actual_cost_usd, estimated_cost_usd)")
        h.commit()
        crew = sessions / "-.treehouse-finks-dagster-1-finks-dagster"
        crew.mkdir(parents=True)
        # A factory crew session that names no dispatch, while two dispatches genuinely execute in parallel
        # (distinct route/pane/claims, both windows open at the crew's start) — attribution is ambiguous, not
        # fabricated onto one of them.
        omp_log(crew / "factory.jsonl", "2026-09-29T10:05:00.000Z", 1.0, "factory-fleet crew work")
        c = db.connect(tmp / "f.db")
        seed_executing(c, "20260929-090000-fin-100", "fx-news-pipeline", "pane-a",
                       ["repo:Finks-ai/finks-dagster", "route:fx-news-pipeline"], "2026-09-29T10:00:00Z")
        seed_executing(c, "20260929-091500-fin-200", "fx-financial-data", "pane-b",
                       ["repo:Finks-ai/finks-portfolio-service", "route:fx-financial-data"], "2026-09-29T10:04:00Z")
        costs.sync(c, home, sessions)
        s = costs.summary(c, 36500)
        self.assertEqual(s["by_stage"], {"crew": 1.0})  # ambiguous spend stays visible as crew
        self.assertEqual(s["per_dispatch"], {})         # and is not fabricated onto either dispatch
        row = c.execute("SELECT run_id, usd, stage FROM cost_session WHERE id LIKE 'omp:%'").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual((row[1], row[2]), (1.0, "crew"))


if __name__ == "__main__":
    unittest.main()

"""Learnings: one- or two-line facts that save agents tokens, each with provenance (`source`), anchors (repo paths)
and expiry. `sync` (every propose tick) harvests and expires them:
  codemap     what lives at a path, from verdict evidence notes and plan step file refs; active at once, one per
              repo+path; expired when trunk changes (or deletes) the path.
  pitfall     from a blocked card's reason; proposed, active once the user keeps it (decision kind 'learning').
  house_rule  from the user's plan answers: a label chosen twice, or a non-★ choice with a note; proposed likewise.
Gates hand agents the relevant ones (`relevant`); agents cite `L<id>` when one saved them work (`cite`)."""
import json
import re
from functools import cache

from . import db, decide, repos

CAP = 12  # lines per gate context
CITE = re.compile(r"\bL(\d+)\b")
PATHISH = re.compile(r"[\w./-]+\.[A-Za-z]\w{0,5}\b")


def _clip(s: str, n: int = 200) -> str:
    return decide._clip(s, n)


def cite(conn, *texts: str | None) -> None:
    """A write that names `L<id>`: that learning saved someone work. Counted once per write."""
    ids = {int(m) for t in texts if t for m in CITE.findall(t)}
    if ids:
        conn.execute(f"UPDATE learning SET uses = uses + 1 WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))


def _add(conn, kind, scope, body, anchors, trunk, source, status) -> int | None:
    """Insert once per (kind, scope, source, anchors); a codemap also only when its path has no active one."""
    cur = conn.execute("INSERT OR IGNORE INTO learning(kind, scope, body, anchors_json, trunk_sha, source, status, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (kind, scope, _clip(body), json.dumps(anchors), trunk, source, status, db.now()))
    return cur.lastrowid if cur.rowcount else None


def _propose(conn, kind, scope, body, anchors, trunk, source, why) -> None:
    lid = _add(conn, kind, scope, body, anchors, trunk, source, "proposed")
    if lid is None:
        return
    what = "pitfall" if kind == "pitfall" else "house rule"
    decide.open_(conn, "learning", f"Keep this {what} for {scope}? “{_clip(body, 160)}”", [
        decide.option("keep", "Keep it", f"agents working in {scope} read it before they start (verification, "
                                         "planning, dispatch.md)"),
        decide.option("reject", "Drop it", "it is never shown to agents or proposed again"),
    ], "keep", why, "factory:learn", ref=str(lid), detail={"learning": lid, "kind": kind, "source": source})


@cache
def _files(mirror, sha: str) -> tuple[frozenset, dict]:
    """(paths at sha, basename -> paths) in the mirror; empty when sha is unknown to it."""
    try:
        paths = frozenset(repos.git(mirror, "ls-tree", "-r", "--name-only", sha).splitlines())
    except RuntimeError:
        paths = frozenset()
    base = {}
    for p in paths:
        base.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    return paths, base


def _refs(cfg, repo: str, sha: str, text: str) -> list[str]:
    """Repo paths a plan step names: a full path, or a file name unique in the repo."""
    paths, base = _files(cfg.mirror_path(repo), sha)
    out = []
    for tok in PATHISH.findall(text):
        p = tok if tok in paths else base[tok][0] if len(base.get(tok, ())) == 1 else None
        if p and p not in out:
            out.append(p)
    return out


def _codemap(cfg, conn) -> None:
    for v in conn.execute("SELECT id, repo, trunk_sha, evidence_json FROM verdict WHERE superseded_at IS NULL "
                          "AND repo IS NOT NULL AND evidence_paths_json <> '[]' ORDER BY id DESC").fetchall():
        for e in json.loads(v["evidence_json"]):
            if e.get("type") == "file" and e.get("path") and str(e.get("note") or "").strip():
                _add(conn, "codemap", v["repo"], f"{e['path']}: {e['note'].strip()}", [e["path"]], v["trunk_sha"],
                     f"verdict:{v['id']}", "active")
    for s in conn.execute(
            "SELECT s.run_id, s.step_id, s.title, s.detail, v.repo, d.repos_json FROM dispatch_step s "
            "JOIN dispatch d USING (run_id) JOIN dispatch_ticket t ON t.run_id = s.run_id "
            "AND t.identifier = substr(s.step_id, 1, instr(s.step_id, '/') - 1) JOIN verdict v ON v.id = t.verdict_id "
            "WHERE s.step_id LIKE '%/%'").fetchall():
        sha = next((r["trunk_sha"] for r in json.loads(s["repos_json"]) if r["repo"] == s["repo"]), None)
        if sha:
            for p in _refs(cfg, s["repo"], sha, f"{s['title']} {s['detail']}"):
                _add(conn, "codemap", s["repo"], f"{p}: {s['detail'] or s['title']}", [p], sha,
                     f"step:{s['run_id']}:{s['step_id']}", "active")


def _pitfalls(conn, trunks: dict) -> None:
    for d in conn.execute(
            "SELECT d.id, d.node_id, json_extract(d.detail_json, '$.reason') reason, v.repo, v.evidence_paths_json "
            "FROM decision d JOIN dispatch_ticket t ON t.run_id = d.run_id AND t.issue_id = d.issue_id "
            "JOIN verdict v ON v.id = t.verdict_id WHERE d.kind = 'blocked'").fetchall():
        if d["repo"] and (d["reason"] or "").strip():
            _propose(conn, "pitfall", d["repo"], f"{d['node_id']} blocked: {d['reason'].strip()}",
                     json.loads(d["evidence_paths_json"]), trunks.get(d["repo"]), f"decision:{d['id']}",
                     "A block cost a dispatch run; telling the next agent in this repo up front is cheap.")


def _house_rules(conn) -> None:
    themes: dict = {}
    for d in conn.execute(
            "SELECT d.*, (SELECT v.repo FROM dispatch_ticket t JOIN verdict v ON v.id = t.verdict_id "
            "  WHERE t.run_id = d.run_id AND t.identifier = substr(d.node_id || '/', 1, instr(d.node_id || '/', '/') - 1)"
            ") repo, (SELECT json_extract(repos_json, '$[0].repo') FROM dispatch WHERE run_id = d.run_id) first_repo "
            "FROM decision d WHERE d.kind = 'plan' AND d.chosen IS NOT NULL ORDER BY d.id").fetchall():
        repo = d["repo"] or d["first_repo"]
        if not repo or not decide._human(d["chosen_by"]):
            continue
        label = {o["id"]: o["label"] for o in json.loads(d["options_json"])}
        if d["chosen"] != d["recommended"] and (d["chosen_note"] or "").strip():
            _propose(conn, "house_rule", repo, f"{_clip(d['question'], 80)} → {label[d['chosen']]}, not "
                     f"{label[d['recommended']]}: {d['chosen_note'].strip()}", [], None, f"decision:{d['id']}",
                     "You overrode the planner's ★ and said why; the planner can follow it next time.")
        themes.setdefault((repo, label[d["chosen"]].strip().lower()), []).append((d["id"], label[d["chosen"]]))
    # ponytail: a theme is the same option label, case-insensitive; similar wording in different labels is missed.
    for (repo, key), picks in themes.items():
        if len(picks) >= 2:
            _propose(conn, "house_rule", repo, f"You choose “{picks[-1][1]}” ({len(picks)}×: "
                     + ", ".join(f"#{i}" for i, _ in picks) + ")", [], None, f"theme:{key}",
                     f"You picked this option {len(picks)} times in plan questions.")


def _expire(cfg, conn, trunks: dict) -> None:
    """An active learning whose anchors trunk changed (or deleted) no longer describes the code; one whose anchors
    are untouched is still true at the new trunk."""
    for l in conn.execute("SELECT id, scope, trunk_sha, anchors_json FROM learning WHERE status = 'active' "
                          "AND trunk_sha IS NOT NULL AND anchors_json <> '[]'").fetchall():
        new = trunks.get(l["scope"])
        if not new or new == l["trunk_sha"]:
            continue
        changed = repos.changed_paths(cfg.mirror_path(l["scope"]), l["trunk_sha"], new)
        hit = sorted(set(json.loads(l["anchors_json"])) & changed) if changed is not None else None
        if hit is None or hit:
            conn.execute("UPDATE learning SET status = 'expired', expired_reason = ? WHERE id = ?",
                         (f"trunk {new[:12]} changed {', '.join(hit)}" if hit else
                          f"trunk {l['trunk_sha'][:12]} is unknown to the mirror", l["id"]))
        else:
            conn.execute("UPDATE learning SET trunk_sha = ? WHERE id = ?", (new, l["id"]))


def sync(cfg, conn) -> dict:
    trunks = {r["repo"]: r["sha"] for r in conn.execute("SELECT repo, sha FROM repo_trunk")}
    with db.tx(conn):
        _codemap(cfg, conn)
        _pitfalls(conn, trunks)
        _house_rules(conn)
        _expire(cfg, conn, trunks)
    return {r[0]: r[1] for r in conn.execute("SELECT status, count(*) FROM learning GROUP BY status")}


def relevant(conn, repos_: set, paths=(), text: str = "", cap: int = CAP) -> list[str]:
    """What an agent should read first: every active house rule and pitfall for these repos, then the code map
    lines for the paths it will look at (cited paths, or files named in the ticket text); at most `cap` lines."""
    paths = set(paths)
    out = []
    for l in conn.execute(f"SELECT * FROM learning WHERE status = 'active' AND scope IN ({','.join('?' * len(repos_))}) "
                          "ORDER BY kind <> 'house_rule', kind <> 'pitfall', uses DESC, id DESC", tuple(repos_)):
        if l["kind"] == "codemap":
            a = json.loads(l["anchors_json"])[0]
            if a not in paths and a not in text and a.rsplit("/", 1)[-1] not in text:
                continue
        out.append(f"L{l['id']} {l['kind']}: {l['body']}")
        if len(out) >= cap:
            break
    return out


def pitfalls(conn, repos_) -> list[str]:
    """dispatch.md's Known pitfalls."""
    repos_ = list(repos_)
    return [f"- L{r['id']} ({r['scope']}): {r['body']}" for r in conn.execute(
        f"SELECT * FROM learning WHERE status = 'active' AND kind = 'pitfall' AND scope IN "
        f"({','.join('?' * len(repos_))}) ORDER BY uses DESC, id", repos_)]


def rows(conn) -> list[dict]:
    """The Learn tab: active and proposed learnings."""
    return [{**dict(r), "anchors": json.loads(r["anchors"])} for r in conn.execute(
        "SELECT id, kind, scope, body, anchors_json anchors, source, status, created_at, uses FROM learning "
        "WHERE status IN ('active', 'proposed') ORDER BY kind, uses DESC, id DESC")]

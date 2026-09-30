"""Learnings: one- or two-line facts that save agents tokens, each with provenance (`source`), anchors (repo paths)
and expiry. `sync` (every propose tick) harvests and expires them:
  codemap     what lives at a path, from verdict evidence notes; active at once, one per repo+path; expired when
              trunk changes (or deletes) the path.
  pitfall     from a blocked card's reason; proposed, active once the user keeps it (decision kind 'learning').
  house_rule  from the user's plan answers: a label chosen twice, or a non-★ choice with a note; proposed likewise.
Gates hand agents the relevant ones (`relevant`); agents cite `L<id>` when one saved them work (`cite`).
Once the harvesting transaction commits, `sync` also compares each proposed learning with the same-scope learnings
(core's factory.jev) and records the judgment on its open decision's detail_json['jev']; guidance never answers the
decision or changes a learning's status."""
import hashlib
import json
import re

from . import db, decide, repos

try:
    from . import jev  # core slice; until the parent merges it, learnings just stay unannotated
except ImportError:
    jev = None

CAP = 12  # lines per gate context
CITE = re.compile(r"\bL(\d+)\b")


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


def _codemap(conn) -> None:
    for v in conn.execute("SELECT id, repo, trunk_sha, evidence_json FROM verdict WHERE superseded_at IS NULL "
                          "AND repo IS NOT NULL AND evidence_paths_json <> '[]' ORDER BY id DESC").fetchall():
        for e in json.loads(v["evidence_json"]):
            if e.get("type") == "file" and e.get("path") and str(e.get("note") or "").strip():
                _add(conn, "codemap", v["repo"], f"{e['path']}: {e['note'].strip()}", [e["path"]], v["trunk_sha"],
                     f"verdict:{v['id']}", "active")


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
        _codemap(conn)
        # Plan step text is no longer a code fact source: retire what it once activated (the rows stay as audit).
        conn.execute("UPDATE learning SET status = 'expired', "
                     "expired_reason = 'plan step text is no longer a code fact source' "
                     "WHERE status = 'active' AND source LIKE 'step:%'")
        _pitfalls(conn, trunks)
        _house_rules(conn)
        _expire(cfg, conn, trunks)
    _jev_refresh(cfg, conn)  # network only after the harvesting transaction committed
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


# ---- jev: relationship guidance on proposed learnings ----------------------------------------------------------
# After the harvesting transaction commits, each open learning decision is compared through core's factory.jev with
# up to JEV_CANDIDATES same-scope learnings; the judgment is stored on the decision's detail_json['jev'] (key 'jev',
# no space). Unchanged inputs reuse the last successful judgment; failed or disabled calls stay visible and are
# retried next tick. Guidance never answers the decision and never changes a learning's status.
JEV_CANDIDATES = 5


def _jev_candidates(conn, l: dict) -> list[dict]:
    """The bounded, deterministic set to compare against: same scope only, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, body, status FROM learning WHERE scope = ? AND id <> ? "
        "AND status IN ('active', 'proposed') ORDER BY id ASC LIMIT ?", (l["scope"], l["id"], JEV_CANDIDATES))]


def _jev_fingerprint(l: dict, candidates: list[dict]) -> str:
    """The judgment input: the proposal and every compared candidate's status and body, so any change reassesses."""
    blob = {"proposal": {"kind": l["kind"], "scope": l["scope"], "body": l["body"], "source": l["source"]},
            "candidates": [{"id": c["id"], "kind": c["kind"], "status": c["status"], "body": c["body"]}
                           for c in candidates]}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def _jev_state(l: dict, candidates: list[dict]) -> dict:
    return {"proposal": {"kind": l["kind"], "body": l["body"]},
            "candidates": [{"id": c["id"], "kind": c["kind"], "body": c["body"]} for c in candidates]}


def _jev_questions(candidates: list[dict]) -> dict:
    criteria = {}
    for c in candidates:
        for rel in ("duplicate", "supports", "conflicts"):
            criteria[f"{rel}:{c['id']}"] = f"the proposed learning {rel}s L{c['id']} “{_clip(c['body'], 120)}”"
    criteria["none"] = "no meaningful relationship to any candidate"
    return {"relation": {"type": "choice",
                         "instructions": "How does the proposed learning relate to the candidate learnings in the "
                                         "same repo? duplicate states the same fact, supports adds agreeing detail, "
                                         "conflicts contradicts it. Pick the single closest relationship.",
                         "criteria": criteria}}


def _jev_relation(choice, candidates: list[dict]) -> dict | None:
    """Only a relationship whose kind and target are among the compared candidates is trusted."""
    if not isinstance(choice, str) or ":" not in choice:
        return None
    kind, _, raw = choice.partition(":")
    if kind not in ("duplicate", "supports", "conflicts") or not raw.isdigit():
        return None
    target = next((c for c in candidates if c["id"] == int(raw)), None)
    return {"kind": kind, "learning_id": target["id"], "body": target["body"]} if target else None


def _jev_group(conn, lid: int, target: int) -> str | None:
    """learning:<root>: every member of a duplicate/supports component points at its lowest id, and the root points
    nowhere, so group links can never form cycles."""
    root = min(lid, target)
    row = conn.execute("SELECT detail_json FROM decision WHERE kind='learning' AND ref=? ORDER BY id LIMIT 1",
                       (str(target),)).fetchone()
    if row:
        g = ((json.loads(row["detail_json"]) or {}).get("jev") or {}).get("group")
        if isinstance(g, str) and g.startswith("learning:") and g[9:].isdigit():
            root = min(root, int(g[9:]))
    return f"learning:{root}" if root < lid else None


def _jev_build(res: dict, fp: str, conn, l: dict, candidates: list[dict]) -> dict:
    out = {"status": res.get("status") or "unavailable", "assessed_at": db.now(), "fingerprint": fp}
    if out["status"] == "ok":
        if res.get("model"):
            out["model"] = res["model"]
        ans = (res.get("answers") or {}).get("relation") or {}
        conf = ans.get("confidence")
        if isinstance(conf, (int, float)) and 0 <= conf <= 1:
            out["confidence"] = conf
        rel = _jev_relation(ans.get("choice"), candidates)
        if rel:
            out["relation"] = rel
            if rel["kind"] in ("duplicate", "supports"):
                group = _jev_group(conn, l["id"], rel["learning_id"])
                if group:
                    out["group"] = group
    else:
        out["error"] = res.get("error") or ("jev disabled" if out["status"] == "disabled" else "jev unavailable")
    return out


def _jev_refresh(cfg, conn) -> None:
    """Assess the open learning decisions. The network call happens here, never under a transaction; a decision
    answered or withdrawn while it was in flight is left untouched."""
    if jev is None:
        return
    opens = conn.execute(
        "SELECT d.id did, d.detail_json, l.id lid, l.kind, l.scope, l.body, l.source FROM decision d "
        "JOIN learning l ON l.id = CAST(d.ref AS INTEGER) "
        "WHERE d.kind = 'learning' AND d.chosen IS NULL AND d.void_reason IS NULL ORDER BY d.id").fetchall()
    updates: list[tuple[int, dict]] = []
    for d in opens:
        l = {"id": d["lid"], "kind": d["kind"], "scope": d["scope"], "body": d["body"], "source": d["source"]}
        candidates = _jev_candidates(conn, l)
        if not candidates:
            continue  # nothing to compare: no guidance to show
        fp = _jev_fingerprint(l, candidates)
        cur = (json.loads(d["detail_json"]) or {}).get("jev") or {}
        if cur.get("status") == "ok" and cur.get("fingerprint") == fp:
            continue  # unchanged successful judgment: reuse it
        try:
            res = jev.evaluate(cfg, _jev_state(l, candidates), _jev_questions(candidates))
        except Exception as e:  # evaluate is contractually non-raising; this keeps learn.sync safe regardless
            res = {"status": "unavailable", "error": f"{type(e).__name__}: {str(e)[:160]}"}
        updates.append((d["did"], _jev_build(res, fp, conn, l, candidates)))
    if not updates:
        return
    with db.tx(conn):
        for did, out in updates:
            d = conn.execute("SELECT detail_json FROM decision WHERE id=? AND chosen IS NULL AND void_reason IS NULL",
                             (did,)).fetchone()
            if d is None:
                continue  # answered or withdrawn during the network call: guidance never rewrites a closed decision
            detail = json.loads(d["detail_json"]) or {}
            detail["jev"] = out
            conn.execute("UPDATE decision SET detail_json=? WHERE id=?", (json.dumps(detail), did))


def rows(conn) -> list[dict]:
    """The Learn tab: active and proposed learnings, each with the guidance (`jev`) its decision carries."""
    out = []
    for r in conn.execute(
            "SELECT l.id, l.kind, l.scope, l.body, l.anchors_json anchors, l.source, l.status, l.created_at, l.uses, "
            "(SELECT detail_json FROM decision d WHERE d.kind='learning' AND d.ref = CAST(l.id AS TEXT) "
            " ORDER BY d.id LIMIT 1) detail_json FROM learning l "
            "WHERE l.status IN ('active', 'proposed') ORDER BY l.kind, l.uses DESC, l.id DESC"):
        d = dict(r)
        d["anchors"] = json.loads(d.pop("anchors"))
        detail = json.loads(d.pop("detail_json") or "{}") or {}
        if detail.get("jev"):
            d["jev"] = detail["jev"]
        out.append(d)
    return out

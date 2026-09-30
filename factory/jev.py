"""Jev: TypeSafe judgment guidance for factory decisions.

factory.jev.evaluate(cfg, state, questions) is the shared client (POST
https://api.typesafe.ai/v1/systemone, model jev-1.13.0 pinned, Bearer
TYPESAFE_API_KEY). It never raises and never fabricates: failure is a
sanitized {'status': 'unavailable', 'error': ...}; a disabled or keyless
config is safe too.

Guidance, not action: Jev screens open plan/ask/review decisions
(investigate / policy / human / unclear), matches approved house rules and
picks a focus from existing consequence text. It never answers, voids or
starts anything. `refresh` (factory jev sync, propose tick) re-judges open
decisions outside any transaction; successful judgments are fingerprinted
(inputs + eligible rules) and reused across ticks while inputs are
unchanged. Reads (decide rows, status, overview) never call the network and
drop a stale approved-rule claim once the rule expires or is rejected."""
import hashlib
import json
import math
import re
import urllib.error
import urllib.request

from . import config, db

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
THRESHOLD = 0.85  # a plan question this sure to be pure missing investigation is refused at plan time
MAX_RULES = 8     # shortlisted eligible rules per judgment (state cap)
MAX_EVIDENCE = 6  # evidence notes per plan question (state cap)
KINDS = ("plan", "ask", "review")  # the only kinds Jev guides; learning's jev belongs to the learning slice


def _conf(cfg) -> dict:
    return cfg.raw.get("jev", {}) if isinstance(getattr(cfg, "raw", None), dict) else {}


def enabled(cfg) -> bool:
    return bool(_conf(cfg).get("enabled"))


# ---- shared client ------------------------------------------------------------------------------------------------
def _p01(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1


def _validate(payload, questions) -> tuple:
    """The response answers every question, with the requested type, allowed candidates and [0,1] numbers."""
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise ValueError("unreadable response")
    answers, usage = payload.get("answers"), payload.get("usage")
    if not isinstance(answers, dict) or not isinstance(usage, dict):
        raise ValueError("malformed response")
    it, ot = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(it, int) or not isinstance(ot, int) or it < 0 or ot < 0:
        raise ValueError("bad usage")
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise ValueError(f"missing or wrong-typed answer for {qid}")
        if q["type"] == "choice":
            if a.get("choice") not in q["criteria"]:
                raise ValueError(f"answer {qid}: choice outside its criteria")
            probs = a.get("probabilities")
            if (not isinstance(probs, dict) or not probs
                    or any(k not in q["criteria"] for k in probs) or any(not _p01(p) for p in probs.values())
                    or not _p01(a.get("confidence"))):
                raise ValueError(f"answer {qid}: bad probabilities or confidence")
        elif q["type"] == "noul":
            if not _p01(a.get("noul")):
                raise ValueError(f"answer {qid}: noul outside [0,1]")
        else:
            raise ValueError(f"answer {qid}: unsupported type")
    return payload["model"], answers, usage


def evaluate(cfg, state: dict, questions: dict) -> dict:
    """One bounded call. Success: {status:'ok', model, answers, usage}. Failure/disabled: a sanitized
    {status:'unavailable'|'disabled', error} — no provider body, no secrets, never a fabricated judgment."""
    j = _conf(cfg)
    if not j.get("enabled"):
        return {"status": "disabled", "error": "jev is disabled ([jev] enabled in factory.toml)"}
    try:
        key = config.secret(cfg, "TYPESAFE_API_KEY")
    except config.ConfigError:
        return {"status": "unavailable", "error": "no TYPESAFE_API_KEY in the environment or secrets.env_files"}
    if not key:
        return {"status": "unavailable", "error": "TYPESAFE_API_KEY is empty"}
    try:
        timeout = max(1, min(int(j.get("timeout_seconds", 30) or 30), 60))
    except (TypeError, ValueError):
        timeout = 30
    body = json.dumps({"state": state, "model": str(j.get("model") or DEFAULT_MODEL), "questions": questions}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": "unavailable", "error": f"typesafe api: HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"status": "unavailable", "error": f"typesafe api: {type(e).__name__}"}
    try:
        model, answers, usage = _validate(payload, questions)
    except ValueError as e:
        return {"status": "unavailable", "error": f"typesafe api: {e}"}
    return {"status": "ok", "model": model, "answers": answers, "usage": usage}


# ---- screening ----------------------------------------------------------------------------------------------------
def _questions() -> dict:
    return {"category": {"type": "choice", "instructions": (
        "The state describes one decision an operator of a software factory is asked to make, plus the approved "
        "house rules that may bear on it. Classify why it is being asked. Authority, consent and permission "
        "questions are never mere investigation."),
        "criteria": {
            "investigate": ("the question asks for missing factual investigation: the code, data, logs or "
                            "records would settle it and it should be checked or measured instead of chosen; "
                            "never authority, consent, permission, taste or a policy call"),
            "policy": ("an approved house rule, stated policy or established convention (see the rules) already "
                       "answers it for this repo"),
            "human": ("a genuine preference, permission, authority, consent or tradeoff only the operator can "
                      "decide: taste, risk appetite, scope or spend"),
            "unclear": "not enough information, or it does not clearly fit the other categories"}}}


def _clip(s, n: int = 400) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _option(o, kind) -> dict:
    if kind != "plan":
        return {k: o[k] for k in ("id", "label", "leads_to") if k in o}
    out = {k: o[k] for k in ("id", "label", "leads_to") if k in o}
    for k in ("result", "cost", "risk"):
        if o.get(k):
            out[k] = _clip(o[k], 200)
    if changes := [c for c in o.get("changes") or []]:
        out["changes"] = [{"step": c["step"], "becomes": c["becomes"]} if "step" in c
                          else {"add": c["add"]["id"], "title": _clip(c["add"].get("title", ""), 120)}
                          for c in changes]
    return out


def repos_for(conn, d) -> set[str]:
    """The repo scope a decision lives in; rules match per repo."""
    j = conn.execute("SELECT repos_json FROM dispatch WHERE run_id=?", (d["run_id"],)).fetchone()
    repos = {r["repo"] for r in json.loads(j["repos_json"])} if j else set()
    if d["kind"] == "ask" and d.get("node_id"):
        r = conn.execute("SELECT v.repo FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                         "WHERE t.run_id=? AND t.identifier=?", (d["run_id"], d["node_id"].split("/")[0])).fetchone()
        if r and r["repo"]:
            return {r["repo"]}
    return repos


def _human_keep(conn, lid: int) -> bool:
    from . import decide
    keep = conn.execute("SELECT chosen_by FROM decision WHERE kind='learning' AND ref=? AND chosen='keep'",
                        (str(lid),)).fetchone()
    return bool(keep and decide._human(keep["chosen_by"]))


def eligible_rules(conn, repos) -> list[dict]:
    """Active house rules a person explicitly kept, in the repo scope, oldest first."""
    repos = list(repos)
    out = []
    if repos:
        for r in conn.execute(f"SELECT id, body FROM learning WHERE status='active' AND kind='house_rule' AND "
                              f"scope IN ({','.join('?' * len(repos))}) ORDER BY id", tuple(repos)):
            if _human_keep(conn, r["id"]):
                out.append(dict(r))
    return out


def _state(conn, d) -> tuple[dict, list]:
    """Minimal state: the decision, its options, plan evidence notes, shortlisted same-repo rules. Never repo
    contents, config or credentials."""
    rules = eligible_rules(conn, repos_for(conn, d))[:MAX_RULES]
    dec = {"kind": d["kind"], "question": d["question"], "recommended": d["recommended"], "why": _clip(d["why"]),
           "options": [_option(o, d["kind"]) for o in d["options"]]}
    detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
    if d["kind"] == "plan":
        if detail.get("now"):
            dec["now"] = _clip(detail["now"])
        ev = []
        for e in (detail.get("evidence") or [])[:MAX_EVIDENCE]:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            ev.append(_clip(f"{e['path']}{f':{e['line']}' if e.get('line') else ''}"
                            + (f" ({e['note']})" if e.get("note") else ""), 200))
        if ev:
            dec["evidence"] = ev
    state = {"decision": dec}
    if rules:
        state["rules"] = [{"id": r["id"], "body": _clip(r["body"], 200)} for r in rules]
    return state, rules


# ---- rule matching ------------------------------------------------------------------------------------------------
_QUESTION_RULE = re.compile(r"^(.+?) → ([^,]+), not ")
_THEME_RULE = re.compile(r"^You choose “([^”]+)”")


def _label_of(options, label) -> str | None:
    want = label.strip().lower()
    return next((o["id"] for o in options if o["label"].strip().lower() == want), None)


def _rule_option(body: str, question: str, options: list) -> str | None:
    """The option id a house rule names, when the rule applies to this question. No match is fine."""
    if m := _THEME_RULE.match(body):
        return _label_of(options, m[1])
    if m := _QUESTION_RULE.match(body):
        frag = m[1].rstrip("…").strip()
        if frag and question.startswith(frag):
            return _label_of(options, m[2].strip())
    return None


def rule_for(conn, d) -> dict | None:
    for r in eligible_rules(conn, repos_for(conn, d)):
        if option_id := _rule_option(r["body"], d["question"], d["options"]):
            return {"id": r["id"], "body": r["body"], "option_id": option_id}
    return None


# ---- focus --------------------------------------------------------------------------------------------------------
def focus(d) -> str:
    """The existing per-option consequence worth highlighting: the first aspect where the options differ.
    Executor (ask) options carry their consequence only as leads_to, so their focus is `result` or nothing."""
    if d["kind"] == "ask":
        return "result" if any(str(o.get("leads_to") or "").strip() for o in d["options"]) else "none"
    if d["kind"] != "plan":
        return "none"
    for aspect in ("risk", "cost", "changes", "result"):
        keys = {json.dumps(o.get(aspect), sort_keys=True) if aspect == "changes" else str(o.get(aspect) or "").strip()
                for o in d["options"]}
        if len(keys) > 1 and any(str(o.get(aspect) or "").strip() for o in d["options"]):
            return aspect
    return "none"


# ---- judging ------------------------------------------------------------------------------------------------------
def fingerprint(state: dict, rule_ids: list) -> str:
    return hashlib.sha256(json.dumps({"state": state, "rules": sorted(rule_ids)}, sort_keys=True).encode()).hexdigest()


def assess(cfg, conn, d) -> dict | None:
    """One open decision's guidance: None = unchanged success (reuse what is stored), else the guidance to
    persist. The network call happens outside any transaction; the caller re-reads before storing."""
    if d.get("kind") not in KINDS:
        return None
    state, rules = _state(conn, d)
    fp = fingerprint(state, [r["id"] for r in rules])
    prev = stored(conn, d["id"]) if d.get("id") is not None else None
    if isinstance(prev, dict) and prev.get("status") == "ok" and prev.get("fingerprint") == fp:
        return None
    res = evaluate(cfg, state, _questions())
    g = {"status": res["status"], "assessed_at": db.now(), "fingerprint": fp}
    if res["status"] != "ok":
        g["error"] = res["error"]
        return g  # retried on the next refresh/tick; never cached as a judgment
    category = res["answers"]["category"]
    g.update(model=res["model"], category=category["choice"], confidence=category["confidence"])
    if category["choice"] == "policy" and (rule := rule_for(conn, d)):
        g["rule"] = rule
    if d["kind"] in ("plan", "ask"):
        g["focus"] = focus(d)
    return g


def assess_plan(cfg, conn, run_id, qs, recommend, review_why) -> dict | None:
    """Judgments for a new plan's questions and its review, before anything is written. A plan question Jev is
    sure (confidence >= THRESHOLD) asks for pure missing investigation is refused here: that is missing work,
    not a choice. Returns {'questions': [...], 'review': ...} or None when Jev is disabled."""
    from . import decide, dispatch
    if not enabled(cfg):
        return None
    out, blocked = [], []
    for q in qs:
        g = assess(cfg, conn, {"kind": "plan", "question": q["question"], "recommended": q["recommend"],
                               "why": q["why"], "options": q["options"], "run_id": run_id, "node_id": q["on"],
                               "detail": {"now": q["now"], "evidence": q["evidence"]}})
        g = g or {"status": "unavailable", "error": "no judgment", "assessed_at": db.now()}
        if g.get("status") == "ok" and g.get("category") == "investigate" and g.get("confidence", 0) >= THRESHOLD:
            blocked.append(q["key"])
        out.append(g)
    if blocked:
        raise dispatch.StageError("investigate in the code, not a question for the reviewer: " + "; ".join(
            f"'{k}' asks for a fact the code or data settle (Jev, confidence >= {THRESHOLD:g}) — check the "
            f"evidence and decide it in the plan" for k in blocked))
    review_g = assess(cfg, conn, {"kind": "review", "question": "Start this dispatch?", "recommended": recommend,
                                  "why": review_why, "options": decide.review_options(False), "run_id": run_id,
                                  "node_id": "root", "detail": {}})
    return {"questions": out, "review": review_g}


# ---- storing and refreshing ---------------------------------------------------------------------------------------
def stored(conn, decision_id: int) -> dict | None:
    """The persisted advice for a decision, or None. A pure read: never the network."""
    r = conn.execute("SELECT payload_json FROM jev_advice WHERE decision_id=?", (decision_id,)).fetchone()
    if r is None:
        return None
    try:
        j = json.loads(r["payload_json"])
    except json.JSONDecodeError:
        return None
    return j if isinstance(j, dict) else None


def store(conn, decision_id: int, advice: dict) -> bool:
    """Upsert advice only while its decision is still open. The recheck happens inside one short transaction,
    with no network. False: the decision is gone, or was answered/withdrawn while the judgment was made."""
    with db.tx(conn):
        row = conn.execute("SELECT chosen, void_reason FROM decision WHERE id=?", (decision_id,)).fetchone()
        if row is None or row["chosen"] is not None or row["void_reason"] is not None:
            return False
        conn.execute("INSERT INTO jev_advice(decision_id, payload_json) VALUES (?, ?) "
                     "ON CONFLICT(decision_id) DO UPDATE SET payload_json=excluded.payload_json",
                     (decision_id, json.dumps(advice)))
    return True


def refresh(cfg, conn) -> list[dict]:
    """Re-judge open plan/ask/review decisions (factory jev sync, propose tick). Learning decisions are never
    touched: their advice holds the learning slice's relation/group metadata."""
    from . import decide
    if not enabled(cfg):
        return []
    out = []
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM decision WHERE {decide.OPEN} AND kind IN ('plan','ask','review') ORDER BY id")]
    for did in ids:
        d = decide.one(conn, did)
        if not d or not d["open"]:
            continue
        guidance = assess(cfg, conn, d)
        if guidance is None:
            continue  # unchanged success: keep the stored judgment
        if store(conn, did, guidance):
            out.append({"decision": did, **{k: guidance[k] for k in ("status", "category", "confidence")
                                           if k in guidance}})
    return out


# ---- reading ------------------------------------------------------------------------------------------------------
def _rule_current(conn, lid: int, options, option_id) -> bool:
    r = conn.execute("SELECT 1 FROM learning WHERE id=? AND status='active' AND kind='house_rule'", (lid,)).fetchone()
    return bool(r and _human_keep(conn, lid) and option_id in {o["id"] for o in options})


def _learning_eligible(conn, lid: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM learning WHERE id=? AND status IN ('active', 'proposed')",
                             (lid,)).fetchone())


def served(payload, conn, options) -> dict | None:
    """The top-level jev a read may show: absent when there is none, and never a stale approved-rule claim or a
    relation to a learning that no longer exists as active/proposed."""
    if not isinstance(payload, dict) or payload.get("status") not in ("ok", "unavailable", "disabled"):
        return None
    j = dict(payload)
    rule = j.get("rule")
    if isinstance(rule, dict) and rule.get("id") and not _rule_current(conn, rule["id"], options, rule.get("option_id")):
        j["rule"] = None
    rel = j.get("relation")
    if isinstance(rel, dict) and rel.get("learning_id") and not _learning_eligible(conn, rel["learning_id"]):
        j["relation"] = None
    return j

"""Jev: TypeSafe judgment guidance for factory decisions.

factory.jev.evaluate(cfg, state, questions) is the shared client (POST
https://api.typesafe.ai/v1/systemone, model jev-1.13.0 pinned, Bearer
TYPESAFE_API_KEY). It never raises and never fabricates: failure is a
sanitized {'status': 'unavailable', 'error': ...}; a disabled or keyless
config is safe too.

Guidance, not action: Jev screens open plan/ask/review decisions
(investigate / policy / human / unclear), matches approved house rules and
picks a focus from existing consequence text — category, rule and focus come
back from one batched typed-Choice call, each answer validated by the client
(no regex or body-text matching). It never answers, voids or starts anything.
`refresh` (factory jev sync, propose tick) re-judges open decisions outside
any transaction within a bounded budget; successful judgments are
fingerprinted (inputs + eligible rules + the questions asked + the model) and
reused across ticks while unchanged. Reads (decide rows, status, overview)
never call the network and drop a stale approved-rule claim (with its policy
category) once the rule expires or is rejected, and a relation/group whose
learning is gone or rewritten."""
import hashlib
import json
import math
import time
import urllib.error
import urllib.request

from . import config, db

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
THRESHOLD = 0.85  # a plan question this sure to be pure missing investigation is refused at plan time
RULE_CONFIDENCE = 0.85  # a rule claim is emitted only when the rule/option choice is this sure
REFRESH_BUDGET = 15.0   # seconds: one propose tick never waits on the network for the whole queue
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
            if (not isinstance(probs, dict) or set(probs) != set(q["criteria"])
                    or any(not _p01(p) for p in probs.values())
                    or not math.isclose(sum(probs.values()), 1.0, abs_tol=1e-6)
                    or not _p01(a.get("confidence"))):
                raise ValueError(f"answer {qid}: bad probabilities or confidence")
            if max(probs.values()) != probs[a["choice"]]:  # ties are fine: the choice may share the maximum
                raise ValueError(f"answer {qid}: choice is not a most probable candidate")
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
    except (OSError, UnicodeDecodeError) as e:  # an unreadable/undecodable env file: type-only, never its contents
        return {"status": "unavailable", "error": f"typesafe api: TYPESAFE_API_KEY unreadable ({type(e).__name__})"}
    if not key:
        return {"status": "unavailable", "error": "TYPESAFE_API_KEY is empty"}
    try:
        timeout = max(1, min(int(j.get("timeout_seconds", 30) or 30), 60))
    except (TypeError, ValueError):
        timeout = 30
    try:
        body = json.dumps({"state": state, "model": str(j.get("model") or DEFAULT_MODEL),
                           "questions": questions}).encode()
    except (TypeError, ValueError, UnicodeEncodeError):  # the serialization boundary: a fixed error, no state echoed
        return {"status": "unavailable", "error": "typesafe api: request state is not JSON-serializable"}
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": "unavailable", "error": f"typesafe api: HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"status": "unavailable", "error": f"typesafe api: {type(e).__name__}"}
    try:
        model, answers, usage = _validate(payload, questions)
    except ValueError as e:
        return {"status": "unavailable", "error": f"typesafe api: {e}"}
    return {"status": "ok", "model": model, "answers": answers, "usage": usage}


# ---- screening ----------------------------------------------------------------------------------------------------
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
    """The repo scope a decision lives in; rules match per repo. A plan question or executor ask on one ticket
    (or a ticket's step) is scoped to that ticket's repo, so other repos' rules can never match it."""
    j = conn.execute("SELECT repos_json FROM dispatch WHERE run_id=?", (d["run_id"],)).fetchone()
    repos = {r["repo"] for r in json.loads(j["repos_json"])} if j else set()
    if d.get("node_id") and d["node_id"] != "root" and d["kind"] in ("plan", "ask"):
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
def _rule_pairs(rules: list, options: list) -> list:
    """Every (rule, option) pair a rule Choice may name: each eligible rule with each of the decision's options."""
    return [(r, o) for r in rules for o in options]


def _rule_choice(conn, d, rules, choice) -> dict | None:
    """The house rule a rule Choice names, only while it is still eligible (active, a person explicitly kept it,
    same repo scope) and its option is one of this decision's. Semantic matching is the model's typed judgment;
    there is no regex or body-text fallback."""
    try:
        lid, option_id = json.loads(choice)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    r = next((r for r in rules if r["id"] == lid), None)
    if r is None or option_id not in {o["id"] for o in d["options"]}:
        return None
    if not _rule_current(conn, lid, d["options"], option_id, repos_for(conn, d)):
        return None
    return {"id": lid, "body": r["body"], "option_id": option_id}


# ---- focus --------------------------------------------------------------------------------------------------------
def _focus_criteria(d) -> list[str]:
    """The consequence aspects that may be highlighted: present and actually differing across the options.
    Executor (ask) options carry their consequence only as leads_to, so that is `result` or nothing."""
    if d["kind"] == "ask":
        leads = {str(o.get("leads_to") or "").strip() for o in d["options"]}
        return ["result"] if len(leads) > 1 and any(leads) else []
    if d["kind"] != "plan":
        return []
    out = []
    for aspect in ("risk", "cost", "changes", "result"):
        keys = {json.dumps(o.get(aspect), sort_keys=True) if aspect == "changes" else str(o.get(aspect) or "").strip()
                for o in d["options"]}
        if len(keys) > 1 and any(str(o.get(aspect) or "").strip() for o in d["options"]):
            out.append(aspect)
    return out


# ---- questions ----------------------------------------------------------------------------------------------------
_FOCUS_LABEL = {"result": "what the user notices once it lands", "changes": "what the plan would change",
                "cost": "the cost of the choice", "risk": "the risk of the choice"}


def _questions(d, rules) -> dict:
    """The typed Choice questions for one decision, batched into one evaluate call: category always, plus the
    rule/option Choice (eligible pairs + none) and the focus Choice (differing consequences + none) when they
    have real candidates."""
    q = {"category": {"type": "choice", "instructions": (
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
    if pairs := _rule_pairs(rules, d["options"]):
        q["rule"] = {"type": "choice", "instructions": (
            "Only if one of the listed rules already names the choice for this exact question, pick that rule's "
            "option. A rule names a choice when its body, however paraphrased, says this decision should go that "
            "way in this repo. Otherwise pick none. Never infer a rule the operator did not approve."),
            "criteria": {json.dumps([r["id"], o["id"]]): f"house rule L{r['id']} supports choosing "
                         f"{_clip(o['label'], 80)} here ({_clip(r['body'], 160)})" for r, o in pairs}
            | {"none": "no listed rule names a choice for this decision"}}
    if focus := _focus_criteria(d):
        q["focus"] = {"type": "choice", "instructions": (
            "Pick the one consequence aspect that most deserves the operator's attention when comparing these "
            "options, using only the consequence text already present. Pick none when nothing stands out."),
            "criteria": {**{f: _FOCUS_LABEL[f] for f in focus}, "none": "no aspect stands out"}}
    return q


# ---- judging ------------------------------------------------------------------------------------------------------
def fingerprint(cfg, state: dict, rule_ids: list, questions: dict) -> str:
    """The judgment input plus its semantics: the state, eligible rules, the exact question set asked, and the
    model (and rule bar) that answered it. A model or config change invalidates every stored success."""
    j = _conf(cfg)
    meta = {"model": str(j.get("model") or DEFAULT_MODEL), "rule_confidence": RULE_CONFIDENCE}
    return hashlib.sha256(json.dumps({"state": state, "rules": sorted(rule_ids), "questions": questions,
                                      "config": meta}, sort_keys=True).encode()).hexdigest()


def assess(cfg, conn, d) -> dict | None:
    """One open decision's guidance: None = unchanged success (reuse what is stored), else the guidance to
    persist. Category, rule and focus come back from one batched typed-Choice call, each answer validated by
    the client. The network call happens outside any transaction; the caller re-reads before storing."""
    if d.get("kind") not in KINDS:
        return None
    state, rules = _state(conn, d)
    questions = _questions(d, rules)
    fp = fingerprint(cfg, state, [r["id"] for r in rules], questions)
    prev = stored(conn, d["id"]) if d.get("id") is not None else None
    if isinstance(prev, dict) and prev.get("status") == "ok" and prev.get("fingerprint") == fp:
        return None
    res = evaluate(cfg, state, questions)
    g = {"status": res["status"], "assessed_at": db.now(), "fingerprint": fp}
    if res["status"] != "ok":
        g["error"] = res["error"]
        return g  # retried on the next refresh/tick; never cached as a judgment
    answers = res["answers"]
    category = answers.get("category") or {}
    g.update(model=res["model"], category=category.get("choice"), confidence=category.get("confidence"))
    if d["kind"] in ("plan", "ask"):
        g["focus"] = (answers.get("focus") or {}).get("choice") if "focus" in questions else "none"
    if category.get("choice") == "policy":
        rule = answers.get("rule") or {}
        if rule.get("choice") != "none" and _p01(rule.get("confidence")) \
                and rule["confidence"] >= RULE_CONFIDENCE \
                and (matched := _rule_choice(conn, d, rules, rule["choice"])):
            g["rule"] = matched
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


def refresh(cfg, conn, budget: float | None = REFRESH_BUDGET) -> list[dict]:
    """Re-judge open plan/ask/review decisions (factory jev sync, propose tick). Learning decisions are never
    touched: their advice holds the learning slice's relation/group metadata. `budget` bounds one pass's total
    wall time so a propose tick's notice is never held up by the whole queue; the rest is judged on later ticks
    (unchanged successes are reused, failures are retried then). None: no bound (explicit factory jev sync)."""
    from . import decide
    if not enabled(cfg):
        return []
    deadline = None if budget is None else time.monotonic() + budget
    out = []
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM decision WHERE {decide.OPEN} AND kind IN ('plan','ask','review') ORDER BY id")]
    for did in ids:
        if deadline is not None and time.monotonic() >= deadline:
            break  # the rest of the queue waits for a later pass, never this notice
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
def _rule_current(conn, lid: int, options, option_id, repos) -> bool:
    r = conn.execute("SELECT scope FROM learning WHERE id=? AND status='active' AND kind='house_rule'",
                     (lid,)).fetchone()
    return bool(r and _human_keep(conn, lid) and option_id in {o["id"] for o in options}
                and (repos is None or r["scope"] in repos))


def _relation_current(conn, rel: dict, repos) -> bool:
    """The relation target still exists as active/proposed with an unchanged body, and (scope known) lives in the
    decision's repo scope. Cross-repo or rewritten learnings lose their relation and its group."""
    r = conn.execute("SELECT body, scope FROM learning WHERE id=? AND status IN ('active', 'proposed')",
                     (rel["learning_id"],)).fetchone()
    if r is None or (rel.get("body") is not None and rel["body"] != r["body"]):
        return False
    return repos is None or r["scope"] in repos


def served(payload, conn, options, repos=None) -> dict | None:
    """The top-level jev a read may show: absent when there is none, and never a stale approved-rule claim (a rule
    that expired, was rejected, or left the repo scope, together with the policy claim it carried), a relation to
    a learning that is gone or was rewritten, or the group that relation belonged to."""
    if not isinstance(payload, dict) or payload.get("status") not in ("ok", "unavailable", "disabled"):
        return None
    j = dict(payload)
    rule = j.get("rule")
    if isinstance(rule, dict) and rule.get("id") \
            and not _rule_current(conn, rule["id"], options, rule.get("option_id"), repos):
        j["rule"] = None
        if j.get("category") == "policy":  # the approved-rule claim is gone: policy no longer applies
            j["category"] = "unclear"
    rel = j.get("relation")
    if isinstance(rel, dict) and rel.get("learning_id") and not _relation_current(conn, rel, repos):
        j["relation"] = None
        j["group"] = None
    return j


def read(conn, decision_id: int, options: list, repos: set[str] | None = None) -> dict | None:
    """The guidance a surface may show for one decision: the stored payload with every stale claim dropped.
    `repos` is the decision's repo scope for the scope checks; None skips them (learning decisions have none,
    and learn.rows has its own). Pure read: never the network."""
    return served(stored(conn, decision_id), conn, options, repos)

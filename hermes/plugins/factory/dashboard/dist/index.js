// Factory tab. Plain words first; ids, hashes and evidence one click away. Actions (stage, hand off,
// resolve flag) go through the plugin API to the factory CLI, which enforces every invariant.
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Button } = SDK.components;
  const h = React.createElement;
  const REFRESH_MS = 30000;

  const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "never");
  const epochAgo = (v) => (v == null ? "never" : typeof v === "number" ? SDK.utils.timeAgo(v) : ago(v));
  const errText = (e) => String(e && e.message ? e.message : e);
  const stop = (e) => e.stopPropagation();
  const API = "/api/plugins/factory";
  const post = (path, body) => SDK.fetchJSON(API + path,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  // One button's request: busy while in flight, the API's refusal text shown next to it.
  function useAction(onDone) {
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const run = (path, body) => {
      setBusy(true); setErr(null);
      return post(path, body).then(() => { setBusy(false); onDone(); }, (e) => { setBusy(false); setErr(errText(e)); });
    };
    return { busy, err, run };
  }
  const ActErr = ({ err }) => (err ? h("span", { className: "act-err" }, err) : null);

  // ---- plain-language status for one ticket ------------------------------------------------
  const RECHECK = {
    new: "Not verified yet",
    "ticket-changed": "Ticket changed in Linear since it was verified",
    "evidence-changed": "Code it was verified against has changed",
    "context-changed": "Repo mapping changed; verifying again",
  };
  const CARD = { ready: "not started", running: "being worked on", done: "done", blocked: "blocked" };
  const FLAG_LABEL = (f) => f.kind === "kanban-mirror" ? "Board out of sync"
    : ({ state: "Status held", description: "Note held", comment: "Comment held" })[f.op]
      || "Decide";

  function ticketStatus(t, skipped) {
    const v = t.verdict;
    if (t.dispatch) {
      return { group: "progress", tone: "blue", label: "In a dispatch",
               why: `Card ${CARD[t.dispatch.card_status] || t.dispatch.card_status}` };
    }
    if (!t.context) return { group: "ignored", tone: "gray", label: "Not mapped",
                             why: "No repo is mapped for this domain, so it isn't verified" };
    if (!v || t.freshness !== "fresh") {
      return { group: "progress", tone: "blue", label: "Checking", why: RECHECK[t.freshness] || "Queued for verification" };
    }
    switch (v.kind) {
      case "valid": return skipped[t.identifier]
        ? { group: "nothing", tone: "gray", label: "Not for the factory now", why: skipped[t.identifier] }
        : { group: "ready", tone: "green", label: "Ready to stage", why: v.reason };
      case "needs-clarification": return { group: "you", tone: "amber", label: "Needs your answer", why: v.reason };
      case "invalid-references": return { group: "you", tone: "amber", label: "Refers to something missing",
                                          why: `${v.target}: ${v.reason}` };
      case "already-done": return { group: "nothing", tone: "gray", label: "Already done", why: v.reason };
      case "stale": return { group: "nothing", tone: "gray", label: "No longer applies", why: v.reason };
      case "duplicate-of": return { group: "nothing", tone: "gray", label: `Duplicate of ${v.target}`, why: v.reason };
      default: return { group: "progress", tone: "blue", label: v.kind, why: v.reason };
    }
  }

  // ---- plain-language status for one dispatch ----------------------------------------------
  const STEP_LABEL = { draft: "Prepared", staged: "Ready to start", executing: "Being worked on",
                       done: "Finished", reconciled: "Written to Linear" };

  function dispatchStatus(d) {
    const cards = d.tickets || [];
    const n = (s) => cards.filter((c) => c.card_status === s).length;
    switch (d.state) {
      case "draft": return { tone: "gray", label: "Being prepared", why: `${cards.length} tickets` };
      case "staged": return { tone: "amber", label: "Waiting for you to start",
                              why: `${cards.length} tickets ready. Hand it off to start the work.` };
      case "executing": return { tone: "blue", label: "Being worked on",
                                 why: `${n("done")} of ${cards.length} done` + (n("blocked") ? `, ${n("blocked")} blocked` : "") };
      case "done": return { tone: "blue", label: "Finished, writing back to Linear",
                            why: `${n("done")} done, ${n("blocked")} blocked` };
      case "reconciled": return { tone: "green", label: "Written back to Linear", why: "Waiting to be archived" };
      default: return { tone: "gray", label: d.state, why: "" };
    }
  }

  // ---- small pieces ------------------------------------------------------------------------
  const Chip = ({ tone, children }) => h("span", { className: `chip ${tone}` }, children);

  function Evidence({ items }) {
    return h("ul", null, (items || []).map((e, i) => h("li", { key: i },
      h("strong", null, { file: "Code", sql: "Data", dagster: "Pipeline run", linear: "Ticket", pr: "Pull request" }[e.type] || e.type),
      ": ",
      e.path ? h("code", null, e.path) : e.url ? h("a", { href: e.url, target: "_blank" }, e.url)
        : e.ref ? e.ref : e.witness ? h("code", null, e.witness) : null,
      e.note ? ` — ${e.note}` : "")));
  }

  function TicketRow({ t, pick }) {
    const [open, setOpen] = useState(false);
    const s = t._s;
    return h("div", { className: "row", onClick: () => setOpen(!open) },
      h(Chip, { tone: s.tone }, s.label),
      h("div", null,
        h("div", { className: "title" },
          pick ? h("input", { type: "checkbox", className: "pick", checked: pick.checked, disabled: pick.disabled,
                              onClick: stop, onChange: pick.toggle, "aria-label": `Select ${t.identifier}` }) : null,
          t.title),
        h("div", { className: "meta" },
          h("a", { href: t.url, target: "_blank", onClick: stop }, t.identifier),
          ` · ${t.domain} · ${t.linear_state}` + (t.assignee ? ` · ${t.assignee.split("@")[0]}` : " · unassigned")),
        s.why ? h("div", { className: "why" }, s.why) : null,
        open ? h("div", { className: "detail" },
          t.verdict ? h("div", { className: "meta" },
            `Verified ${ago(t.verdict.created_at)} by ${t.verdict.created_by}` + (t.repo ? ` against ${t.repo}` : "")) : null,
          t.verdict ? h(Evidence, { items: t.verdict.evidence }) : h("div", { className: "meta" }, "No verification yet.")) : null));
  }

  function StageBar({ picked, max, onDone, clear }) {
    const a = useAction(() => { clear(); onDone(); });
    return h("div", { className: "act" },
      h(Button, { size: "sm", disabled: a.busy || !picked.length,
                  onClick: () => a.run("/stage", { identifiers: picked }) },
        a.busy ? `Staging ${picked.length}…` : `Stage selected (${picked.length} of max ${max})`),
      h(ActErr, { err: a.err }));
  }

  function HandoffButton({ d, onDone }) {
    const a = useAction(onDone);
    const go = () => window.confirm(`Hand off dispatch ${d.run_id}?\n\nThis starts real work on factory-fleet: ` +
      `branches, commits and pull requests for its ${(d.tickets || []).length} tickets.`) &&
      a.run("/handoff", { run_id: d.run_id });
    return h("div", { className: "act", onClick: stop },
      h(Button, { size: "sm", disabled: a.busy, onClick: go },
        a.busy ? "Handing off… (can take a few minutes)" : "Hand off"),
      h(ActErr, { err: a.err }));
  }

  const CARD_TONE = { ready: "gray", running: "blue", done: "green", blocked: "amber" };

  function DispatchRow({ d, onDone }) {
    const [open, setOpen] = useState(false);
    const s = dispatchStatus(d);
    return h("div", { className: "row", onClick: () => setOpen(!open) },
      h(Chip, { tone: s.tone }, s.label),
      h("div", null,
        h("div", { className: "title" }, `Dispatch ${d.run_id}`),
        h("div", { className: "why" }, s.why),
        d.state === "staged" ? h(HandoffButton, { d, onDone }) : null,
        h("ul", { className: "cards" }, (d.tickets || []).map((c) => h("li", { key: c.identifier },
          h(Chip, { tone: CARD_TONE[c.card_status] || "gray" }, CARD[c.card_status] || c.card_status), " ",
          h("strong", null, c.identifier),
          c.pr_url ? h("span", null, " · ", h("a", { href: c.pr_url, target: "_blank", onClick: stop },
            c.pr_url.replace("https://github.com/", ""))) : null))),
        open ? h("div", { className: "detail" },
          d.hash_ok === false ? h("div", { className: "meta" }, "WARNING: dispatch file was modified") : null,
          h("ul", null, (d.transitions || []).map((x, i) => h("li", { key: i, className: "meta" },
            `${STEP_LABEL[x.to_state] || x.to_state} — by ${x.actor}, ${ago(x.at)}`)))) : null));
  }

  function FlagRow({ f, onDone }) {
    const [text, setText] = useState("");
    const a = useAction(onDone);
    const go = () => text.trim() && a.run(`/flags/${f.id}/resolve`, { resolution: text.trim() });
    return h("div", { className: "row" }, h(Chip, { tone: "amber" }, FLAG_LABEL(f)),
      h("div", null,
        h("div", { className: "title" }, f.identifier
          ? h("span", null, h("a", { href: f.url, target: "_blank" }, f.identifier), `: ${f.title || ""}`) : f.kind),
        h("div", { className: "why" }, f.reason || ""),
        h("div", { className: "meta" }, `Flag ${f.id}` + (f.run_id ? ` · from ${f.run_id}` : "") +
          " · once handled, say what you decided (this never writes to Linear)"),
        h("div", { className: "act" },
          h("input", { type: "text", value: text, maxLength: 2000, placeholder: "What you decided",
                       disabled: a.busy, onChange: (e) => setText(e.target.value),
                       onKeyDown: (e) => { if (e.key === "Enter") go(); } }),
          h(Button, { size: "sm", disabled: a.busy || !text.trim(), onClick: go }, a.busy ? "Resolving…" : "Resolve"),
          h(ActErr, { err: a.err }))));
  }

  const CLOSED_LABEL = { "already-done": "Closed: already done", "duplicate-of": "Closed: duplicate" };

  function ClosedTicketRow({ w }) {
    return h("div", { className: "row" }, h(Chip, { tone: "green" }, CLOSED_LABEL[w.kind] || w.kind),
      h("div", null,
        h("div", { className: "title" }, w.title),
        h("div", { className: "meta" }, h("a", { href: w.url, target: "_blank" }, w.identifier),
          ` · now ${w.linear_state} in Linear` + (w.target ? ` · duplicate of ${w.target}` : "") + ` · ${w.run_id}`)));
  }

  function ArchivedRow({ a }) {
    return h("div", { className: "row" }, h(Chip, { tone: "green" }, "Archived"),
      h("div", null,
        h("div", { className: "title" }, `Dispatch ${a.run_id}`),
        h("div", { className: "meta" }, `${a.tickets} · ${a.done} done` + (a.blocked ? `, ${a.blocked} blocked` : "") +
          ` · archived ${ago(a.archived_at)}`)));
  }

  function jobLine(jobs) {
    const names = { "factory-ingest": "Linear sync", "factory-prune": "Verification", "factory-reconcile": "Write-back" };
    const bad = jobs.filter((j) => j.last_status && !["ok", "success", "succeeded"].includes(j.last_status));
    const parts = jobs.map((j) => `${names[j.name] || j.name} ${epochAgo(j.last_run_at)}`);
    return { ok: bad.length === 0, text: (bad.length ? bad.map((j) => `${names[j.name] || j.name} failed: ${j.last_error || j.last_status}`).join(" · ")
                                                     : "All jobs healthy") + " · last runs: " + parts.join(", ") };
  }

  // ---- throughput ----------------------------------------------------------------------------
  const pct = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
  const hours = (v) => (v == null ? "—" : v < 48 ? `${v.toFixed(1)} h` : `${(v / 24).toFixed(1)} days`);
  const Stat = ({ k, v }) => h("div", { className: "stat" }, h("div", { className: "v" }, v), h("div", { className: "k" }, k));
  const Table = ({ head, rows }) => h("table", { className: "tbl" },
    h("thead", null, h("tr", null, head.map((c, i) => h("th", { key: i, className: i ? "r" : "" }, c)))),
    h("tbody", null, rows.map((r, j) => h("tr", { key: j }, r.map((c, i) => h("td", { key: i, className: i ? "r" : "" }, c))))));

  function Throughput({ stamp }) {
    const [m, setM] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => {
      SDK.fetchJSON(`${API}/metrics?days=28`).then((d) => { setM(d); setErr(null); }, (e) => setErr(errText(e)));
    }, [stamp]);
    if (!m) return err ? h("div", { className: "err" }, `Throughput unavailable: ${err}`) : h("div", { className: "empty" }, "Loading…");
    const verdicts = Object.entries(m.verdicts || {}).map(([k, n]) => `${k} ${n}`).join(" · ");
    return h("div", null,
      err ? h("div", { className: "act-err" }, `Last refresh failed: ${err}`) : null,
      h("div", { className: "stats" },
        h(Stat, { k: "tickets done", v: m.tickets.done }),
        h(Stat, { k: "tickets blocked", v: m.tickets.blocked }),
        h(Stat, { k: "block rate", v: pct(m.block_rate) }),
        h(Stat, { k: "stage → done, median", v: hours(m.hours.stage_to_done_p50) }),
        h(Stat, { k: "stage → done, slowest", v: hours(m.hours.stage_to_done_max) }),
        h(Stat, { k: "done → archived, median", v: hours(m.hours.done_to_archived_p50) })),
      h("div", { className: "sub" }, `Dispatches: ${m.dispatches.staged} staged, ${m.dispatches.archived} archived · ` +
        `Linear writes: ${m.writeback.confirmed} confirmed, ${m.writeback.failed} failed, ${m.writeback.flagged} flagged` +
        (verdicts ? ` · Verdicts: ${verdicts}` : "")),
      m.per_week.length ? h(Table, { head: ["Week of", "Staged", "Done", "Blocked"],
                                     rows: m.per_week.map((w) => [w.week, w.staged, w.done, w.blocked]) }) : null,
      m.per_repo.length ? h(Table, { head: ["Repo", "Done", "Blocked"],
                                     rows: m.per_repo.map((r) => [r.repo, r.done, r.blocked]) }) : null);
  }

  // ---- page --------------------------------------------------------------------------------
  // One section per lifecycle stage, in order. `tone` colors the stage pill in the strip.
  const STAGES = [
    { key: "checking", title: "Checking", tone: "blue", hint: "Verifying the ticket against code and data.",
      empty: "Nothing being verified." },
    { key: "answer", title: "Needs your answer", tone: "amber", hint: "The check couldn't decide; fix or answer in Linear.",
      empty: "No questions for you." },
    { key: "ready", title: "Ready to stage", tone: "green", hint: "Verified and free. Tick tickets and stage them.",
      empty: "No verified tickets waiting. New verifications land every 20 minutes." },
    { key: "staged", title: "Staged", tone: "amber", hint: "Frozen into a dispatch; waiting for your handoff.",
      empty: "No dispatch waiting to start." },
    { key: "working", title: "Being worked on", tone: "blue", hint: "factory-fleet is building it.",
      empty: "No dispatch running." },
    { key: "writeback", title: "Writing back", tone: "blue", hint: "Finished; results going to Linear. Held writes need you.",
      empty: "Nothing waiting to be written to Linear." },
    { key: "closed", title: "Closed", tone: "green", hint: "Written to Linear and archived.", empty: "Nothing closed yet." },
  ];
  const DISPATCH_STAGE = { draft: "staged", staged: "staged", executing: "working", done: "writeback", reconciled: "writeback" };
  const TICKET_STAGE = { progress: "checking", you: "answer", ready: "ready" };

  function FactoryPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loadedAt, setLoadedAt] = useState(null);
    const [picked, setPicked] = useState([]);
    const load = useCallback(() => {
      SDK.fetchJSON(`${API}/overview`)
        .then((d) => { setData(d); setError(null); setLoadedAt(new Date().toISOString()); })
        .catch((e) => setError(errText(e)));
    }, []);
    useEffect(() => { load(); const t = setInterval(load, REFRESH_MS); return () => clearInterval(t); }, [load]);

    if (!data) return h("div", { className: "fx" }, error ? h("div", { className: "err" }, error) : h("div", { className: "empty" }, "Loading…"));

    const max = data.candidates?.max_tickets || 0;
    const stageable = new Set((data.candidates?.candidates || []).map((c) => c.identifier));
    const sel = picked.filter((i) => stageable.has(i));  // drop picks that stopped being candidates
    const pickFor = (id) => stageable.has(id) ? {
      checked: sel.includes(id), disabled: !sel.includes(id) && sel.length >= max,
      toggle: () => setPicked(sel.includes(id) ? sel.filter((i) => i !== id) : [...sel, id]),
    } : null;

    const rows = Object.fromEntries(STAGES.map((s) => [s.key, []]));
    const aside = [];
    const skipped = Object.fromEntries((data.candidates?.skipped || []).map((x) => [x.identifier, x.reason]));
    data.tickets.forEach((t) => {
      t._s = ticketStatus(t, skipped);
      if (t.dispatch) return;  // shown on its dispatch's card list
      const stage = TICKET_STAGE[t._s.group];
      (stage ? rows[stage] : aside).push(h(TicketRow, { key: t.identifier, t,
                                                        pick: stage === "ready" ? pickFor(t.identifier) : null }));
    });
    data.dispatches.forEach((d) => rows[DISPATCH_STAGE[d.state] || "working"].push(h(DispatchRow, { key: d.run_id, d, onDone: load })));
    (data.status.open_flags || []).forEach((f) => rows.writeback.unshift(h(FlagRow, { key: `f${f.id}`, f, onDone: load })));
    (data.status.archived || []).forEach((a) => rows.closed.push(h(ArchivedRow, { key: a.run_id, a })));
    (data.status.written_back || []).forEach((w) => rows.closed.push(h(ClosedTicketRow, { key: `w${w.identifier}`, w })));

    const flags = (data.status.open_flags || []).length;
    const needYou = flags + rows.answer.length + rows.staged.length;
    const jl = jobLine(data.jobs);
    const go = (key) => document.getElementById(`fx-${key}`)?.scrollIntoView({ behavior: "smooth", block: "start" });

    return h("div", { className: "fx" },
      h("div", { className: "bar" },
        h("div", null,
          h("div", { className: "summary" },
            needYou ? `${needYou} thing${needYou > 1 ? "s" : ""} need you` : "Nothing needs you right now"),
          h("div", { className: "sub" }, `${data.tickets.length} tickets in your domains · ${data.status.ignored_other_leads} in other leads' domains are ignored`),
          h("div", { className: "sub", style: { color: jl.ok ? undefined : "#ef4444" } }, jl.text)),
        h(Button, { variant: "outline", size: "sm", onClick: load }, `Refresh · ${loadedAt ? ago(loadedAt) : ""}`)),
      error ? h("div", { className: "err" }, `Last refresh failed: ${error}`) : null,

      h("nav", { className: "pipeline" }, STAGES.map((s, i) => h(React.Fragment, { key: s.key },
        i ? h("span", { className: "arrow" }, "→") : null,
        h("button", { className: `stage ${rows[s.key].length ? s.tone : "idle"}`, onClick: () => go(s.key) },
          h("span", { className: "count" }, rows[s.key].length), s.title)))),

      STAGES.map((s, i) => h("section", { key: s.key, id: `fx-${s.key}` },
        h("h2", null, h("span", { className: "num" }, i + 1), s.title,
          h("span", { className: "n" }, `(${rows[s.key].length})`), h("span", { className: "hint" }, s.hint)),
        s.key === "ready" && stageable.size
          ? h(StageBar, { picked: sel, max, onDone: load, clear: () => setPicked([]) }) : null,
        rows[s.key].length ? h("div", { className: "list" }, rows[s.key]) : h("div", { className: "empty" }, s.empty))),

      h("section", { id: "fx-throughput" },
        h("h2", null, "Throughput", h("span", { className: "hint" }, "Last 28 days.")),
        h(Throughput, { stamp: loadedAt })),

      h("details", null, h("summary", null, `Not for the factory (${aside.length}): already handled, held elsewhere, or not mapped`),
        h("div", { className: "list" }, aside)));
  }

  window.__HERMES_PLUGINS__.register("factory", FactoryPage);
})();

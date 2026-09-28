// Factory status tab (read-only). Plain words first; ids, hashes and evidence one click away.
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

  // ---- plain-language status for one ticket ------------------------------------------------
  const RECHECK = {
    new: "Not verified yet",
    "ticket-changed": "Ticket changed in Linear since it was verified",
    "evidence-changed": "Code it was verified against has changed",
    "context-changed": "Repo mapping changed; verifying again",
  };
  const CARD = { ready: "not started", running: "being worked on", done: "done", blocked: "blocked" };

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
  const STEPS = ["draft", "staged", "executing", "done", "reconciled"];
  const STEP_LABEL = { draft: "Prepared", staged: "Ready to start", executing: "Being worked on",
                       done: "Finished", reconciled: "Written to Linear" };

  function dispatchStatus(d) {
    const cards = d.tickets || [];
    const n = (s) => cards.filter((c) => c.card_status === s).length;
    switch (d.state) {
      case "draft": return { tone: "gray", label: "Being prepared", why: `${cards.length} tickets` };
      case "staged": return { tone: "amber", label: "Waiting for you to start",
                              why: `${cards.length} tickets ready. Start it from the factory chat (hermes -p factory): "hand off ${d.run_id}"` };
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

  function Section({ title, count, empty, children }) {
    return h("section", null,
      h("h2", null, title, count != null ? h("span", { className: "n" }, `(${count})`) : null),
      count === 0 ? h("div", { className: "empty" }, empty) : h("div", { className: "list" }, children));
  }

  function Evidence({ items }) {
    return h("ul", null, (items || []).map((e, i) => h("li", { key: i },
      h("strong", null, { file: "Code", sql: "Data", dagster: "Pipeline run", linear: "Ticket", pr: "Pull request" }[e.type] || e.type),
      ": ",
      e.path ? h("code", null, e.path) : e.url ? h("a", { href: e.url, target: "_blank" }, e.url)
        : e.ref ? e.ref : e.witness ? h("code", null, e.witness) : null,
      e.note ? ` — ${e.note}` : "")));
  }

  function TicketRow({ t }) {
    const [open, setOpen] = useState(false);
    const s = t._s;
    return h("div", { className: "row", onClick: () => setOpen(!open) },
      h(Chip, { tone: s.tone }, s.label),
      h("div", null,
        h("div", { className: "title" }, t.title),
        h("div", { className: "meta" },
          h("a", { href: t.url, target: "_blank", onClick: (e) => e.stopPropagation() }, t.identifier),
          ` · ${t.domain} · ${t.linear_state}` + (t.assignee ? ` · ${t.assignee.split("@")[0]}` : " · unassigned")),
        s.why ? h("div", { className: "why" }, s.why) : null,
        open ? h("div", { className: "detail" },
          t.verdict ? h("div", { className: "meta" },
            `Verified ${ago(t.verdict.created_at)} by ${t.verdict.created_by}` + (t.repo ? ` against ${t.repo}` : "")) : null,
          t.verdict ? h(Evidence, { items: t.verdict.evidence }) : h("div", { className: "meta" }, "No verification yet.")) : null));
  }

  function DispatchRow({ d }) {
    const [open, setOpen] = useState(false);
    const s = dispatchStatus(d);
    return h("div", { className: "row", onClick: () => setOpen(!open) },
      h(Chip, { tone: s.tone }, s.label),
      h("div", null,
        h("div", { className: "title" }, `${(d.tickets || []).length} tickets · ${(d.tickets || []).map((c) => c.identifier).join(", ")}`),
        h("div", { className: "why" }, s.why),
        h("div", { className: "steps" }, STEPS.map((st) =>
          h("span", { key: st, className: "step" + (st === d.state ? " on" : "") }, STEP_LABEL[st]))),
        open ? h("div", { className: "detail" },
          h("div", { className: "meta" }, `Run ${d.run_id}` + (d.hash_ok === false ? " · WARNING: dispatch file was modified" : "")),
          h("ul", null, (d.tickets || []).map((c) => h("li", { key: c.identifier },
            `${c.identifier}: ${CARD[c.card_status] || c.card_status}`,
            c.pr_url ? h("span", null, " · ", h("a", { href: c.pr_url, target: "_blank" }, "PR")) : null))),
          h("ul", null, (d.transitions || []).map((x, i) => h("li", { key: i, className: "meta" },
            `${STEP_LABEL[x.to_state] || x.to_state} — by ${x.actor}, ${ago(x.at)}`)))) : null));
  }

  function jobLine(jobs) {
    const names = { "factory-ingest": "Linear sync", "factory-prune": "Verification", "factory-reconcile": "Write-back" };
    const bad = jobs.filter((j) => j.last_status && !["ok", "success", "succeeded"].includes(j.last_status));
    const parts = jobs.map((j) => `${names[j.name] || j.name} ${epochAgo(j.last_run_at)}`);
    return { ok: bad.length === 0, text: (bad.length ? bad.map((j) => `${names[j.name] || j.name} failed: ${j.last_error || j.last_status}`).join(" · ")
                                                     : "All jobs healthy") + " · last runs: " + parts.join(", ") };
  }

  // ---- page --------------------------------------------------------------------------------
  function FactoryPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loadedAt, setLoadedAt] = useState(null);
    const load = useCallback(() => {
      SDK.fetchJSON("/api/plugins/factory/overview")
        .then((d) => { setData(d); setError(null); setLoadedAt(new Date().toISOString()); })
        .catch((e) => setError(String(e && e.message ? e.message : e)));
    }, []);
    useEffect(() => { load(); const t = setInterval(load, REFRESH_MS); return () => clearInterval(t); }, [load]);

    if (!data) return h("div", { className: "fx" }, error ? h("div", { className: "err" }, error) : h("div", { className: "empty" }, "Loading…"));

    const groups = { you: [], ready: [], progress: [], nothing: [], ignored: [] };
    const skipped = Object.fromEntries((data.candidates?.skipped || []).map((x) => [x.identifier, x.reason]));
    data.tickets.forEach((t) => { t._s = ticketStatus(t, skipped); groups[t._s.group].push(t); });
    const flags = data.status.open_flags || [];
    const waiting = data.dispatches.filter((d) => d.state === "staged");
    const active = data.dispatches.filter((d) => d.state !== "staged");
    const needYou = flags.length + waiting.length + groups.you.length;
    const jl = jobLine(data.jobs);

    return h("div", { className: "fx" },
      h("div", { className: "bar" },
        h("div", null,
          h("div", { className: "summary" },
            needYou ? `${needYou} thing${needYou > 1 ? "s" : ""} need you` : "Nothing needs you right now",
            ` · ${groups.ready.length} ready to stage · ` +
            (active.some((d) => d.state === "executing") ? "a dispatch is being worked on" : "no dispatch running")),
          h("div", { className: "sub" }, `${data.tickets.length} tickets in your domains · ${data.status.ignored_other_leads} in other leads' domains are ignored`),
          h("div", { className: "sub", style: { color: jl.ok ? undefined : "#ef4444" } }, jl.text)),
        h(Button, { variant: "outline", size: "sm", onClick: load }, `Refresh · ${loadedAt ? ago(loadedAt) : ""}`)),
      error ? h("div", { className: "err" }, `Last refresh failed: ${error}`) : null,

      h(Section, { title: "Needs you", count: needYou, empty: "Nothing is waiting on you." },
        flags.map((f) => h("div", { className: "row", key: `f${f.id}` }, h(Chip, { tone: "amber" }, "Decide"),
          h("div", null, h("div", { className: "title" }, `${f.kind.replace(/-/g, " ")}${f.issue_id ? "" : ""}`),
            h("div", { className: "meta" }, f.run_id ? `from dispatch ${f.run_id}` : "")))),
        waiting.map((d) => h(DispatchRow, { key: d.run_id, d })),
        groups.you.map((t) => h(TicketRow, { key: t.identifier, t }))),

      h(Section, { title: "Dispatches", count: active.length, empty: "No dispatch in progress." },
        active.map((d) => h(DispatchRow, { key: d.run_id, d }))),

      h(Section, { title: "Ready to stage", count: groups.ready.length,
                   empty: "No verified tickets waiting. New verifications land every 20 minutes." },
        groups.ready.map((t) => h(TicketRow, { key: t.identifier, t }))),

      h(Section, { title: "Being checked", count: groups.progress.length, empty: "Nothing being checked." },
        groups.progress.map((t) => h(TicketRow, { key: t.identifier, t }))),

      h("details", null, h("summary", null, `Nothing to do (${groups.nothing.length}) · Not mapped (${groups.ignored.length})`),
        h("div", { className: "list" }, groups.nothing.concat(groups.ignored).map((t) => h(TicketRow, { key: t.identifier, t })))));
  }

  window.__HERMES_PLUGINS__.register("factory", FactoryPage);
})();

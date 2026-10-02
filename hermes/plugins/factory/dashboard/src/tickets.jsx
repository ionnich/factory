// Ticket workspaces: Tickets (the whole ledger) and its Verify and Draft slices. `GET /tickets` lists every ticket in
// scope or ever touched, fetched while the workspace is active and again on each overview refresh. The server's `phase`
// decides the slice (mode verify: phase verify, mode draft: phase draft, mode tickets: every row); filters narrow it by
// the server's `group`, newest activity first. Only Draft picks ready tickets. A row opens a bottom sheet with the
// ticket's current verdict and evidence, and its timeline (`/tickets/{id}/timeline`): everything the factory saw and
// did, oldest first.
//
// Draft no longer stages a dispatch itself: picking tickets hands them to Strategy (?stage=strategy) pre-selected,
// where grooming (real DeepSeek) turns them into a published, approved brief before anything can be staged. The ticket
// audit stays here.
//
// <TicketsTab data mode active view onViewChange onDone onNavigate />: data is the overview; mode tickets|verify|draft;
//   active false = no fetch and no sheet (default true). view {q, filter, onlyMine, picked, open} is the parent's, one per mode,
//   so a workspace keeps it while unmounted. onViewChange is that mode's React-style setter; this file only passes
//   updaters (latest view) => next view, so a call that lands after its workspace moved on or unmounted patches only
//   picked. onNavigate({stage, run?, ticket?, sources?}): the parent owns history and the pane. A row opens with
//   {stage: mode, ticket}, the sheet closes with {stage: mode, ticket: null}, and a Draft handoff goes to
//   {stage: "strategy", sources} with the picked identifiers.
//
// ticket row: {identifier, title, url, phase (tickets|verify|draft, or its live dispatch's phase), group: ready|answer|
//   stale|dispatch|done|not, domain?, assignee?, linear_state, state_type, in_scope?, in_review?, owned?, context?,
//   unmapped_reason?, freshness?, verdict?: {kind, target, reason}, dispatch?: {run_id, state, card_status?, pr_url?},
//   last_at?}. Absent keys mean null/false (the server drops them).
// timeline event: {at, kind: linear|own-write|verdict|dispatch|note|decision|card|writeback, actor, summary, detail?}.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect } = SDK.hooks;
const { Button, Badge, Card, CardContent, Input } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;
const API = "/api/plugins/factory";

// Small helpers index.jsx also has; repeated here so this file stands alone.
const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "never");
const errText = (e) => String(e && e.message ? e.message : e);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const localTime = (iso) => new Date(iso).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
const stop = (e) => e.stopPropagation();
const BADGE = { amber: "warning", green: "success", blue: "secondary", gray: "outline", red: "destructive" };
const Tone = ({ tone, children }) => <Badge tone={BADGE[tone] || "outline"}>{children}</Badge>;
const Ext = ({ href, children }) => <a className="fx-link" href={href} target="_blank" rel="noreferrer" onClick={stop}>{children}</a>;

const CARD = { ready: "not started", running: "in progress", done: "done", blocked: "blocked" };
const RECHECK = { new: "Not verified yet", "ticket-changed": "Changed in Linear since it was verified",
                  "evidence-changed": "The code it was verified against changed", "context-changed": "Repo mapping changed",
                  aged: "Verified over a week ago" };
const VERDICT_TONE = { valid: "green", "needs-clarification": "amber", "invalid-references": "amber" };
// Lifecycle stage a ticket is in (the server's `phase`); "tickets" = only in the ledger, no badge.
const PHASE = { verify: "Verify", draft: "Draft", plan: "Plan", review: "Review", run: "Run", reconcile: "Reconcile",
                archive: "Archive" };
const phaseOf = (t) => (t.phase && t.phase !== "tickets" ? PHASE[t.phase] || t.phase : null);
// Each workspace's filters over its slice: [server `group` or "all", label, what an empty list means].
const FILTERS = {
  tickets: [["all", "All", "No ticket in scope or touched yet."], ["ready", "Ready", "Nothing verified and free right now."],
            ["answer", "Needs answer", "No questions from verification."], ["stale", "Stale", "Every verdict is current."],
            ["dispatch", "In dispatch", "No ticket is in a live dispatch."], ["done", "Done", "Nothing done yet."],
            ["not", "Not ours", "Nothing set aside."]],
  verify: [["all", "All", "Nothing waits on verification or an answer."],
           ["stale", "To verify", "Nothing is unverified or stale."],
           ["answer", "Needs answer", "No questions from verification."]],
  draft: [["all", "All", "Nothing verified and free right now, and no draft waits to be offered to a planner."],
          ["ready", "Ready", "Nothing verified and free right now."],
          ["dispatch", "In a draft", "No ticket is in a draft not yet offered to a planner."]],
};

// Why a row sits in its group (the server picks the group: cli.py `group`); a group this file doesn't know gets none.
function whyOf(t, skipped) {
  const v = t.verdict, d = t.dispatch;
  switch (t.group) {
    case "dispatch": return [d?.run_id, d?.state, d?.card_status && `card ${CARD[d.card_status] || d.card_status}`].filter(Boolean).join(" · ");
    case "done": return t.in_review ? `${t.linear_state}: waits on a person` : `${t.linear_state} in Linear`;
    case "stale": return RECHECK[t.freshness] || "Queued for verification";
    case "ready": return v?.reason;
    case "answer": return v?.kind === "invalid-references" ? `${v.target}: ${v.reason}` : v?.reason;
    case "not": break;
    default: return null;
  }
  if (!t.owned) return t.domain ? `${t.domain}: another lead's Domain` : "No Domain: line";
  if (!t.in_scope) return `${t.linear_state}: out of scope in Linear`;
  if (!t.context) return t.unmapped_reason || "No repo is mapped for this domain";
  if (skipped[t.identifier]) return skipped[t.identifier];
  if (!v) return "No verdict yet";
  return `${v.kind === "duplicate-of" ? `Duplicate of ${v.target}` : v.kind.replace("-", " ")}: ${v.reason}`;
}

function Row({ t, why, phase, pick, onOpen }) {
  const v = t.verdict, d = t.dispatch;
  return (
    <div className={`fx-trow${pick?.checked ? " picked" : ""}`} onClick={onOpen} role="button" tabIndex={0}
         onKeyDown={(e) => e.key === "Enter" && e.target === e.currentTarget && onOpen()}>
      {pick ? <label className="fx-check-target" onClick={stop}><input type="checkbox" className="fx-pick" checked={pick.checked} disabled={pick.disabled} onChange={pick.toggle} aria-label={`Select ${t.identifier}`} /></label> : null}
      <div className="fx-grow">
        <div className="fx-row fx-row-meta"><span className="fx-id">{t.identifier}</span><span className="fx-hint">{t.linear_state}</span>
          <span className="fx-grow" /><span className="fx-hint">{ago(t.last_at)}</span></div>
        <div className="fx-row-title fx-ttitle">{t.title}</div>
        <div className="fx-row fx-row-status">
          {phase ? <Tone tone="blue">{phase}</Tone> : null}
          {v ? <Tone tone={VERDICT_TONE[v.kind] || "gray"}>{v.kind}</Tone> : null}
          {t.freshness && t.freshness !== "fresh" ? <Tone tone="amber">{t.freshness}</Tone> : null}
          {d ? <span className="fx-hint">{[d.run_id, CARD[d.card_status] || d.card_status].filter(Boolean).join(" · ")}</span> : null}
          {t.domain ? <span className="fx-hint">{t.domain}</span> : null}
        </div>
        {why ? <div className="fx-hint clamp">{why}</div> : null}
      </div>
    </div>
  );
}

const Evidence = ({ e }) => (
  <li>
    {e.type === "file" ? <code>{e.path}{e.line ? `:${e.line}` : ""}</code>
      : e.type === "sql" || e.type === "dagster" ? <code>{e.witness} · witness #{e.witness_log_id}</code>
      : e.type === "pr" ? <Ext href={e.url}>{e.url}</Ext>
      : <code>{e.ref}</code>}
    {e.note ? <div className="fx-hint">{e.note}</div> : null}
  </li>
);

const EVENT = { linear: ["Linear", "gray"], "own-write": ["Our write", "blue"], verdict: ["Verdict", "amber"],
                dispatch: ["Dispatch", "blue"], note: ["Note", "gray"], decision: ["Decision", "amber"],
                card: ["Card", "green"], writeback: ["Write-back", "green"] };

function Event({ e }) {
  const [open, setOpen] = useState(false);
  const [label, tone] = EVENT[e.kind] || [e.kind, "gray"];
  const more = e.detail && (e.kind === "verdict" || e.kind === "writeback") ? e.detail.reason : null;
  return (
    <li className="fx-ev" onClick={() => setOpen(!open)}>
      <div className="fx-row"><Tone tone={e.detail?.superseded_at ? "gray" : tone}>{label}</Tone>
        <span className="fx-hint">{e.at ? localTime(e.at) : "?"} · {e.actor}</span></div>
      <div className={open ? "fx-ev-body" : "fx-ev-body clamp"}>{e.summary}</div>
      {more ? <div className={`fx-hint${open ? "" : " clamp"}`}>{more}</div> : null}
    </li>
  );
}

function Sheet({ t, onClose }) {
  const [tl, setTl] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {  // refetched when the ledger says the ticket moved; a reply to an older fetch is dropped
    let live = true;
    SDK.fetchJSON(`${API}/tickets/${t.identifier}/timeline`)
      .then((x) => { if (live) { setTl(x); setErr(null); } }, (e) => { if (live) setErr(errText(e)); });
    return () => { live = false; };
  }, [t.identifier, t.last_at]);
  useEffect(() => {
    const k = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const cur = (tl || []).filter((e) => e.kind === "verdict" && !e.detail.superseded_at).pop()?.detail;
  const d = t.dispatch, phase = phaseOf(t);
  return (
    <div className="fx-sheet-bg" onClick={onClose}>
      <div className="fx-sheet" role="dialog" aria-modal="true" aria-label={t.identifier} onClick={stop}>
        <div className="fx-row between">
          <div className="fx-row"><Ext href={t.url}>{t.identifier}</Ext><Tone tone="gray">{t.linear_state}</Tone>
            {phase ? <Tone tone="blue">{phase}</Tone> : null}</div>
          <Button size="sm" ghost onClick={onClose} aria-label="Close">✕</Button>
        </div>
        <div className="fx-title small">{t.title}</div>
        <div className="fx-hint">{[t.domain || "no Domain", t.assignee || "unassigned", d && `${d.run_id} (${d.state})`,
          d?.pr_url].filter(Boolean).join(" · ")}</div>
        {cur ? (
          <div className="fx-stack-v fx-verdict">
            <div className="fx-row"><span className="fx-k">Verdict</span>
              <Tone tone={VERDICT_TONE[cur.kind] || "gray"}>{cur.kind}{cur.target ? ` ${cur.target}` : ""}</Tone>
              {cur.repo ? <span className="fx-hint">{cur.repo}{cur.trunk_sha ? `@${cur.trunk_sha.slice(0, 8)}` : ""}</span> : null}</div>
            <div className="fx-why">{cur.reason}</div>
            <ul className="fx-evidence">{(cur.evidence || []).map((e, i) => <Evidence key={i} e={e} />)}</ul>
          </div>
        ) : null}
        <div className="fx-k">Timeline</div>
        {err ? <div className="fx-err">{err}</div> : !tl ? <div className="fx-hint">Loading…</div>
          : <ol className="fx-timeline">{tl.map((e, i) => <Event key={i} e={e} />)}</ol>}
      </div>
    </div>
  );
}

export function TicketsTab({ data, mode, active = true, view, onViewChange, onDone, onNavigate }) {
  const filters = FILTERS[mode];
  const q = view?.q || "", picked = view?.picked || [], open = view?.open || null;
  const filter = filters.some(([k]) => k === view?.filter) ? view.filter : "all";
  const onlyMine = mode === "tickets" && !!view?.onlyMine;
  const update = (patch) => onViewChange((v) => ({ ...v, ...patch }));  // lands on the view as it is by then
  const [all, setAll] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  useEffect(() => {  // while active: now and on each overview refresh (`data` is a new object every time)
    if (!active) return;
    let live = true;  // a reply that lands after unmount, a mode change or a newer fetch is dropped
    SDK.fetchJSON(`${API}/tickets`).then((x) => { if (live) { setAll(x); setLoadErr(null); } },
                                          (e) => { if (live) setLoadErr(errText(e)); });
    return () => { live = false; };
  }, [data, active, mode]);
  const cands = data.candidates || {};
  const max = cands.max_tickets || 0;
  const stageable = (cands.candidates || []).map((c) => c.identifier);
  const suggested = (cands.suggested || []).filter((i) => stageable.includes(i));
  const sel = picked.filter((i) => stageable.includes(i));
  const skipped = Object.fromEntries((cands.skipped || []).map((x) => [x.identifier, x.reason]));
  const titles = Object.fromEntries((cands.candidates || []).map((c) => [c.identifier, c.title]));
  // The server's phase says which workspace a ticket is in; Tickets is the whole ledger.
  const rows = (all || []).filter((t) => mode === "tickets" || t.phase === mode)
    .filter((t) => !onlyMine || (t.assignee && t.assignee === data.status?.lead))
    .map((t) => ({ t, why: whyOf(t, skipped) }))
    .sort((a, b) => (Date.parse(b.t.last_at) || 0) - (Date.parse(a.t.last_at) || 0));
  const within = (k) => (k === "all" ? rows : rows.filter(({ t }) => t.group === k));
  const needle = q.trim().toLowerCase();
  // A search looks through this whole workspace, whatever the filter, and never past it.
  const list = needle ? rows.filter(({ t }) => `${t.identifier} ${t.title || ""}`.toLowerCase().includes(needle)) : within(filter);
  const suggest = mode === "draft" && !needle && filter !== "dispatch" && suggested.length > 0;  // Next dispatch card
  // Draft no longer creates an unreviewed dispatch: raw candidates go to Strategy, where grooming (DeepSeek) turns
  // them into a published, approved brief before anything is staged. The selection is handed over pre-picked.
  const handoff = (ids) => {
    update({ picked: [] });
    onNavigate({ stage: "strategy", sources: ids });
  };
  // History first, then the view: the entry the sheet opened from keeps its own URL.
  const setOpen = (id) => { onNavigate({ stage: mode, ticket: id }); update({ open: id }); };
  const current = open && (all || []).find((t) => t.identifier === open);  // whole ledger: a link may outlive the slice
  return (
    <>
      <Input className="fx-search" type="search" placeholder="Search id or title" value={q} onChange={(e) => update({ q: e.target.value })} />
      {mode === "tickets" ? <div className="fx-chips" role="group" aria-label="Ticket assignee filter">
        <button className={`fx-chip${onlyMine ? " on" : ""}`} aria-pressed={onlyMine}
                onClick={() => update({ onlyMine: !onlyMine })}>Only mine</button>
      </div> : null}
      <div className="fx-chips" role="group" aria-label="Ticket filter">
        {filters.map(([k, label]) => (
          <button key={k} aria-pressed={!needle && filter === k} className={`fx-chip${!needle && filter === k ? " on" : ""}`}
                  onClick={() => update({ filter: k, q: "" })}>{label} {all ? <span className="fx-count">{within(k).length}</span> : null}</button>
        ))}
      </div>
      {mode === "draft" ? <div className="fx-hint">{plural(stageable.length, "ticket")} ready to draft · up to {max} per dispatch</div> : null}
      {suggest ? (
        <Card className="fx-card fx-suggest"><CardContent className="fx-stack-v">
          <div className="fx-row between"><span className="fx-k">Next dispatch</span><span className="fx-hint">★ recommended</span></div>
          <div>{suggested.map((i) => <div key={i} className="fx-ttitle clamp"><span className="fx-id">{i}</span> {titles[i]}</div>)}</div>
          <div className="fx-hint">The factory would group these next: same Domain, then same repo. They are groomed into an approved brief in Strategy before anything is drafted; you review that before anything runs.</div>
          <div className="fx-row"><Button size="sm" onClick={() => handoff(suggested)}>★ Groom these {suggested.length} in Strategy</Button>
            <span className="fx-hint">or tick your own below</span></div>
        </CardContent></Card>
      ) : null}
      {loadErr ? <div className="fx-err" role="alert">{all ? `Refreshing tickets failed: ${loadErr}. Showing the last loaded list.` : `Tickets did not load: ${loadErr}`}</div> : null}
      {active && open && all && !current ? (
        <div className="fx-row between"><span className="fx-empty">{open} is not in the ticket ledger.</span>
          <button className="fx-x" onClick={() => setOpen(null)} aria-label="Close">✕</button></div>
      ) : null}
      <div className="fx-list">
        {!all ? (loadErr ? null : <div className="fx-hint">Loading…</div>) : list.length ? list.map(({ t, why }) => (
          <Row key={t.identifier} t={t} why={why} phase={mode === "tickets" ? phaseOf(t) : null} onOpen={() => setOpen(t.identifier)}
               pick={mode === "draft" && t.group === "ready" && stageable.includes(t.identifier) ? {
            checked: sel.includes(t.identifier), disabled: !sel.includes(t.identifier) && sel.length >= max,
            toggle: () => update({ picked: sel.includes(t.identifier) ? sel.filter((i) => i !== t.identifier) : [...sel, t.identifier] }),
          } : null} />
        )) : <div className="fx-empty">{onlyMine ? "No tickets assigned to you match this filter." :
          needle ? "No ticket here matches." : filters.find(([k]) => k === filter)[2]}</div>}
      </div>
      {mode === "draft" && sel.length ? (
        <div className="fx-draftbar">
          <span><b>{sel.length}</b> {sel.length === 1 ? "ticket" : "tickets"} picked</span><span className="fx-grow" />
          <Button size="sm" ghost onClick={() => update({ picked: [] })}>Clear</Button>
          <Button size="sm" onClick={() => handoff(sel)}>Groom in Strategy</Button>
        </div>
      ) : null}
      {active && current ? <Sheet key={current.identifier} t={current} onClose={() => setOpen(null)} /> : null}
    </>
  );
}

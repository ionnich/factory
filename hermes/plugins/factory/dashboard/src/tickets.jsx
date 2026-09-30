// Tickets tab: every ticket in scope or ever touched (overview `all_tickets`), filtered by what it needs, newest
// activity first. Ready tickets can be picked into a draft. A row opens a bottom sheet with the ticket's current
// verdict and evidence, and its timeline (`/tickets/{id}/timeline`): everything the factory saw and did, oldest first.
//
// all_tickets row: {identifier, title, url, domain?, assignee?, linear_state, state_type, in_scope?, in_review?, owned?,
//   context?, unmapped_reason?, freshness?, verdict?: {kind, target, reason}, dispatch?: {run_id, state, card_status,
//   pr_url?}, last_at?}. Absent keys mean null/false (the server drops them to keep the overview small).
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
const FILTERS = [["ready", "Ready"], ["answer", "Needs answer"], ["stale", "Stale"], ["dispatch", "In dispatch"],
                 ["done", "Done"], ["not", "Not ours"]];
const EMPTY = { ready: "Nothing verified and free right now.", answer: "No questions from verification.",
                stale: "Every verdict is current.", dispatch: "No ticket is in a live dispatch.", done: "Nothing done yet.",
                not: "Nothing set aside." };

export function groupOf(t, skipped) {
  const v = t.verdict, d = t.dispatch;
  if (d && d.state !== "archived") return { group: "dispatch", why: `${d.run_id} · ${d.state} · card ${CARD[d.card_status]}` };
  if (!t.owned) return { group: "not", why: t.domain ? `${t.domain}: another lead's Domain` : "No Domain: line" };
  if (["completed", "canceled", "duplicate"].includes(t.state_type)) return { group: "done", why: `${t.linear_state} in Linear` };
  if (t.in_review) return { group: "done", why: `${t.linear_state}: waits on a person` };
  if (!t.in_scope) return { group: "not", why: `${t.linear_state}: out of scope in Linear` };
  if (!t.context) return { group: "not", why: t.unmapped_reason || "No repo is mapped for this domain" };
  if (!v || t.freshness !== "fresh") return { group: "stale", why: RECHECK[t.freshness] || "Queued for verification" };
  if (v.kind === "valid") return skipped[t.identifier] ? { group: "not", why: skipped[t.identifier] } : { group: "ready", why: v.reason };
  if (v.kind === "needs-clarification") return { group: "answer", why: v.reason };
  if (v.kind === "invalid-references") return { group: "answer", why: `${v.target}: ${v.reason}` };
  return { group: "not", why: `${v.kind === "duplicate-of" ? `Duplicate of ${v.target}` : v.kind.replace("-", " ")}: ${v.reason}` };
}

function Row({ t, why, pick, onOpen }) {
  const v = t.verdict, d = t.dispatch;
  return (
    <div className={`fx-trow${pick?.checked ? " picked" : ""}`} onClick={onOpen} role="button" tabIndex={0}
         onKeyDown={(e) => e.key === "Enter" && onOpen()}>
      {pick ? <input type="checkbox" className="fx-pick" checked={pick.checked} disabled={pick.disabled} onClick={stop}
                     onChange={pick.toggle} aria-label={`Select ${t.identifier}`} /> : null}
      <div className="fx-grow">
        <div className="fx-row"><span className="fx-id">{t.identifier}</span><span className="fx-hint">{t.linear_state}</span>
          <span className="fx-grow" /><span className="fx-hint">{ago(t.last_at)}</span></div>
        <div className="fx-ttitle">{t.title}</div>
        <div className="fx-row fx-tmeta">
          {v ? <Tone tone={VERDICT_TONE[v.kind] || "gray"}>{v.kind}</Tone> : null}
          {t.freshness && t.freshness !== "fresh" ? <Tone tone="amber">{t.freshness}</Tone> : null}
          {d ? <span className="fx-hint">{d.run_id} · {CARD[d.card_status]}</span> : null}
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
  useEffect(() => {  // refetched when the overview says the ticket moved
    SDK.fetchJSON(`${API}/tickets/${t.identifier}/timeline`).then((x) => { setTl(x); setErr(null); }, (e) => setErr(errText(e)));
  }, [t.identifier, t.last_at]);
  useEffect(() => {
    const k = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const cur = (tl || []).filter((e) => e.kind === "verdict" && !e.detail.superseded_at).pop()?.detail;
  const d = t.dispatch;
  return (
    <div className="fx-sheet-bg" onClick={onClose}>
      <div className="fx-sheet" role="dialog" aria-modal="true" aria-label={t.identifier} onClick={stop}>
        <div className="fx-row between">
          <div className="fx-row"><Ext href={t.url}>{t.identifier}</Ext><Tone tone="gray">{t.linear_state}</Tone></div>
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
            <ul className="fx-evidence">{cur.evidence.map((e, i) => <Evidence key={i} e={e} />)}</ul>
          </div>
        ) : null}
        <div className="fx-k">Timeline</div>
        {err ? <div className="fx-err">{err}</div> : !tl ? <div className="fx-hint">Loading…</div>
          : <ol className="fx-timeline">{tl.map((e, i) => <Event key={i} e={e} />)}</ol>}
      </div>
    </div>
  );
}

export function TicketsTab({ data, onDone }) {
  const [picked, setPicked] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(null);
  const all = data.all_tickets || [];
  const max = data.candidates?.max_tickets || 0;
  const stageable = (data.candidates?.candidates || []).map((c) => c.identifier);
  const suggested = (data.candidates?.suggested || []).filter((i) => stageable.includes(i));
  const sel = picked.filter((i) => stageable.includes(i));
  const skipped = Object.fromEntries((data.candidates?.skipped || []).map((x) => [x.identifier, x.reason]));
  const groups = { ready: [], answer: [], stale: [], dispatch: [], done: [], not: [] };
  const rows = all.map((t) => ({ t, ...groupOf(t, skipped) }))
    .sort((a, b) => (Date.parse(b.t.last_at) || 0) - (Date.parse(a.t.last_at) || 0));
  rows.forEach((r) => groups[r.group].push(r));
  const [filter, setFilter] = useState(groups.answer.length && !groups.ready.length ? "answer" : "ready");
  const needle = q.trim().toLowerCase();
  // A search looks through every ticket, whatever the filter.
  const shown = needle ? rows.filter(({ t }) => `${t.identifier} ${t.title}`.toLowerCase().includes(needle)) : groups[filter];
  const titles = Object.fromEntries(all.map((t) => [t.identifier, t.title]));
  const draft = (ids) => {
    setBusy(true); setErr(null);
    SDK.fetchJSON(`${API}/stage`, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ identifiers: ids }) })
      .then(() => { setBusy(false); setPicked([]); onDone({}, null, "Drafted; a planner is writing the plan"); },
            (e) => { setBusy(false); setErr(errText(e)); });
  };
  const current = open && all.find((t) => t.identifier === open);
  return (
    <>
      <Input className="fx-search" type="search" placeholder="Search id or title" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="fx-chips" role="tablist" aria-label="Ticket filter">
        {FILTERS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={!needle && filter === k} className={`fx-chip${!needle && filter === k ? " on" : ""}`}
                  onClick={() => { setFilter(k); setQ(""); }}>{label} <span className="fx-count">{groups[k].length}</span></button>
        ))}
      </div>
      {!needle && filter === "ready" && suggested.length ? (
        <Card className="fx-card fx-suggest"><CardContent className="fx-stack-v">
          <div className="fx-row between"><span className="fx-k">Next dispatch</span><span className="fx-hint">★ recommended</span></div>
          <div>{suggested.map((i) => <div key={i} className="fx-ttitle clamp"><span className="fx-id">{i}</span> {titles[i]}</div>)}</div>
          <div className="fx-hint">The factory would group these next: same Domain, then same repo. A planner shapes them into one plan; you review it before anything runs.</div>
          <div className="fx-row"><Button size="sm" disabled={busy} onClick={() => draft(suggested)}>{busy ? "Drafting…" : `★ Draft these ${suggested.length}`}</Button>
            <span className="fx-hint">or tick your own below</span></div>
          {err ? <div className="fx-err" role="alert">{err}</div> : null}
        </CardContent></Card>
      ) : null}
      <div className="fx-list">
        {shown.length ? shown.map(({ t, why, group }) => (
          <Row key={t.identifier} t={t} why={why} onOpen={() => setOpen(t.identifier)} pick={group === "ready" && stageable.includes(t.identifier) ? {
            checked: sel.includes(t.identifier), disabled: !sel.includes(t.identifier) && sel.length >= max,
            toggle: () => setPicked(sel.includes(t.identifier) ? sel.filter((i) => i !== t.identifier) : [...sel, t.identifier]),
          } : null} />
        )) : <div className="fx-empty">{needle ? "No ticket matches." : EMPTY[filter]}</div>}
      </div>
      {sel.length ? (
        <div className="fx-draftbar">
          <span><b>{sel.length}</b> picked</span><span className="fx-grow" />
          <Button size="sm" ghost onClick={() => setPicked([])}>Clear</Button>
          <Button size="sm" disabled={busy} onClick={() => draft(sel)}>{busy ? "Drafting…" : "Draft dispatch"}</Button>
        </div>
      ) : null}
      {sel.length && err ? <div className="fx-err" role="alert">{err}</div> : null}
      {current ? <Sheet t={current} onClose={() => setOpen(null)} /> : null}
    </>
  );
}

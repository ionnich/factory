// Factory tab, mobile first: a stack of cards.
//   Needs you: a deck of decisions. Each is a question with options, what each leads to, and the factory's
//     recommendation. Tap an option, or swipe right to take the recommendation, left for later.
//   Lifecycle: one tab per factory stage — Tickets (every ticket and its audit trail; pick drafts), Draft (the plan:
//     plan.jsx, the configurator, with notes while draft), Run (staged, executing, done; the plan read-only), Learn
//     (reconciled, archived: predicted vs landed, learnings, throughput). Dispatches are rows in an engineering table.
//     Quick lane: light decisions, ★ on all in one tap.
// Every action goes through the plugin API to the factory CLI, which enforces the invariants.
// Built by install.sh (`bun build`, classic JSX via tsconfig.json) to dist/index.js; React and the shadcn-style
// components come from the dashboard SDK. The Tickets tab lives in tickets.jsx.
import { Learnings } from "./learn.jsx";
import { Plan, Quick } from "./plan.jsx";
import { TicketsTab } from "./tickets.jsx";
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useCallback, useRef, useMemo } = SDK.hooks;
const { Button, Badge, Card, CardContent, Input, Toast } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;  // for <>…</>
const API = "/api/plugins/factory";
import { Why, WhyContext } from "./why.jsx";


const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "never");
const epochAgo = (v) => (v == null ? "never" : typeof v === "number" ? SDK.utils.timeAgo(v) : ago(v));
const errText = (e) => String(e && e.message ? e.message : e);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const clip = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
const post = (path, body) => SDK.fetchJSON(API + path,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const localTime = (iso) => new Date(iso).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
const until = (iso) => {
  const m = Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
};
const stop = (e) => e.stopPropagation();
const BADGE = { amber: "warning", green: "success", blue: "secondary", gray: "outline", red: "destructive" };
const Tone = ({ tone, children }) => <Badge tone={BADGE[tone] || "outline"}>{children}</Badge>;
const Ext = ({ href, children }) => <a className="fx-link" href={href} target="_blank" rel="noreferrer" onClick={stop}>{children}</a>;

function useAction(onDone) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const run = (path, body) => {
    setBusy(true); setErr(null);
    return post(path, body).then((r) => { setBusy(false); onDone(r); }, (e) => { setBusy(false); setErr(errText(e)); });
  };
  return { busy, err, run };
}
const ActErr = ({ err }) => (err ? <div className="fx-err" role="alert">{err}</div> : null);

// ---- plain words ----------------------------------------------------------------------------------------------
const KIND = { review: "Review", plan: "Planner asks", ask: "Executor asks", blocked: "Blocked",
               "executor-gone": "Executor gone", "dispatch-stuck": "Gone quiet", writeback: "Held Linear write",
               learning: "Keep a learning?" };
const KIND_ORDER = ["review", "ask", "executor-gone", "dispatch-stuck", "blocked", "writeback", "plan", "learning"];
const CARD = { ready: "not started", running: "in progress", done: "done", blocked: "blocked" };
const CARD_TONE = { ready: "gray", running: "blue", done: "green", blocked: "amber" };

function dispatchStatus(d) {
  const cards = d.tickets || [];
  const n = (s) => cards.filter((c) => c.card_status === s).length;
  switch (d.state) {
    case "draft": return d.review === "planning" ? { tone: "gray", label: "Planning…" }
      : d.review === "held" ? { tone: "amber", label: "Held" }
      : d.review === "in-review" ? { tone: "amber", label: `Auto in ${until(d.review_until)}` }
      : { tone: "amber", label: "In review" };
    case "staged": return { tone: "blue", label: "Starting" };
    case "executing": return { tone: "blue", label: `${n("done")}/${cards.length} done` };
    case "done": return { tone: "blue", label: "Writing back" };
    case "reconciled": return { tone: "green", label: "Written back" };
    case "archived": return { tone: "green", label: "Archived" };
    default: return { tone: "gray", label: d.state };
  }
}

function dispatchTitle(d, titles) {
  const root = (d.tree || []).find((n) => n.id === "root");
  if (root && root.title && root.title !== d.run_id) return root.title;
  const ids = (d.tickets || []).map((c) => c.identifier);
  if (ids.length === 1) return titles[ids[0]] || ids[0];
  return ids.length ? `${ids.length} tickets: ${ids.join(", ")}` : d.run_id;
}

// ---- decisions -------------------------------------------------------------------------------------------------
// One option: recommended first and marked ★. Weighty ones (start or stop real work, write Linear) take a second
// tap; options that need words open a box first.
function Option({ d, o, onChoose, busy }) {
  const [armed, setArmed] = useState(false);
  const [noting, setNoting] = useState(false);
  const [text, setText] = useState("");
  useEffect(() => { if (!armed) return; const t = setTimeout(() => setArmed(false), 4000); return () => clearTimeout(t); }, [armed]);
  const rec = o.id === d.recommended;
  const go = () => {
    if (busy) return;
    if (o.note && !noting) return setNoting(true);
    if (o.note && !text.trim()) return;
    if ((o.weighty || d.kind === "ask") && !armed) return setArmed(true);
    onChoose(o.id, text.trim() || undefined);
  };
  return (
    <div className={`fx-opt${rec ? " rec" : ""}${armed ? " armed" : ""}`} onClick={stop}>
      <button className="fx-opt-btn" disabled={busy} onClick={go} aria-label={`${o.label}${rec ? " (recommended)" : ""}`}>
        <span className="fx-opt-label">{rec ? <span className="star">★</span> : null}{armed ? `Tap again: ${o.label}` : o.label}</span>
        <span className="fx-opt-leads">→ {o.leads_to}</span>
      </button>
      {noting ? (
        <div className="fx-row fx-note-in">
          <Input autoFocus value={text} maxLength={4000} placeholder={o.note} disabled={busy}
                 onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") go(); if (e.key === "Escape") setNoting(false); }} />
          <Button size="sm" disabled={busy || !text.trim()} onClick={go}>{armed ? "Confirm" : "Send"}</Button>
        </div>
      ) : null}
    </div>
  );
}

function sortOptions(d) {
  return [...d.options].sort((a, b) => (b.id === d.recommended) - (a.id === d.recommended));
}

function Answered({ d }) {
  const o = d.options.find((x) => x.id === d.chosen);
  if (d.void_reason) return <div className="fx-answered void">Withdrawn: {d.void_reason}</div>;
  return (
    <div className="fx-answered">
      <span className="tick">✓</span> <b>{o ? o.label : d.chosen}</b>{d.chosen !== d.recommended ? " (not the recommendation)" : ""}
      <span className="fx-hint"> · {d.chosen_by} · {ago(d.chosen_at)}</span>
      {d.chosen_note ? <div className="fx-hint">“{d.chosen_note}”</div> : null}
    </div>
  );
}

function Silence({ d }) {
  if (!d.on_timeout) return null;
  const when = d.deadline ? ` until ${localTime(d.deadline)} (in ${until(d.deadline)})` : "";
  return <div className="fx-hint">If you stay silent{when}: {d.on_timeout}.</div>;
}

function DecisionBody({ d, onChoose, busy, err, compact, hideHold }) {
  if (!d.open) return <><Answered d={d} /><Why d={d} /></>;
  return (
    <>
      <div className="fx-why"><span className="star">★</span> {d.options.find((o) => o.id === d.recommended)?.label}: {d.why}</div>
      <div className="fx-opts">{sortOptions(d).filter((o) => !hideHold || o.id !== "hold").map((o) => <Option key={o.id} d={d} o={o} busy={busy} onChoose={onChoose} />)}</div>
      <ActErr err={err} />
      {!compact ? <Silence d={d} /> : null}
      <Why d={d} />
    </>
  );
}

function useChoose(d, onDone) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const choose = (option, note) => {
    setBusy(true); setErr(null);
    return post(`/decisions/${d.id}`, { option, note }).then((r) => { setBusy(false); onDone(r, d); return r; },
      (e) => { setBusy(false); setErr(errText(e)); throw e; });
  };
  return { busy, err, choose };
}

// The deck: the top card is live; two more peek out behind it. A tap answers at once: the card flies off and the
// next one is live while the server confirms; if it refuses, the card comes back on top with the reason.
function Deck({ decisions, context, onDone, onOpen }) {
  const [order, setOrder] = useState([]);
  const [gone, setGone] = useState({});
  const [flying, setFlying] = useState([]);
  const [errs, setErrs] = useState({});
  const ids = decisions.map((d) => d.id).filter((id) => !gone[id]);
  const seq = [...order.filter((id) => ids.includes(id)), ...ids.filter((id) => !order.includes(id))];
  const byId = Object.fromEntries(decisions.map((d) => [d.id, d]));
  const later = () => setOrder([...seq.slice(1), seq[0]]);
  const drop = (map, id) => { const { [id]: _, ...rest } = map; return rest; };
  const choose = (d, option, note) => {
    setGone((g) => ({ ...g, [d.id]: true }));
    setErrs((e) => drop(e, d.id));
    setFlying((f) => [...f, d]);
    setTimeout(() => setFlying((f) => f.filter((x) => x.id !== d.id)), 340);
    post(`/decisions/${d.id}`, { option, note }).then((r) => onDone(r, d), (e) => {
      setGone((g) => drop(g, d.id));
      setErrs((x) => ({ ...x, [d.id]: errText(e) }));
      setOrder((o) => [d.id, ...o.filter((id) => id !== d.id)]);  // back on top, with why
    });
  };
  const flyers = flying.map((d) => (
    <div key={`fly-${d.id}`} className="fx-card fx-top fx-fly leave-right" aria-hidden="true">
      <span className="fx-k">{KIND[d.kind]}</span><div className="fx-q">{d.question}</div>
    </div>
  ));
  if (!seq.length) {
    return (
      <div className="fx-deck">
        <div className="fx-stack">
          <Card className="fx-card fx-clear"><CardContent><div className="fx-bounce">✓</div>
            <div className="fx-title">Nothing needs you</div>
            <div className="fx-hint">New questions land here as cards. The factory keeps going on its own meanwhile.</div>
          </CardContent></Card>
          {flyers}
        </div>
      </div>
    );
  }
  return (
    <div className="fx-deck">
      <div className="fx-stack">
        {seq.slice(1, 3).reverse().map((id, i, arr) => (
          <div key={id} className={`fx-card fx-behind b${arr.length - i}`} aria-hidden="true">
            <span className="fx-k">{KIND[byId[id].kind]}</span>
          </div>
        ))}
        <TopCard key={seq[0]} d={byId[seq[0]]} ctx={context(byId[seq[0]])} err={errs[seq[0]]} onChoose={choose}
                 onLater={later} onOpen={onOpen} pos={`1 of ${seq.length}`} canLater={seq.length > 1} />
        {flyers}
      </div>
    </div>
  );
}

function TopCard({ d, ctx, err, onChoose, onLater, onOpen, pos, canLater }) {
  const [dx, setDx] = useState(0);
  const [leaving, setLeaving] = useState(null);
  const drag = useRef(null);
  const rec = d.options.find((o) => o.id === d.recommended);
  const down = (e) => {
    if (e.target.closest("button, input, a")) return;
    drag.current = { x: e.clientX, id: e.pointerId };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e) => { if (drag.current) setDx(e.clientX - drag.current.x); };
  const up = () => {
    if (!drag.current) return;
    drag.current = null;
    if (dx < -110 && canLater) { setLeaving("left"); setTimeout(() => { setLeaving(null); setDx(0); onLater(); }, 280); return; }
    if (dx > 110 && d.kind !== "ask" && rec && !rec.note && !rec.weighty) { setDx(0); onChoose(d, rec.id); return; }
    setDx(0);  // weighty or needs words: swipe only points at it; tap the ★ option
  };
  const style = leaving ? undefined : dx ? { transform: `translateX(${dx}px) rotate(${dx / 24}deg)`, transition: "none" } : undefined;
  return (
    <Card className={`fx-card fx-top${leaving ? ` leave-${leaving}` : ""}`} style={style}
          onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { drag.current = null; setDx(0); }}>
      <CardContent className="fx-stack-v">
        {dx > 40 && !d.group ? <div className="fx-swipe right">★ {rec?.label}</div> : dx < -40 && canLater ? <div className="fx-swipe left">Later</div> : null}
        <div className="fx-row between">
          <span className="fx-row"><Tone tone={d.tier === "now" || d.kind === "writeback" || d.kind === "blocked" ? "red" : "amber"}>{KIND[d.kind]}</Tone>
            {d.tier === "now" ? <span className="fx-urgent">work waits on you</span> : null}
            {d.deadline ? <span className="fx-clock">⏱ {until(d.deadline)}</span> : null}</span>
          <span className="fx-hint">{pos}</span>
        </div>
        {ctx ? <button className="fx-ctx" onClick={() => onOpen(d)}>{ctx} ›</button> : null}
        <div className="fx-q">{d.question}</div>
        {d.detail?.reason && d.kind !== "writeback" ? <div className="fx-hint">{d.detail.reason}</div> : null}
        {d.group ? (<>  {/* a draft's plan questions + review: one card that opens the configurator */}
          {d.result ? <div className="fx-hint">→ {d.result}</div> : null}
          <Button onClick={() => onOpen(d)}>Open the plan ›</Button>
        </>) : <DecisionBody d={d} err={err} onChoose={(o, n) => onChoose(d, o, n)} />}
        <div className="fx-hint fx-gesture">{d.group ? (canLater ? "Swipe left for later" : "") : d.kind === "ask" ? `Tap an answer twice to confirm${canLater ? "; swipe left for later" : ""}` : `Swipe right for ★${canLater ? ", left for later" : ""}`}</div>
      </CardContent>
    </Card>
  );
}

function ExecutorDelivery({ item, onDone }) {
  const action = useAction(onDone);
  const sending = action.busy || item.state === "sending";
  return (
    <div className="fx-stack-v">
      <div className="fx-row"><Tone tone="amber">{sending ? "Sending to executor" : "Executor answer not delivered"}</Tone><span className="fx-id">{item.run_id} · #{item.decision_id}</span></div>
      <div>{item.question}</div>
      <div>Recorded answer: <b>{item.answer}</b>. This answer will not be changed.</div>
      <ActErr err={item.error} />
      <ActErr err={action.err} />
      <Button size="sm" disabled={sending} onClick={() => action.run(`/decisions/${item.decision_id}/resend`, {})}>
        {sending ? "Sending…" : "Resend recorded answer"}
      </Button>
    </div>
  );
}

function ExecutorDeliveries({ items, onDone }) {
  if (!items.length) return null;
  return <section className="fx-sec fx-stack-v" aria-label="Executor answer delivery">
    {items.map((item) => <ExecutorDelivery key={item.decision_id} item={item} onDone={onDone} />)}
  </section>;
}

// What the factory answered on its own this week (earned, nothing to weigh, silence past its deadline).
function DoneForYou({ items }) {
  const label = (d) => d.options.find((o) => o.id === d.chosen)?.label || d.chosen;
  const why = (d) => d.chosen_by.replace(/^factory:auto \((.*)\)$/, "$1");
  return (
    <ul className="fx-done">
      {[...items].reverse().map((d) => (
        <li key={d.id}><span className="tick">✓</span> <b>{label(d)}</b> · {d.question}
          <div className="fx-hint">{why(d)} · {ago(d.chosen_at)}</div></li>
      ))}
    </ul>
  );
}

// ---- the rail: dots and wires beside rows (railway.jsx lays out the plan's map on it) --------------------------
const LANE_W = 16, RAIL_X0 = 10;
const DOT_TONE = (n) => {
  if (n.kind === "root") return dispatchStatus(n.d).tone;
  if (n.kind === "gate" || n.kind === "decision") return n.decision.open ? "amber" : n.decision.void_reason ? "gray" : "done";
  if (n.kind === "outcome") return n.card?.card_status === "done" ? (n.writes.length && n.writes.every((w) => w.status === "confirmed") ? "green" : "blue") : n.card?.card_status === "blocked" ? "amber" : "ghost";
  return CARD_TONE[n.card?.card_status] || "gray";
};

function Rail({ flow, layout, ys, lit, width }) {
  const x = (l) => RAIL_X0 + l * LANE_W;
  const at = Object.fromEntries(flow.nodes.map((n, i) => [n.id, ys[i]]));
  if (ys.length !== flow.nodes.length || ys.some((y) => y == null)) return null;
  const path = (e) => {
    const px = x(layout.lane[e.s]), py = at[e.s], cx = x(layout.lane[e.t]), cy = at[e.t];
    const T = layout.track.get(e);
    if (T == null) return px === cx ? `M${px},${py} L${cx},${cy}` : `M${px},${py} C${px},${(py + cy) / 2} ${cx},${(py + cy) / 2} ${cx},${cy}`;
    const tx = x(T), g = 12;
    return `M${px},${py} C${px},${py + g} ${tx},${py + g} ${tx},${py + 2 * g} L${tx},${cy - 2 * g} C${tx},${cy - g} ${cx},${cy - g} ${cx},${cy}`;
  };
  return (
    <svg className="fx-rail" width={width} height={Math.max(...ys) + 40} aria-hidden="true">
      {flow.edges.map((e, i) => <path key={i} d={path(e)} className={`fx-wire ${e.kind}${lit && lit.has(e.s) && lit.has(e.t) ? " lit" : ""}`} />)}
      {flow.nodes.map((n, i) => {
        const cx = x(layout.lane[n.id]), cy = ys[i], tone = DOT_TONE(n);
        const cls = `fx-dot t-${tone}${lit?.has(n.id) ? " lit" : ""}`;
        if (n.kind === "gate" || n.kind === "decision") return <path key={n.id} className={cls} d={`M${cx},${cy - 6} L${cx + 6},${cy} L${cx},${cy + 6} L${cx - 6},${cy} z`} />;
        if (n.kind === "outcome") return <rect key={n.id} className={cls} x={cx - 5} y={cy - 5} width="10" height="10" rx="3" />;
        return <circle key={n.id} className={cls} cx={cx} cy={cy} r={n.kind === "root" ? 7 : n.kind === "ticket" ? 6 : 4} />;
      })}
    </svg>
  );
}

function NoteBox({ run, nodeId, onDone }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const a = useAction(() => { setText(""); setOpen(false); onDone({}, null, "Note added"); });
  if (!open) return <button className="fx-link-btn" onClick={(e) => { stop(e); setOpen(true); }}>+ note</button>;
  const go = () => text.trim() && a.run(`/drafts/${encodeURIComponent(run)}/notes`, { node: nodeId, body: text.trim() });
  return (
    <div onClick={stop}>
      <div className="fx-row">
        <Input autoFocus value={text} maxLength={4000} disabled={a.busy} placeholder={nodeId === "root" ? "Note for the whole dispatch" : `Note on ${nodeId}`}
               onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") go(); if (e.key === "Escape") setOpen(false); }} />
        <Button size="sm" disabled={a.busy || !text.trim()} onClick={go}>{a.busy ? "…" : "Add"}</Button>
      </div>
      <div className="fx-hint">The executor reads it word for word; it can't be edited later.</div>
      <ActErr err={a.err} />
    </div>
  );
}

const Notes = ({ notes }) => (notes || []).map((n) => <div key={n.id} className="fx-note">{n.body}<div className="fx-hint">{n.author} · {ago(n.at)}</div></div>);

const Writes = ({ writes }) => (writes.length ? <ul className="fx-writes">{writes.map((w, k) => (
  <li key={k} className={`w-${w.decision === "flag" ? "held" : w.status}`}>
    <span className="mark">{w.decision === "flag" ? "⏸" : w.status === "confirmed" ? "✓" : w.status === "failed" ? "✕" : "…"}</span>
    Linear {w.op === "state" ? `→ ${w.payload?.state || "state"}` : w.op === "create" ? `follow-up “${clip(w.payload?.title, 40)}”${w.linear_ref ? ` (${w.linear_ref})` : ""}` : w.op}
    {w.decision === "flag" ? " · held" : w.approved_by ? ` · applied by ${w.approved_by}` : ""}
  </li>))}</ul> : null);

// ---- lifecycle: dispatches as rows in an engineering table, one tab per stage ------------------------------
const STAGES = [["tickets", "Tickets"], ["draft", "Draft"], ["run", "Run"], ["learn", "Learn"]];
const stageOf = (d) => d.state === "draft" ? "draft" : ["staged", "executing", "done"].includes(d.state) ? "run" : "learn";

function DispatchRow({ d, title, needs, selected, onClick }) {
  const s = dispatchStatus(d);
  const cards = d.tickets || [];
  const repos = (() => { try { return JSON.parse(d.repos_json || "[]").map((r) => r.repo.split("/").pop()); } catch { return []; } })();
  return (
    <div className={`fx-tr${selected ? " sel" : ""}`} onClick={onClick} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { stop(e); onClick(); } }}>
      <div className="fx-tc fx-tc-st"><Tone tone={s.tone}>{s.label}</Tone></div>
      <div className="fx-tc fx-tc-ti">
        <div className="fx-ttitle">{title}</div>
        <div className="fx-hint">{d.run_id}{repos.length ? ` · ${repos.join(", ")}` : ""}</div>
      </div>
      <div className="fx-tc fx-tc-tk"><div className="fx-row">{cards.map((c) => <span key={c.identifier} className="fx-id">{c.identifier}</span>)}</div></div>
      <div className="fx-tc fx-tc-pr"><div className="fx-bar">{cards.map((c) => <i key={c.identifier} className={`t-${CARD_TONE[c.card_status]}`} />)}</div></div>
      <div className="fx-tc fx-tc-ne">{needs ? <span className="fx-needs">{needs}</span> : <span className="fx-hint">—</span>}</div>
      <div className="fx-tc fx-tc-age"><span className="fx-hint">{ago(d.created_at)}</span></div>
    </div>
  );
}

// An engineering table of dispatches: stage, what it is, tickets, progress, what waits on you, age. On the phone
// the columns fold into two lines; the header row disappears.
function StageTable({ dispatches, titles, needsOf, selected, onSelect }) {
  return (
    <div className="fx-table">
      <div className="fx-thead"><span>Stage</span><span>Dispatch</span><span>Tickets</span><span>Progress</span><span>Needs</span><span>Age</span></div>
      {dispatches.map((d) => (
        <DispatchRow key={d.run_id} d={d} title={dispatchTitle(d, titles)} needs={needsOf(d.run_id)}
                     selected={selected?.run_id === d.run_id} onClick={() => onSelect(d.run_id)} />
      ))}
    </div>
  );
}

// ---- health + throughput -------------------------------------------------------------------------------------
const JOB_NAME = { "factory-prune": "Verification", "[bot:planner] Plan drafts": "Planning",
                   "factory-propose": "Proposals", "factory-reconcile": "Write-back", "factory-backup": "Backup" };
function Health({ jobs }) {
  const name = (j) => JOB_NAME[j.name] || j.name;
  const bad = jobs.filter((j) => j.last_status && !["ok", "success", "succeeded"].includes(j.last_status));
  return (
    <span className={`fx-health ${bad.length ? "bad" : "ok"}`} title={jobs.map((j) => `${name(j)}: ${j.last_status || "not run"}, ${epochAgo(j.last_run_at)}`).join("\n")}>
      <i />{bad.length ? bad.map((j) => `${name(j)} failed`).join(" · ") : "all jobs fine"}
    </span>
  );
}

const pct = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
const hours = (v) => (v == null ? "—" : v < 48 ? `${v.toFixed(1)}h` : `${(v / 24).toFixed(1)}d`);
function Throughput() {
  const [m, setM] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { SDK.fetchJSON(`${API}/metrics?days=28`).then(setM, (e) => setErr(errText(e))); }, []);
  if (!m) return err ? <div className="fx-err">{err}</div> : <div className="fx-hint">Loading…</div>;
  const stats = [["done", m.tickets.done], ["blocked", m.tickets.blocked], ["block rate", pct(m.block_rate)],
                 ["approve → done", hours(m.hours.stage_to_done_p50)], ["slowest", hours(m.hours.stage_to_done_max)],
                 ["done → archived", hours(m.hours.done_to_archived_p50)]];
  const c = m.cost;
  const usd = (v) => (v == null ? "—" : `$${v.toFixed(2)}`);
  const spend = Object.entries(c.by_stage).sort((a, b) => b[1] - a[1]);
  return (
    <div className="fx-stack-v">
      <div className="fx-stats">{stats.map(([k, v]) => <div key={k} className="fx-stat"><b>{v}</b><span>{k}</span></div>)}</div>
      <div className="fx-hint">{`Last 28 days · ${m.dispatches.staged} approved, ${m.dispatches.archived} archived · Linear writes: ${m.writeback.confirmed} sent, ${m.writeback.failed} failed, ${m.writeback.flagged} held`}</div>
      <div className="fx-stats">{[["per dispatch", c.per_unit.dispatch], ["per plan", c.per_unit.plan], ["per verdict", c.per_unit.verdict]]
        .map(([k, v]) => <div key={k} className="fx-stat"><b>{usd(v)}</b><span>{k}</span></div>)}</div>
      <ul className="fx-cost">{spend.map(([s, v]) => (
        <li key={s}><span>{STAGE_COST[s] || s}</span><i style={{ width: `${c.total ? (v / c.total) * 100 : 0}%` }} /><b>{usd(v)}</b></li>))}</ul>
      <div className="fx-hint">{`Agent spend ${usd(c.total)} in 28 days, from the agents' own session records`}</div>
    </div>
  );
}
const STAGE_COST = { captain: "Fleet captain (routing)", secondmate: "Domain leads", crew: "Crews (code)", prune: "Verification",
                     plan: "Planning", reconcile: "Write-back", chat: "Chat" };

// ---- page ------------------------------------------------------------------------------------------------------
function FactoryPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [live, setLive] = useState(false);
  const [sel, setSel] = useState(null);
  // ?ticket=<IDENT> opens the Tickets tab with that ticket's sheet (tickets.jsx reads the id)
  const [tab, setTab] = useState(() => new URLSearchParams(location.search).get("ticket") ? "tickets" : null);  // null = pick from what's going on
  const [toast, setToast] = useState(null);
  const [showTp, setShowTp] = useState(false);
  // ?view=review&run=<run_id> opens that dispatch's plan (the configurator) in place of the page, e.g. from a Hermex push
  const [review, setReview] = useState(() => { const p = new URLSearchParams(location.search); return p.get("view") === "review" ? p.get("run") : null; });
  const top = useRef(null);
  useEffect(() => { top.current?.scrollIntoView(); }, [review]);
  const strip = useRef(null);
  useEffect(() => {  // the active stage tab fully in view (nearest edge), scrolling the strip only, never the page
    const s = strip.current, t = s?.querySelector(".on");
    if (!t) return;
    const a = s.getBoundingClientRect(), b = t.getBoundingClientRect();
    if (b.left < a.left) s.scrollLeft -= a.left - b.left + 4;
    else if (b.right > a.right) s.scrollLeft += b.right - a.right + 4;
  }, [tab, review, !data]);
  const [, tick] = useState(0);
  const inflight = useRef(false);
  const again = useRef(false);
  const load = useCallback(() => {  // one refresh at a time; a change mid-flight refreshes once more after
    if (inflight.current) { again.current = true; return; }
    inflight.current = true;
    SDK.fetchJSON(`${API}/overview`)
      .then((d) => { setData(d); setError(null); setLoadedAt(new Date().toISOString()); }, (e) => setError(errText(e)))
      .finally(() => { inflight.current = false; if (again.current) { again.current = false; load(); } });
  }, []);
  useEffect(() => {  // the server says when factory.db changed; no polling
    load();
    const es = new EventSource(`${API}/stream`);
    es.addEventListener("change", load);
    es.onopen = () => { setLive(true); load(); };
    es.onerror = () => setLive(false);
    return () => es.close();
  }, [load]);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 30000); return () => clearInterval(t); }, []);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 3500); return () => clearTimeout(t); }, [toast]);
  const titles = useMemo(() => {
    const m = {};
    (data?.tickets || []).forEach((t) => { m[t.identifier] = t.title; });
    (data?.status?.written_back || []).forEach((w) => { m[w.identifier] ||= w.title; });
    return m;
  }, [data]);

  if (!data) return <div className="fx">{error ? <div className="fx-err">{error}</div> : <div className="fx-hint">Loading…</div>}</div>;

  // The deck holds what needs a person: pushed (work waits) and digest ones; the factory takes the auto ones itself.
  // A planner question sorts with its dispatch's review, just ahead of it: answer the plan, then approve it.
  const open = data.status.decisions;
  const reviewOf = Object.fromEntries(open.filter((x) => x.kind === "review").map((x) => [x.run_id, x]));
  const lead = (x) => (x.kind === "plan" && reviewOf[x.run_id]) || x;
  const decisions = open.filter((x) => x.tier !== "auto").sort((p, q) => {
    const a = lead(p), b = lead(q);
    return (b.tier === "now") - (a.tier === "now")
      || (a.deadline ? Date.parse(a.deadline) : Infinity) - (b.deadline ? Date.parse(b.deadline) : Infinity)
      || KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.id - b.id
      || (p.kind === "review") - (q.kind === "review") || p.id - q.id;
  });
  const byRun = Object.fromEntries(data.dispatches.map((d) => [d.run_id, d]));
  const answers = data.ticket_counts?.answer || 0;
  const deliveries = data.status.executor_deliveries || [];
  const needs = decisions.length + deliveries.length;
  const needsOf = (runId) => open.filter((x) => x.run_id === runId && x.tier !== "auto").length;
  // A draft's plan questions and review are one deck card that opens the configurator; light ones (★ starts or
  // stops nothing, writes nothing) go to the Quick lane, ★ on all in one tap.
  const light = (x) => x.tier !== "now" && !["plan", "review", "ask"].includes(x.kind) && !x.options.find((o) => o.id === x.recommended)?.weighty;
  const grouped = new Set();
  const deck = decisions.filter((x) => !light(x)).flatMap((x) => {
    const d = byRun[x.run_id];
    if (!((x.kind === "plan" || x.kind === "review") && d?.state === "draft")) return [x];
    if (grouped.has(x.run_id)) return [];
    grouped.add(x.run_id);
    const n = open.filter((y) => y.kind === "plan" && y.run_id === x.run_id).length;
    return [{ ...(reviewOf[x.run_id] || x), kind: "review", group: true, options: [], result: (d.tree || []).find((t) => t.id === "root")?.result,
              question: `Review ${(d.tickets || []).map((c) => c.identifier).join(", ")}${n ? ` · ${plural(n, "question")}` : ""}` }];
  });
  const tix = Object.fromEntries(data.tickets.map((t) => [t.identifier, t]));
  const rows = { tickets: [], draft: [], run: [], learn: [] };
  data.dispatches.forEach((d) => rows[stageOf(d)].push(d));
  const count = (stage) => stage === "tickets" ? (data.ticket_counts?.ready || 0) + answers : rows[stage].length;
  // Default stage: what needs you, else where the dispatches are. A dispatch belongs to one stage for its whole
  // life there, so a row only ever moves forward.
  const active = tab && STAGES.some(([id]) => id === tab) ? tab
    : (decisions[0] && byRun[decisions[0].run_id] ? stageOf(byRun[decisions[0].run_id]) : null)
      || (rows.draft.length ? "draft" : rows.run.length ? "run" : "tickets");
  const list = rows[active];
  const current = (byRun[sel] && list.some((d) => d.run_id === sel) ? byRun[sel] : list[0]) || null;
  const context = (x) => {
    const base = byRun[x.run_id] ? clip(dispatchTitle(byRun[x.run_id], titles), 60) : x.identifier ? `${x.identifier} ${clip(x.title, 50)}` : null;
    if (x.kind === "plan" || x.group) return x.kind === "plan" ? `${base} · step ${x.node_id}` : base;
    const qs = x.kind === "review" ? open.filter((y) => y.kind === "plan" && y.run_id === x.run_id).length : 0;
    return qs ? `${base} · ${plural(qs, "planner question")} still open, ★ on approval` : base;
  };
  const done4u = data.status.done_for_you || [];
  const done = (r, d, msg) => {
    const o = d && d.options.find((x) => x.id === r?.chosen);
    setToast({ type: r?.after_error || r?.handoff_error ? "error" : "success",
               message: msg || (r?.after_error ? `Recorded, but: ${r.after_error}` : r?.handoff_error ? `Approved; starting is retried: ${r.handoff_error}` : `${o?.label || "Done"} ✓`) });
    load();
  };
  const openReview = (run) => {  // the URL follows, so a reload or a shared link lands on the same plan
    setReview(run);
    const u = new URL(location.href);
    if (run) { u.searchParams.set("view", "review"); u.searchParams.set("run", run); } else { u.searchParams.delete("view"); u.searchParams.delete("run"); }
    history.replaceState(history.state, "", u);
  };
  const openNode = (x) => {
    const run = byRun[x.run_id];
    if (x.group) return openReview(x.run_id);
    if (!run) return;
    setTab(stageOf(run));
    setSel(x.run_id);
  };
  const switchTab = (stage) => { setTab(stage); setSel(null); };
  const EMPTY = {
    draft: "No draft right now. The factory proposes one when verified tickets accumulate, or draft your own in Tickets.",
    run: "Nothing staged or executing right now.",
    learn: "Nothing learned yet. Dispatches land here after they run and write back to Linear.",
  };

  const why = { asks: data.asks || {}, reviews: reviewOf };  // why.jsx threads, in the plan and on every decision
  const connection = <>
    <button className="fx-link-btn" onClick={load} title="Refresh factory status">
      <span className={`fx-live${live ? " on" : ""}`} />{live ? "live" : "reconnecting"} · {loadedAt ? ago(loadedAt) : ""} · Refresh
    </button>
    {error ? <div className="fx-err" role="alert">Last refresh failed: {error}. Showing last loaded decisions; refresh before acting.</div> : null}
  </>;
  if (review) {
    return (
      <WhyContext.Provider value={why}>
      <div className="fx" ref={top}>
        <Toast toast={toast} />
        <header className="fx-head">{connection}</header>
        <ExecutorDeliveries items={deliveries.filter((item) => item.run_id === review)} onDone={done} />
        {byRun[review] ? <Plan key={review} d={byRun[review]} tickets={tix} onDone={done} onClose={() => openReview(null)} /> : (
          <div className="fx-row between"><span className="fx-empty">Dispatch {review} is not live any more.</span>
            <button className="fx-x" onClick={() => openReview(null)} aria-label="Close">✕</button></div>)}
      </div>
      </WhyContext.Provider>
    );
  }
  return (
    <WhyContext.Provider value={why}>
    <div className="fx" ref={top}>
      <Toast toast={toast} />
      <header className="fx-head">
        <div className={`fx-hello${needs ? " you" : ""}`}>{needs ? `${plural(needs, "thing")} need${needs === 1 ? "s" : ""} you` : "All clear"}</div>
        <div className="fx-row fx-hint">
          <Health jobs={data.jobs} /><span>·</span>
          {connection}
          {answers ? <><span>·</span><span>{plural(answers, "ticket")} need answers in Linear</span></> : null}
        </div>
      </header>
      <ExecutorDeliveries items={deliveries} onDone={done} />

      <Deck decisions={deck} context={context} onDone={(r, d) => done(r, d)} onOpen={openNode} />
      <Quick items={decisions.filter(light)} context={context} onDone={done} />

      <div className="fx-stage-tabs" role="tablist" aria-label="Factory lifecycle" ref={strip}>
        {STAGES.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={active === id} className={`fx-stage-tab${active === id ? " on" : ""}`}
                  onClick={() => switchTab(id)}>
            {label} <span className="fx-count">{count(id)}</span>
          </button>
        ))}
      </div>

      {active === "tickets" ? (
        <section className="fx-sec">
          <TicketsTab data={data} onDone={done} />
        </section>
      ) : (
        <section className="fx-sec">
          {list.length ? (<>
            <StageTable dispatches={list} titles={titles} needsOf={needsOf} selected={current} onSelect={setSel} />
            {current ? <Plan key={current.run_id} d={current} tickets={tix} onDone={done} /> : null}
          </>) : <div className="fx-empty">{EMPTY[active]}</div>}
        </section>
      )}

      {active === "learn" ? (<>
        <details className="fx-sec fx-fold" open>
          <summary>Learnings <span className="fx-count">{(data.learnings || []).length}</span></summary>
          <Learnings items={data.learnings || []} />
        </details>
        <details className="fx-sec fx-fold" onToggle={(e) => setShowTp(e.currentTarget.open)}>
          <summary>Throughput</summary>
          {showTp ? <Throughput /> : null}
        </details>
        {done4u.length ? (
          <details className="fx-sec fx-fold">
            <summary>Done for you <span className="fx-count">{done4u.length}</span> <span className="fx-hint">this week</span></summary>
            <DoneForYou items={done4u} />
          </details>
        ) : null}
      </>) : null}
    </div>
    </WhyContext.Provider>
  );
}

window.__HERMES_PLUGINS__.register("factory", FactoryPage);

// For plan.jsx / railway.jsx (bundled together; used at render time only, so the import cycle is harmless).
export { API, ActErr, CARD, CARD_TONE, DecisionBody, Ext, LANE_W, NoteBox, Notes, Option, RAIL_X0, Rail, Silence, Tone, Writes, clip, errText, plural, post, sortOptions, useChoose };

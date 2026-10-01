// Factory tab, mobile first. The header: Needs you (every decision or undelivered executor answer waiting on a person,
// each line a link to its stage and the thing itself), job health, the live refresh. Under it one tab per lifecycle
// stage and, apart, Learn and Costs; only the open one is on the page, with all of its own content:
//   Tickets: the whole ticket ledger and each ticket's audit trail (tickets.jsx), when Linear was last ingested.
//   Verify: tickets waiting on verification or an answer; the verification job's last run.
//   Draft: ready tickets to draft, drafts not offered to a planner yet, blocked tickets' retry questions.
//   Plan: drafts offered to the planner, or whose plan it refused, as recorded; the planner job's last run.
//   Review: planned drafts, the plan as a configurator (plan.jsx): lock in a path, approve, hold.
//   Run: executor questions, undelivered answers, the server's runtime read, the plan read-only.
//   Reconcile: done dispatches, every unsettled Linear write, held writes' questions.
//   Archive: every archived dispatch, rejected ones too (GET /archive), its transitions and plan.
//   Learn: learnings, and proposed ones to keep or drop. Costs: throughput and agent spend.
// The server's `phase` puts each dispatch, ticket and decision in its stage; nothing here guesses one. Where you are is
// the URL (?stage=, run=, decision=, ticket=): each deliberate move is a history entry and Back returns to it; a stage
// keeps its selection, ticket filters and scroll while you are elsewhere. A refresh never moves, reselects or unfolds.
// Every action goes through the plugin API to the factory CLI, which enforces the invariants.
// Built by install.sh (`bun build`, classic JSX via tsconfig.json) to dist/index.js; React and the shadcn-style
// components come from the dashboard SDK.
import { Learnings } from "./learn.jsx";
import { Plan, Quick } from "./plan.jsx";
import { Jev } from "./jev.jsx";
import { TicketsTab } from "./tickets.jsx";
import { StrategyTab } from "./strategy.jsx";
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
const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
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

// A dispatch's status inside its stage (the server's phase). Planning only as recorded: an offer, never a planner at work.
function dispatchStatus(d) {
  const cards = d.tickets || [];
  const n = (s) => cards.filter((c) => c.card_status === s).length;
  switch (d.phase) {
    case "draft": return { tone: "gray", label: "Not offered" };
    case "plan": return d.planning_error ? { tone: "red", label: "Plan refused" }
      : { tone: "gray", label: `Requested ${ago(d.planning_requested_at)}` };
    case "review": return d.review === "held" ? { tone: "amber", label: "Held" }
      : d.review === "in-review" ? { tone: "amber", label: `Auto in ${until(d.review_until)}` }
      : { tone: "amber", label: "In review" };
    case "run": return d.state === "staged" ? { tone: "blue", label: "Starting" } : { tone: "blue", label: `${n("done")}/${cards.length} done` };
    case "reconcile": return d.state === "done" ? { tone: "blue", label: "To write back" } : { tone: "green", label: "Written back" };
    case "archive": return d.rejected_reason ? { tone: "gray", label: "Rejected" } : { tone: "green", label: "Archived" };
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
  const focus = d.jev && d.jev.status === "ok" ? d.jev.focus : null;
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
        <span className={`fx-opt-leads${focus === "result" ? " jev-focus" : ""}`}>→ {o.leads_to}</span>
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

// An explanation longer than ~3 lines starts folded to its opening words (native details: a tap or Enter opens it).
// Those words stay the summary, so the toggle keeps a visible, accessible label; open, the rest of the text follows.
function Fold({ head, text, className }) {
  if (!text) return null;
  if (text.length <= 140) return <div className={className}>{head}{text}</div>;
  const i = text.lastIndexOf(" ", 80), cut = i > 40 ? i : 80;  // the opening words end between words when they can
  return (
    <details className={`fx-more ${className}`}>
      <summary>{head}{text.slice(0, cut)}<span className="fx-pv">…</span></summary>
      {text.slice(cut)}
    </details>
  );
}

function DecisionBody({ d, onChoose, busy, err, compact, hideHold }) {
  if (!d.open) return <><Answered d={d} /><Why d={d} /></>;
  return (
    <>
      <Jev d={d} />
      <Fold className="fx-why" head={<><span className="star">★</span> {d.options.find((o) => o.id === d.recommended)?.label}: </>} text={d.why} />
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
// next one is live while the server confirms; if it refuses, the card comes back on top with the reason. `front`
// ({id, nav}): the card a Needs you line, a link or Back points at comes to the top, once per navigation.
function Deck({ decisions, context, onDone, onOpen, front }) {
  const [order, setOrder] = useState([]);
  const [gone, setGone] = useState({});
  const [flying, setFlying] = useState([]);
  const [errs, setErrs] = useState({});
  const [led, setLed] = useState(null);
  if (front && front.nav !== led) {  // set while rendering (React's derived state), so it is on top in this very render
    setLed(front.nav);
    setOrder((o) => [front.id, ...o.filter((id) => id !== front.id)]);
  }
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
            <div className="fx-title">Nothing here needs you</div>
            <div className="fx-hint">New questions for this stage land here as cards. The factory keeps going on its own meanwhile.</div>
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
    if (e.target.closest("button, input, a, summary")) return;  // a summary folds and unfolds; it never drags
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
    <Card id={`fx-d-${d.id}`} tabIndex={-1} className={`fx-card fx-top${leaving ? ` leave-${leaving}` : ""}`} style={style}
          onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { drag.current = null; setDx(0); }}>
      <CardContent className="fx-stack-v">
        {dx > 40 ? <div className="fx-swipe right">★ {rec?.label}</div> : dx < -40 && canLater ? <div className="fx-swipe left">Later</div> : null}
        <div className="fx-row between">
          <span className="fx-row"><Tone tone={d.tier === "now" || d.kind === "writeback" || d.kind === "blocked" ? "red" : "amber"}>{KIND[d.kind]}</Tone>
            {d.tier === "now" ? <span className="fx-urgent">work waits on you</span> : null}
            {d.deadline ? <span className="fx-clock">⏱ {until(d.deadline)}</span> : null}</span>
          <span className="fx-hint">{pos}</span>
        </div>
        {ctx ? <button className="fx-ctx" onClick={() => onOpen(d)}>{ctx} ›</button> : null}
        <div className="fx-q">{d.question}</div>
        {d.kind !== "writeback" ? <Fold className="fx-hint" text={d.detail?.reason} /> : null}
        <DecisionBody d={d} err={err} onChoose={(o, n) => onChoose(d, o, n)} />
        <div className="fx-hint fx-gesture">{d.kind === "ask" ? `Tap an answer twice to confirm${canLater ? "; swipe left for later" : ""}` : `Swipe right for ★${canLater ? ", left for later" : ""}`}</div>
      </CardContent>
    </Card>
  );
}

function ExecutorDelivery({ item, onDone }) {
  const action = useAction(onDone);
  const sending = action.busy || item.state === "sending";
  return (
    <div className="fx-stack-v" id={`fx-d-${item.decision_id}`} tabIndex={-1}>
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

// ---- lifecycle: one workspace per stage, dispatches as rows in an engineering table -------------------------------
const STAGES = [["tickets", "Tickets"], ["verify", "Verify"], ["draft", "Draft"], ["plan", "Plan"], ["review", "Review"],
                ["run", "Run"], ["reconcile", "Reconcile"], ["archive", "Archive"]];
const SIDE = [["strategy", "Strategy"], ["learn", "Learn"], ["costs", "Costs"]];  // supporting views beside the stages, not stages
const LABEL = Object.fromEntries([...STAGES, ...SIDE]);
const IDS = new Set(Object.keys(LABEL));
const TICKET_MODES = ["tickets", "verify", "draft"];  // tickets.jsx workspaces, each with its view kept by the page
const BLANK = { q: "", filter: "all", picked: [], open: null, busy: false, err: null };
// What a stage's tab counts (the server's lifecycle.counts): tickets before they are grouped, dispatches after.
const unit = (id, n) => (id === "tickets" || id === "verify" ? plural(n, "ticket") : plural(n, "dispatch", "dispatches"));
// A stage's dispatches by the server's phase; Archive's are every archived one, from GET /archive (the overview carries
// only the last five). Null: not loaded, or not a dispatch stage.
const stageRows = (data, arch, stage) => (stage === "archive" ? arch?.rows || null
  : ["draft", "plan", "review", "run", "reconcile"].includes(stage) ? data.dispatches.filter((d) => d.phase === stage) : null);
const HEAD = { draft: "not offered to a planner yet", plan: "waiting on a plan", review: "with a plan to review",
               run: "staged or executing", reconcile: "done or written back", archive: "archived, newest first" };

// Where the page is, in the URL: ?stage=<view>, run=<run_id> (the selected dispatch), decision=<id> (what a link points
// at), ticket=<ID> (its open sheet), brief=<id> (Strategy's open brief); the host's own parameters (profile) stay.
// Older links still land: ?view=review&run=<run_id> is Review with that dispatch, ?ticket=<ID> alone Tickets with that sheet.
function readLoc() {
  const p = new URLSearchParams(location.search);
  return { stage: IDS.has(p.get("stage")) ? p.get("stage") : p.get("view") === "review" ? "review" : "tickets",
           run: p.get("run") || null, decision: Number(p.get("decision")) || null,
           ticket: p.get("ticket")?.toUpperCase() || null, brief: Number(p.get("brief")) || null };
}
function locUrl(l) {
  const u = new URL(location.href);
  u.searchParams.delete("view");
  u.searchParams.set("stage", l.stage);
  ["run", "decision", "ticket", "brief"].forEach((k) => (l[k] ? u.searchParams.set(k, l[k]) : u.searchParams.delete(k)));
  return u.href;
}

// The dashboard scrolls an element of its own, not the window.
function scroller(el) {
  for (let p = el?.parentElement; p; p = p.parentElement) if (/auto|scroll/.test(getComputedStyle(p).overflowY)) return p;
  return document.scrollingElement;
}
// Puts a stage back at its scroll: at once, or as its content comes in (a ticket list fetches when it mounts). The
// user's own scrolling wins; after 3s it gives up.
function settle(el, y) {
  const s = scroller(el), input = ["wheel", "touchstart", "keydown", "pointerdown"];
  const ro = new ResizeObserver(() => { s.scrollTop = y; if (s.scrollTop >= y - 1) stop(); });
  const t = setTimeout(stop, 3000);
  function stop() { ro.disconnect(); clearTimeout(t); input.forEach((k) => s.removeEventListener(k, stop)); }
  input.forEach((k) => s.addEventListener(k, stop, { passive: true }));
  ro.observe(el);
  return stop;
}

function DispatchRow({ d, title, needs, selected, onClick }) {
  const s = dispatchStatus(d);
  const cards = d.tickets || [];
  const repos = (() => { try { return JSON.parse(d.repos_json || "[]").map((r) => r.repo.split("/").pop()); } catch { return []; } })();
  return (
    <div id={`fx-run-${d.run_id}`} className={`fx-tr${selected ? " sel" : ""}`} onClick={onClick} role="button" tabIndex={0}
         aria-current={selected ? "true" : undefined}
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

// Run, beside the selected dispatch: the server's runtime read (dispatch_status.runtime, from recorded activity, open
// decisions and executor deliveries). None (an older server, or not staged/executing): nothing shown, nothing guessed.
// Its blocker or next step names any decision it is about (#id); that is answered in Run's own deck, never here.
function Runtime({ r }) {
  if (!r) return null;
  return (
    <dl className="fx-rt">
      <dt>Last activity</dt>
      <dd>{r.last_activity_at ? `${r.last_activity_kind || "activity"} · ${ago(r.last_activity_at)}` : "none recorded"}</dd>
      {r.blocker ? <><dt>Blocker</dt><dd className="blk">{r.blocker}</dd></> : null}
      <dt>Next</dt>
      <dd>{r.next_step}</dd>
    </dl>
  );
}

// Run, beside the selected dispatch: the pinned Strategy brief (when the run came from an approved brief), linking
// back to Strategy, and its resource claims. Nothing here is inferred: the server records both.
function BriefLink({ d, onGo }) {
  const b = d.brief_id, resources = d.resources || [];
  if (!b && !resources.length) return null;
  return (
    <div className="fx-stack-v fx-line">
      {b ? <div className="fx-row"><span className="fx-hint">Pinned brief</span>
        <button className="fx-link-btn" onClick={() => onGo({ stage: "strategy", brief: b }, { jump: true })}>#{b} ›</button></div> : null}
      {resources.length ? <div className="fx-hint">Claims: {resources.join(", ")}</div> : null}
    </div>
  );
}

// Run: the launch reservation (reserved|sent|uncertain), visible so an uncertain send is never silently replayed. An
// uncertain or reserved launch offers an explicit recovery — a human attests the send never landed and names why —
// which releases it for a normal handoff.
function LaunchRecovery({ d, onDone }) {
  const l = d.launch;
  const [reason, setReason] = useState("");
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  if (!l) return null;
  const uncertain = l.state === "uncertain";
  const releasable = l.state === "reserved" || l.state === "uncertain";
  const go = async () => {
    if (!armed) { setArmed(true); return; }
    if (!reason.trim()) return;
    setBusy(true); setErr(null);
    try {
      const r = await post(`/dispatch/${encodeURIComponent(d.run_id)}/release-unsent`,
                           { confirm_unsent: true, reason: reason.trim() });
      setBusy(false); setArmed(false); setReason("");
      onDone(r, null, `Released ${d.run_id}'s ${l.state} launch`);
    } catch (e) {
      setBusy(false); setErr(errText(e));
    }
  };
  return (
    <div className={`fx-stack-v fx-line${uncertain ? " fx-sw" : ""}`}>
      <div className="fx-row"><Tone tone={uncertain ? "red" : l.state === "sent" ? "blue" : "amber"}>launch {l.state}</Tone>
        <span className="fx-hint">pane {l.pane_id || "—"}</span></div>
      {uncertain ? <div className="fx-err">The send to pane {l.pane_id} is uncertain and is not auto-replayed.</div> : null}
      {l.error ? <Fold className="fx-hint" head="Send error: " text={l.error} /> : null}
      {releasable ? (armed ? (
        <div className="fx-row">
          <Input autoFocus value={reason} maxLength={2000} disabled={busy} placeholder="Reason: why is the send known unsent?"
                 onChange={(e) => setReason(e.target.value)} />
          <Button size="sm" disabled={busy || !reason.trim()} onClick={go}>{busy ? "Releasing…" : "Confirm release"}</Button>
        </div>
      ) : (
        <div><Button size="sm" ghost onClick={go}>Release unsent launch</Button></div>
      )) : null}
      {err ? <ActErr err={err} /> : null}
    </div>
  );
}

// Run: the execution scheduler's read (capacity in use, launch reservations, held resource claims), straight from the
// DB — capacity shows even at zero, reservations/uncertain launches honestly, and held claims are distinct from
// running slots (they persist through done/reconcile until archive).
function SchedulerStatus({ s, onGo }) {
  if (!s) return null;
  const launches = s.launches || [], holders = s.holders || [];
  const cap = s.max_parallel ?? 2, used = s.capacity_used ?? 0;
  const uncertain = launches.filter((l) => l.state === "uncertain");
  return (
    <details className="fx-sec fx-fold">
      <summary>Scheduling <span className="fx-count">{used}/{cap}</span></summary>
      <div className="fx-hint fx-line">parallel cap {cap} · {used} in use · {plural(launches.length, "launch reservation")}</div>
      {uncertain.length ? (
        <div className="fx-err fx-line">{plural(uncertain.length, "launch")} uncertain — open the run to confirm unsent and release it.</div>
      ) : null}
      {launches.length ? (
        <div className="fx-hint fx-line">{launches.map((l) => (
          <span key={l.run_id}>
            <button className="fx-link-btn" onClick={() => onGo({ stage: "run", run: l.run_id })}>{l.run_id} ›</button>
            {` ${l.state} (pane ${l.pane_id || "—"})`}
            {" · "}
          </span>
        ))}</div>
      ) : null}
      {holders.length ? (
        <div className="fx-hint fx-line">held claims (not running): {holders.map((h) => `${h.resource}@${h.run_id}:${h.state}`).join(", ")}</div>
      ) : null}
    </details>
  );
}

// Draft and Plan, from the record only: when planning was requested (the plan gate's offer, or a replan) and why the
// planner's last plan was refused. Whether a planner is at work right now is not recorded, so it is never shown.
function Planning({ d }) {
  if (d.state !== "draft" || d.planned_at) return null;
  const at = d.planning_requested_at;
  return (
    <div className="fx-stack-v fx-line">
      <div className="fx-hint">{at ? `Planning requested ${ago(at)} (${localTime(at)}).` : "No offer to a planner recorded."}</div>
      {d.planning_error ? <Fold className="fx-err" head="Last plan refused: " text={d.planning_error} /> : null}
    </div>
  );
}

// Archive: how the dispatch got here, from its transition log, and why it was rejected if it was.
function Audit({ d }) {
  return (
    <div className="fx-stack-v fx-line">
      {d.rejected_reason ? <Fold className="fx-hint" head="Rejected: " text={d.rejected_reason} /> : null}
      <ol className="fx-audit">{(d.transitions || []).map((t, i) => (
        <li key={i}>{t.from_state || "new"} → {t.to_state} · {t.actor} · {localTime(t.at)}</li>))}</ol>
    </div>
  );
}

// Reconcile: every Linear write not settled yet (planned, sent, failed, or held on an open decision), sweeps and
// follow-ups too, as recorded. Nothing here applies one: a held write is its decision's (#id, a card in the deck).
function Writebacks({ rows, onGo }) {
  if (!rows.length) return null;
  return (
    <section className="fx-sec" aria-label="Unsettled Linear writes">
      <div className="fx-k fx-line">{plural(rows.length, "Linear write")} not settled</div>
      <ul className="fx-writes">{rows.map((w, i) => {
        const held = w.decision === "flag";
        return (
          <li key={i} className={`w-${held ? "held" : w.status}`}>
            <span className="mark">{held ? "⏸" : w.status === "failed" ? "✕" : "…"}</span>
            {w.identifier || "?"} · Linear {w.op} · {held ? "held" : w.status} <span className="fx-hint">· {w.run_id}{w.reason ? ` · ${w.reason}` : ""}</span>
            {w.decision_id ? <> <button className="fx-link-btn" onClick={() => onGo({ stage: "reconcile", decision: w.decision_id }, { jump: true })}>
              #{w.decision_id} ›</button></> : null}
          </li>
        );
      })}</ul>
    </section>
  );
}

// A stage's dispatches and, below, the selected one's own content. The selection is the user's: a refresh never swaps
// it for another row; one that has left the stage says where it is now.
function Dispatches({ stage, rows, byRun, titles, needsOf, sel, onGo, detail }) {
  const cur = sel && rows.find((d) => d.run_id === sel);
  const now = sel && !cur && byRun[sel]?.phase !== stage ? byRun[sel] : null;
  return (<>
    <div className="fx-k fx-line">{plural(rows.length, "dispatch", "dispatches")} {HEAD[stage]}</div>
    {rows.length ? <StageTable dispatches={rows} titles={titles} needsOf={needsOf} selected={cur}
                               onSelect={(run) => run !== sel && onGo({ stage, run })} /> : null}
    {cur ? detail(cur) : sel ? (
      <div className="fx-row between fx-moved" role="status">
        <span className="fx-hint">{now ? `${sel} is in ${LABEL[now.phase]} now.`
          : stage === "archive" ? `${sel} is not in the archive.` : `${sel} is not in the last refresh.`}</span>
        {now || stage !== "archive" ? (
          <Button size="sm" ghost onClick={() => onGo({ stage: now ? now.phase : "archive", run: sel }, { jump: true })}>
            {now ? `Open in ${LABEL[now.phase]}` : "Look in Archive"} ›</Button>) : null}
      </div>
    ) : null}
  </>);
}

// ---- health, the stages' job and sync lines, throughput ---------------------------------------------------------
const JOB_NAME = { "factory-prune": "Verification", "[bot:planner] Plan drafts": "Planning",
                   "factory-propose": "Proposals", "factory-reconcile": "Write-back", "factory-backup": "Backup" };
const JOB_OK = ["ok", "success", "succeeded"];
function Health({ jobs }) {
  const name = (j) => JOB_NAME[j.name] || j.name;
  const bad = jobs.filter((j) => j.last_status && !JOB_OK.includes(j.last_status));
  return (
    <span className={`fx-health ${bad.length ? "bad" : "ok"}`} title={jobs.map((j) => `${name(j)}: ${j.last_status || "not run"}, ${epochAgo(j.last_run_at)}`).join("\n")}>
      <i />{bad.length ? bad.map((j) => `${name(j)} failed`).join(" · ") : "all jobs fine"}
    </span>
  );
}

// A cron job's own last run, as its store records it. The job covers every ticket or draft: its status and error are
// the job's, never one dispatch's or ticket's.
function JobLine({ jobs, name, of }) {
  const j = jobs.find((x) => x.name === name), head = `${JOB_NAME[name]} job (the whole job, not one ${of})`;
  if (!j) return <div className="fx-hint fx-line">{head}: not in the cron store.</div>;
  const bad = j.last_status && !JOB_OK.includes(j.last_status);
  return (
    <div className="fx-hint fx-line">
      {head}: {j.paused_at || j.enabled === false ? "paused; " : ""}
      {j.last_run_at ? `last ran ${epochAgo(j.last_run_at)}, ${j.last_status || "no status"}` : "not run yet"}
      {j.schedule_display ? ` · ${j.schedule_display}` : ""}
      {bad && j.last_error ? <Fold className="fx-err" text={j.last_error} /> : null}
    </div>
  );
}

// Tickets: how fresh the ledger is: when Linear was last ingested (tickets changed since the run before), the trunks.
function Sync({ st }) {
  const trunks = st.trunks || [], oldest = trunks.map((t) => t.fetched_at).sort()[0];
  const linear = (st.sync || []).map((c) => `Linear ingested ${ago(c.last_run_at)}, ${plural(c.last_count, "ticket")} changed`);
  return (
    <div className="fx-hint fx-line">
      {[...(linear.length ? linear : ["Linear not ingested yet"]),
        trunks.length ? `${plural(trunks.length, "trunk")} fetched, the oldest ${ago(oldest)}` : null].filter(Boolean).join(" · ")}
    </div>
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
// A tab strip: arrows, Home and End move the focus, Enter or Space opens the tab (so moving along never piles up
// history entries), and one Tab key press reaches it: the open tab, else the first.
function Tabs({ label, items, current, counts, onPick }) {
  const keys = (e) => {
    const tabs = [...e.currentTarget.querySelectorAll('[role="tab"]')], i = tabs.indexOf(document.activeElement);
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || j === undefined) return;
    e.preventDefault();
    tabs[(j + tabs.length) % tabs.length].focus();
  };
  const here = items.some(([id]) => id === current);
  return (
    <div role="tablist" aria-label={label} className="fx-tabs" onKeyDown={keys}>
      {items.map(([id, name], k) => {
        const on = id === current, n = counts?.[id];
        return (
          <button key={id} id={`fx-tab-${id}`} role="tab" aria-selected={on} aria-controls={on ? "fx-pane" : undefined}
                  tabIndex={on || (!here && k === 0) ? 0 : -1} className={`fx-stage-tab${on ? " on" : ""}`}
                  aria-label={n == null ? undefined : `${name}, ${unit(id, n)}`} title={n == null ? undefined : unit(id, n)}
                  onClick={() => on || onPick(id)}>
            {name}{n == null ? null : <span className="fx-count">{n}</span>}
          </button>
        );
      })}
    </div>
  );
}

// Needs you: everything waiting on a person, folded to one line; open, each line goes to its stage and the thing itself.
function NeedsYou({ items, answers, onGo }) {
  const ref = useRef(null);
  const n = items.length;
  const pick = (t) => { ref.current.open = false; onGo(t); };
  return (
    <details className="fx-needs-menu" ref={ref}>
      <summary className={`fx-hello${n ? " you" : ""}`}>
        {n ? `${plural(n, "thing")} need${n === 1 ? "s" : ""} you`
          : answers ? `${plural(answers, "ticket")} need${answers === 1 ? "s" : ""} an answer in Linear` : "All clear"}
      </summary>
      <ul className="fx-needs-list">
        {items.map((t) => (
          <li key={t.key}><button type="button" className="fx-need" onClick={() => pick(t)}>
            <span className="fx-row"><Tone tone={t.tone}>{t.kind}</Tone><span className="fx-hint">{LABEL[t.stage]} ›</span></span>
            <span>{clip(t.text, 140)}</span>
          </button></li>
        ))}
        {answers ? (
          <li><button type="button" className="fx-need" onClick={() => pick({ stage: "verify", filter: "answer" })}>
            <span className="fx-row"><Tone tone="amber">Answer in Linear</Tone><span className="fx-hint">Verify ›</span></span>
            <span>{plural(answers, "ticket")} waiting on an answer in Linear</span>
          </button></li>
        ) : null}
        {n || answers ? null : <li className="fx-hint">Nothing waits on you. A new question shows up here and in its stage.</li>}
      </ul>
    </details>
  );
}

function FactoryPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [live, setLive] = useState(false);
  const [toast, setToast] = useState(null);
  const [loc, setLoc] = useState(readLoc);  // where the page is: stage, its selected dispatch, a target decision, open ticket
  const [nav, setNav] = useState(0);        // counts deliberate moves and Back/Forward; a refresh never changes it
  // tickets.jsx's views, one per ticket workspace, kept here so each outlives leaving it (a pending draft included)
  const [views, setViews] = useState(() => {
    const l = readLoc();
    return Object.fromEntries(TICKET_MODES.map((m) => [m, { ...BLANK, open: m === l.stage ? l.ticket : null }]));
  });
  // one stable React-style setter per workspace (a next view or an updater): a late reply patches its own mode's view
  const setView = useMemo(() => Object.fromEntries(TICKET_MODES.map((m) => [m, (next) =>
    setViews((vs) => ({ ...vs, [m]: typeof next === "function" ? next(vs[m]) : next }))])), []);
  // Strategy's own view, kept here so it outlives leaving the tab (a pending groom or its error included)
  const [strat, setStrat] = useState(() => {
    const l = readLoc();
    return { q: "", picked: [], open: l.stage === "strategy" ? l.brief : null, busy: null, err: null };
  });
  const setStrategy = (next) => setStrat((s) => ({ ...s, ...(typeof next === "function" ? next(s) : next) }));
  const [arch, setArch] = useState({ rows: null, err: null });  // GET /archive, kept while elsewhere
  const root = useRef(null);
  const strip = useRef(null);
  const mem = useRef({});                 // per stage, while elsewhere: its selected dispatch and scroll
  const after = useRef({ focus: true });  // what the next navigation's render does: focus its target, restore a scroll
  const canon = useRef(false);
  const latest = useRef(null);            // this render's state, for handlers that run later (Back, a late reply)
  latest.current = { loc, views, data, arch, strat };
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
  useEffect(() => {  // Back and Forward: that entry's stage, selection and scroll; a ticket view keeps its live busy and err
    const pop = () => {
      const cur = latest.current.loc;
      mem.current[cur.stage] = { run: cur.run, scroll: root.current ? scroller(root.current).scrollTop : 0 };
      const l = readLoc();
      if (TICKET_MODES.includes(l.stage)) setViews((vs) => ({ ...vs, [l.stage]: { ...vs[l.stage], open: l.ticket } }));
      if (l.stage === "strategy") setStrat((s) => ({ ...s, open: l.brief }));
      setLoc(l);
      setNav((n) => n + 1);
      after.current = { scroll: history.state?.fx?.scroll ?? mem.current[l.stage]?.scroll ?? 0 };
    };
    addEventListener("popstate", pop);
    return () => removeEventListener("popstate", pop);
  }, []);
  useEffect(() => {  // first overview: a stage with no dispatch named pins its first row; an old link gets the new form
    if (!data || canon.current) return;
    canon.current = true;
    const l = latest.current.loc, first = l.run || l.decision ? null : stageRows(data, null, l.stage)?.[0];
    const next = first ? { ...l, run: first.run_id } : l;
    history.replaceState(history.state, "", locUrl(next));
    if (first) setLoc(next);
  }, [data]);
  const archived = data?.lifecycle?.counts?.archive;
  useEffect(() => {  // Archive: fetched on entering it, and again when a dispatch gets archived while it is open
    if (loc.stage !== "archive" || archived == null) return;
    let current = true;  // a reply after leaving, or to an older fetch, is dropped
    SDK.fetchJSON(`${API}/archive`).then((rows) => current && setArch({ rows, err: null }),
                                          (e) => current && setArch((a) => ({ ...a, err: errText(e) })));
    return () => { current = false; };
  }, [loc.stage === "archive", archived]);
  React.useLayoutEffect(() => {  // after a navigation, never a refresh: focus what it points at, or restore the scroll
    const a = after.current;
    if (!a || !data) return;
    after.current = null;
    if (a.focus && a.scroll != null) scroller(root.current).scrollTop = a.scroll;
    const el = a.focus && ((loc.decision && document.getElementById(`fx-d-${loc.decision}`))
      || (loc.run && document.getElementById(`fx-run-${loc.run}`))
      || (loc.brief && document.getElementById(`fx-brief-${loc.brief}`)));
    if (el) return void el.focus();
    if (a.scroll != null) return settle(root.current, a.scroll);
  }, [nav, !data]);
  useEffect(() => {  // the open tab fully in view (nearest edge), scrolling the strip only, never the page
    const s = strip.current, t = s?.querySelector(".on");
    if (!t) return;
    const a = s.getBoundingClientRect(), b = t.getBoundingClientRect();
    if (b.left < a.left) s.scrollLeft -= a.left - b.left + 4;
    else if (b.right > a.right) s.scrollLeft += b.right - a.right + 4;
  }, [loc.stage, !data]);

  if (!data) return <div className="fx">{error ? <div className="fx-err">{error}</div> : <div className="fx-hint">Loading…</div>}</div>;

  // Every deliberate move (a tab, a row, a Needs you line, a link, a ticket sheet) is one history entry, so Back returns
  // to it. Another stage comes back at its own scroll, or at the thing a jump points at; within a stage the page stays
  // put. Entered with nothing selected, a live stage pins its first dispatch then, never on a later refresh (Archive's
  // history is the user's to pick from).
  const go = (to, { jump = false } = {}) => {
    if (!IDS.has(to.stage)) return;  // a stage this page doesn't have: stay put
    const { loc: cur, views: vs, data: dt, arch: ar, strat: st } = latest.current;
    const y = root.current ? scroller(root.current).scrollTop : 0, same = to.stage === cur.stage;
    mem.current[cur.stage] = { run: cur.run, scroll: y };
    const first = to.stage !== "archive" && stageRows(dt, ar, to.stage)?.[0];
    const next = {
      stage: to.stage,
      run: to.run !== undefined ? to.run : same ? cur.run : mem.current[to.stage]?.run || first?.run_id || null,
      decision: to.decision || null,
      ticket: to.ticket !== undefined ? to.ticket?.toUpperCase() || null : vs[to.stage]?.open || null,
      brief: to.stage === "strategy" ? (to.brief !== undefined ? to.brief || null : st.open || null) : null,
    };
    const url = locUrl(next);
    if (url !== location.href) {  // this entry keeps its scroll for Back; the router's own state rides along
      history.replaceState({ ...history.state, fx: { scroll: y } }, "", location.href);
      history.pushState({ ...history.state, fx: null }, "", url);
    }
    if (to.ticket !== undefined && vs[to.stage]) setView[to.stage]((v) => ({ ...v, open: next.ticket }));
    if (to.brief !== undefined) setStrategy({ open: to.brief });
    if (to.sources) setStrategy((s) => ({ ...s, picked: to.sources, q: "" }));  // a Draft handoff pre-selects sources
    setLoc(next);
    setNav((n) => n + 1);
    after.current = jump ? { focus: true, scroll: same ? null : 0 } : same ? null : { scroll: mem.current[to.stage]?.scroll || 0 };
  };
  const jumpTo = (t) => {  // a Needs you line; the Linear answers line opens Verify on that filter
    if (t.filter) setView[t.stage]((v) => ({ ...v, filter: t.filter, q: "" }));
    go(t, { jump: true });
  };
  const done = (r, d, msg) => {  // a toast and a refresh; where the page is stays the user's
    const o = d && d.options.find((x) => x.id === r?.chosen);
    setToast({ type: r?.after_error || r?.handoff_error ? "error" : "success",
               message: msg || (r?.after_error ? `Recorded, but: ${r.after_error}` : r?.handoff_error ? `Approved; starting is retried: ${r.handoff_error}` : `${o?.label || "Done"} ✓`) });
    load();
  };

  // Decisions waiting on a person (the factory takes the auto ones itself), work waiting on you first, then the nearest
  // deadline. Each lives in the stage the server's `phase` names, and only there.
  const open = data.status.decisions;
  const waiting = open.filter((x) => x.tier !== "auto").sort((p, q) => (q.tier === "now") - (p.tier === "now")
    || (p.deadline ? Date.parse(p.deadline) : Infinity) - (q.deadline ? Date.parse(q.deadline) : Infinity)
    || KIND_ORDER.indexOf(p.kind) - KIND_ORDER.indexOf(q.kind) || p.id - q.id);
  const byRun = Object.fromEntries(data.dispatches.map((d) => [d.run_id, d]));
  const reviewOf = Object.fromEntries(open.filter((x) => x.kind === "review").map((x) => [x.run_id, x]));
  const deliveries = data.status.executor_deliveries || [];
  const tix = Object.fromEntries(data.tickets.map((t) => [t.identifier, t]));
  const done4u = data.status.done_for_you || [];
  const needsOf = (runId) => waiting.filter((x) => x.run_id === runId).length;
  // Needs you, one line per thing, each to its stage and the thing itself: an executor answer not delivered (Run), a
  // draft's planner questions with its review (its plan in Review, at the first open question), any other decision (its
  // card in its stage's deck, with its dispatch selected when that dispatch is in the same stage).
  const grouped = new Set();
  const targets = [
    ...deliveries.map((x) => ({ key: `e${x.decision_id}`, stage: "run", run: x.run_id, decision: x.decision_id, text: x.question,
                                tone: x.state === "sending" ? "amber" : "red", kind: x.state === "sending" ? "Answer sending" : "Answer not delivered" })),
    ...waiting.flatMap((x) => {
      if (x.phase !== "review") {
        return [{ key: `d${x.id}`, stage: x.phase, run: byRun[x.run_id]?.phase === x.phase ? x.run_id : undefined, decision: x.id,
                  tone: x.tier === "now" || x.kind === "writeback" || x.kind === "blocked" ? "red" : "amber", kind: KIND[x.kind], text: x.question }];
      }
      if (grouped.has(x.run_id)) return [];
      grouped.add(x.run_id);
      const qs = waiting.filter((y) => y.kind === "plan" && y.run_id === x.run_id).sort((a, b) => a.id - b.id);
      const d = byRun[x.run_id];
      return [{ key: `r${x.run_id}`, stage: "review", run: x.run_id, decision: (qs[0] || reviewOf[x.run_id] || x).id, tone: "amber",
                kind: "Review", text: `${d ? dispatchTitle(d, titles) : x.run_id}${qs.length ? ` · ${plural(qs.length, "question")}` : ""}` }];
    }),
  ];
  const context = (x) => (byRun[x.run_id] ? clip(dispatchTitle(byRun[x.run_id], titles), 60) : x.identifier ? `${x.identifier} ${clip(x.title, 50)}` : null);
  const openCtx = (x) => (byRun[x.run_id] ? go({ stage: byRun[x.run_id].phase, run: x.run_id }, { jump: true })
    : x.identifier ? go({ stage: "tickets", ticket: x.identifier }, { jump: true }) : null);
  // A stage's own decisions: the deck, light ones (★ starts, stops and writes nothing) in the Quick lane, ★ on all in one
  // tap. The one a link points at is always a card, brought to the front.
  const deckOf = (stage) => {
    const xs = waiting.filter((x) => x.phase === stage);
    const light = (x) => x.id !== loc.decision && x.tier !== "now" && !["plan", "review", "ask"].includes(x.kind)
      && !x.options.find((o) => o.id === x.recommended)?.weighty;
    const cards = xs.filter((x) => !light(x));
    return <>
      {cards.length ? <Deck decisions={cards} context={context} onDone={done} onOpen={openCtx}
                            front={loc.decision ? { id: loc.decision, nav } : null} /> : null}
      <Quick items={xs.filter(light)} context={context} onDone={done} />
    </>;
  };
  const archRun = Object.fromEntries((arch.rows || []).map((d) => [d.run_id, d]));
  const table = (stage, detail) => (
    <Dispatches stage={stage} rows={stageRows(data, arch, stage) || []} byRun={{ ...byRun, ...archRun }} titles={titles}
                needsOf={needsOf} sel={loc.run} onGo={go} detail={detail} />
  );
  const plan = (d) => <Plan key={d.run_id} d={d} tickets={tix} onDone={done} />;
  const planning = (d) => <><Planning d={d} />{plan(d)}</>;
  const ticketsOf = (m) => <TicketsTab data={data} mode={m} active view={views[m]} onViewChange={setView[m]} onDone={done} onNavigate={go} />;
  const PANES = {
    tickets: () => <><Sync st={data.status} />{ticketsOf("tickets")}</>,
    verify: () => <><JobLine jobs={data.jobs} name="factory-prune" of="ticket" />{ticketsOf("verify")}</>,
    draft: () => <>{deckOf("draft")}<section className="fx-sec">{table("draft", planning)}</section>{ticketsOf("draft")}</>,
    plan: () => <><JobLine jobs={data.jobs} name="[bot:planner] Plan drafts" of="draft" />{table("plan", planning)}</>,
    review: () => table("review", plan),
    run: () => <>{deckOf("run")}<ExecutorDeliveries items={deliveries} onDone={done} />
      <SchedulerStatus s={data.status.scheduler} onGo={go} />
      {table("run", (d) => <><Runtime r={d.runtime} /><LaunchRecovery d={d} onDone={done} /><BriefLink d={d} onGo={go} />{plan(d)}</>)}</>,
    strategy: () => <StrategyTab data={data} view={strat} onViewChange={setStrategy} onDone={done} onNavigate={go} />,
    reconcile: () => <><JobLine jobs={data.jobs} name="factory-reconcile" of="dispatch" />{deckOf("reconcile")}
      <Writebacks rows={data.writebacks || []} onGo={go} />{table("reconcile", plan)}</>,
    archive: () => (arch.rows ? <>
      {arch.err ? <div className="fx-err" role="alert">Refreshing the archive failed: {arch.err}. Showing it as last loaded.</div> : null}
      {table("archive", (d) => <><Audit d={d} />{plan(d)}</>)}
    </> : arch.err ? <div className="fx-err" role="alert">The archive did not load: {arch.err}</div> : <div className="fx-hint">Loading the archive…</div>),
    learn: () => <>{deckOf("learn")}
      <details className="fx-sec fx-fold" open>
        <summary>Learnings <span className="fx-count">{(data.learnings || []).length}</span></summary>
        <Learnings items={data.learnings || []} />
      </details>
      {done4u.length ? (
        <details className="fx-sec fx-fold">
          <summary>Done for you <span className="fx-count">{done4u.length}</span> <span className="fx-hint">this week</span></summary>
          <DoneForYou items={done4u} />
        </details>
      ) : null}</>,
    costs: () => <Throughput />,
  };

  const why = { asks: data.asks || {}, reviews: reviewOf };  // why.jsx threads, in the plan and on every decision
  const connection = <>
    <button className="fx-link-btn" onClick={load} title="Refresh factory status">
      <span className={`fx-live${live ? " on" : ""}`} />{live ? "live" : "reconnecting"} · {loadedAt ? ago(loadedAt) : ""} · Refresh
    </button>
    {error ? <div className="fx-err" role="alert">Last refresh failed: {error}. Showing last loaded decisions; refresh before acting.</div> : null}
  </>;
  return (
    <WhyContext.Provider value={why}>
    <div className="fx" ref={root}>
      <Toast toast={toast} />
      <header className="fx-head">
        <NeedsYou items={targets} answers={data.ticket_counts?.answer || 0} onGo={jumpTo} />
        <div className="fx-row fx-hint"><Health jobs={data.jobs} /><span>·</span>{connection}</div>
      </header>
      <nav className="fx-stage-tabs" ref={strip} aria-label="Factory views">
        <Tabs label="Lifecycle stages" items={STAGES} current={loc.stage} counts={data.lifecycle?.counts} onPick={(id) => go({ stage: id })} />
        <Tabs label="Supporting views" items={SIDE} current={loc.stage} onPick={(id) => go({ stage: id })} />
      </nav>
      <section key={loc.stage} id="fx-pane" className="fx-pane" role="tabpanel" aria-labelledby={`fx-tab-${loc.stage}`} tabIndex={0}>
        {PANES[loc.stage]()}
      </section>
    </div>
    </WhyContext.Provider>
  );
}

window.__HERMES_PLUGINS__.register("factory", FactoryPage);

// For plan.jsx / railway.jsx (bundled together; used at render time only, so the import cycle is harmless).
export { API, ActErr, CARD, CARD_TONE, DecisionBody, Ext, Fold, LANE_W, NoteBox, Notes, Option, RAIL_X0, Rail, Silence, Tone, Writes, clip, errText, plural, post, sortOptions, useChoose };

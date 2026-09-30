// The plan as one outline, phone first (the "configurator"; the Draft tab's view of a draft): a sticky result header,
// then ticket → result → numbered steps, each plan question a switch inside the step it is about, with its "why?"
// thread (why.jsx). Flipping a switch changes nothing on the server: the picked option's `changes` are applied to the
// outline and its `result` rewrites the header. "Lock in path" answers the open questions (POST /decisions/{id}, one by
// one), then the review decision shows. While draft, `+ note` on the root, each ticket and each step. Read-only in Run
// (the chosen path, steps ✓ from card comments) and Learn (predicted next to landed; untaken paths flip as ghosts).
import { API, ActErr, CARD, CARD_TONE, DecisionBody, Ext, NoteBox, Notes, Option, Silence, Tone, Writes, clip, errText, plural, post, sortOptions, useChoose } from "./index.jsx";
import { Jev, REL, jevFocus } from "./jev.jsx";
import { Railway } from "./railway.jsx";
import { Why } from "./why.jsx";

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useMemo, useRef } = SDK.hooks;
const { Button } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;
const stop = (e) => e.stopPropagation();

// ---- pure: questions, picks, the outline under a pick ----------------------------------------------------------
export const planQs = (d) => (d.decisions || []).filter((x) => x.kind === "plan" && !x.void_reason);
export const starPick = (qs) => Object.fromEntries(qs.map((q) => [q.id, q.recommended]));
const optOf = (q, pick) => q.options.find((o) => o.id === pick[q.id]);
const byKey = (qs) => Object.fromEntries(qs.filter((q) => q.key).map((q) => [q.key, q]));

// A question with depends_on counts only while that answer is picked (and its own question counts).
export function activeQs(qs, pick) {
  const k = byKey(qs);
  const on = (q) => { const dep = q.depends_on, p = dep && k[dep.question]; return !dep || (!!p && pick[p.id] === dep.option && on(p)); };
  return qs.filter(on);
}

// Pick an option, and the answers above it that it depends on (so the path to it lights up).
export function flip(qs, pick, qid, oid) {
  const next = { ...pick, [qid]: oid }, k = byKey(qs);
  for (let q = qs.find((x) => x.id === qid), p; q?.depends_on && (p = k[q.depends_on.question]); q = p) next[p.id] = q.depends_on.option;
  return next;
}

const stepKey = (id) => id.split("/")[1].split(".").map(Number);
export const byStep = (a, b) => {
  const x = stepKey(a.id), y = stepKey(b.id);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] ?? -1) !== (y[i] ?? -1)) return (x[i] ?? -1) - (y[i] ?? -1);
  return 0;
};

// Tree nodes with every active question's picked changes applied: a changed step gets `was` (old title) and `by`
// (question id), a dropped one `gone`, a new one `added`.
export function applyPlan(tree, qs, pick) {
  const nodes = tree.map((n) => ({ ...n }));
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  activeQs(qs, pick).forEach((q) => (optOf(q, pick)?.changes || []).forEach((c) => {
    if (c.add && !byId[c.add.id]) nodes.push(byId[c.add.id] = { kind: "step", detail: "", depends_on: [], files: [], notes: [], ...c.add, added: q.id });
    const n = c.step && byId[c.step];
    if (!n) return;
    if (c.becomes == null) n.gone = q.id;
    else Object.assign(n, { was: n.was ?? n.title, title: c.becomes, by: q.id });
  }));
  return nodes;
}

// The predicted result: the dispatch's, then one line per question from its picked option (tagged with the question).
// Plans from before results existed fall back to what the option leads to.
export function results(d, qs, pick) {
  const root = (d.tree || []).find((n) => n.id === "root");
  return [...(root?.result ? [{ text: root.result }] : []),
          ...activeQs(qs, pick).map((q) => ({ q: q.id, text: optOf(q, pick)?.result || optOf(q, pick)?.leads_to })).filter((x) => x.text)];
}

export const breadcrumb = (qs, pick) => activeQs(qs, pick).map((q) => optOf(q, pick)?.label).filter(Boolean).join(" › ");

// This pick against the all-★ one: step count delta, the steps that differ, the risks taken on, the questions off ★.
export function vsStar(tree, qs, pick) {
  const star = starPick(qs);
  const off = activeQs(qs, pick).filter((q) => pick[q.id] !== star[q.id]);
  const sig = (p) => Object.fromEntries(applyPlan(tree, qs, p).filter((n) => n.kind === "step" && !n.gone).map((n) => [n.id, n.title]));
  const x = sig(pick), y = sig(star);
  const diff = new Set([...Object.keys(x), ...Object.keys(y)].filter((id) => x[id] !== y[id]));
  return { same: !off.length && !diff.size, steps: Object.keys(x).length - Object.keys(y).length, diff,
           risks: off.map((q) => optOf(q, pick)?.risk).filter(Boolean), qs: new Set(off.map((q) => q.id)) };
}

// Steps the executor reported done in a card comment ("FIN-1/2 done", "FIN-1/2: done", "FIN-1/2 is done").
export function doneSteps(events) {
  const done = new Set();
  (events || []).forEach((e) => {
    for (const m of (e.body || "").matchAll(/\b([A-Z]+-\d+\/\d+(?:\.\d+)*)\s*(?::|is)?\s*done\b/gi)) done.add(m[1].toUpperCase());
  });
  return done;
}

const stageMode = (d) => d.state === "draft" ? "draft" : ["staged", "executing", "done"].includes(d.state) ? "run" : "learn";

// ---- one question: a switch ------------------------------------------------------------------------------------
function Switch({ q, pick, onPick, locked, diff }) {
  const opts = sortOptions(q), cur = pick[q.id];
  const seg = opts.length === 2 && opts.every((o) => o.label.length <= 18);
  const star = (o) => (o.id === q.recommended ? <span className="star">★</span> : null);
  const focus = jevFocus(q);
  const detail = (o) => {
    if (!o) return null;
    const what = o.changes?.length
      ? o.changes.map((c) => (c.add ? `+ ${c.add.title}` : c.becomes ? `${c.step} → ${c.becomes}` : `drops ${c.step}`)).join(" · ")
      : o.leads_to;
    const lit = (focus === "changes" && o.changes?.length) || (focus === "result" && !o.changes?.length);
    return (<>
      <span className={`fx-opt-leads${lit ? " jev-focus" : ""}`}>{what}</span>
      {o.cost || o.risk ? (
        <span className="fx-sw-meta">
          {o.cost ? <span className={focus === "cost" ? "jev-focus" : undefined}>{o.cost}</span> : null}
          {o.cost && o.risk ? " · " : null}
          {o.risk ? <span className={focus === "risk" ? "jev-focus" : undefined}>{o.risk}</span> : null}
        </span>
      ) : null}
    </>);
  };
  return (
    <div className={`fx-sw${diff ? " diff" : ""}${locked ? " locked" : ""}`} onClick={stop}>
      <span className="fx-k">{q.open ? "Planner asks" : "Answered"} <span className="fx-id">#{q.id}</span></span>
      <div className="fx-sw-q">{q.question}</div>
      {q.now ? <div className="fx-hint">Today: {q.now}</div> : null}
      {q.evidence?.length ? <div className="fx-row">{q.evidence.map((e, i) => (
        <span key={i} className="fx-file" title={e.note || ""}>{e.path}{e.line ? `:${e.line}` : ""}</span>))}</div> : null}
      <Jev d={q} />
      {seg ? (<>
        <div className="fx-seg" role="radiogroup" aria-label={q.question}>
          {opts.map((o) => <button key={o.id} role="radio" aria-checked={o.id === cur} className={o.id === cur ? "on" : ""}
                                   disabled={locked} onClick={() => onPick(q.id, o.id)}>{star(o)}{o.label}</button>)}
        </div>
        <div className="fx-sw-detail">{detail(optOf(q, pick))}</div>
      </>) : (
        <div className="fx-opts" role="radiogroup" aria-label={q.question}>
          {opts.map((o) => (
            <button key={o.id} role="radio" aria-checked={o.id === cur} className={`fx-opt-btn fx-sw-o${o.id === cur ? " on" : ""}`}
                    disabled={locked} onClick={() => onPick(q.id, o.id)}>
              <span className="fx-opt-label">{star(o)}{o.label}</span>{detail(o)}
            </button>))}
        </div>
      )}
      {q.why ? <div className="fx-hint"><span className="star">★</span>{q.why}</div> : null}
      <Why d={q} />
    </div>
  );
}

// ---- the outline: ticket → result → numbered steps, switches inside ----------------------------------------------
function Outline({ d, qs, pick, free, onPick, cmp, tickets, onDone }) {
  const nodes = applyPlan(d.tree || [], qs, pick);
  const act = activeQs(qs, pick);
  const cards = Object.fromEntries((d.tickets || []).map((c) => [c.identifier, c]));
  const done = doneSteps(d.events);
  let tks = nodes.filter((n) => n.kind === "ticket");
  if (!tks.length) tks = (d.tickets || []).map((c) => ({ id: c.identifier, title: tickets[c.identifier]?.title }));
  const shown = new Set(nodes.filter((n) => !n.gone).map((n) => n.id).concat(tks.map((t) => t.id)));
  const on = (id) => act.filter((q) => q.node_id === id || (id === "root" && !shown.has(q.node_id)));
  const sw = (q) => <Switch key={q.id} q={q} pick={pick} onPick={onPick} locked={!free(q)} diff={cmp?.qs.has(q.id)} />;
  // notes bind the executor (dispatch.md); new ones only while draft, on nodes the plan has (not an unsent flip's)
  const notes = (n) => (<><Notes notes={n?.notes} />
    {d.state === "draft" && n && !n.added && !n.gone ? <NoteBox run={d.run_id} nodeId={n.id} onDone={onDone} /> : null}</>);
  return (
    <div className="fx-ol">
      {notes(nodes.find((n) => n.id === "root") || { id: "root" })}
      {on("root").map(sw)}
      {tks.map((t) => {
        const c = cards[t.id];
        const steps = nodes.filter((n) => n.kind === "step" && n.id.split("/")[0] === t.id).sort(byStep);
        return (
          <section key={t.id} className="fx-ol-t">
            <div className="fx-row between">
              <span className="fx-row"><span className="fx-id">{t.id}</span>
                {t.parent && t.parent !== "root" ? <span className="fx-hint">under {t.parent}</span> : null}
                {c?.pr_url ? <Ext href={c.pr_url}>PR</Ext> : null}</span>
              {c && d.state !== "draft" ? <Tone tone={CARD_TONE[c.card_status]}>{CARD[c.card_status]}</Tone> : null}
            </div>
            <div className="fx-title small">{t.title || tickets[t.id]?.title}</div>
            {t.result ? <div className="fx-ol-res">→ {t.result}</div> : null}
            {notes(nodes.find((n) => n.id === t.id) || { id: t.id })}
            {on(t.id).map(sw)}
            {steps.length ? <ol className="fx-ol-steps">{steps.map((s) => {
              const n = s.id.split("/")[1], ok = c?.card_status === "done" || done.has(s.id);
              const cls = ["fx-ol-s", s.gone && "gone", s.by && "chg", s.added && "add", cmp?.diff.has(s.id) && "diff"].filter(Boolean).join(" ");
              return (
                <li key={s.id} className={cls} style={{ marginLeft: `${(n.split(".").length - 1) * 1.1}rem` }}>
                  <div className="fx-ol-h">
                    <span className={`fx-ol-n${ok ? " ok" : ""}`}>{ok ? "✓" : s.added ? "+" : n}</span>
                    <span className="fx-ol-tt">{s.title}{s.was ? <span className="fx-ol-was">{s.was}</span> : null}</span>
                  </div>
                  {s.depends_on?.length || s.files?.length ? (
                    <div className="fx-row fx-ol-meta">
                      {(s.depends_on || []).map((x) => <span key={x} className="fx-chip">after {x.startsWith(`${t.id}/`) ? x.split("/")[1] : x}</span>)}
                      {(s.files || []).map((f) => <span key={f.path} className="fx-file">{f.path}{f.new ? " (new)" : ""}</span>)}
                    </div>) : null}
                  {s.detail ? <div className="fx-hint clamp">{s.detail}</div> : null}
                  {notes(s)}
                  {s.gone ? null : on(s.id).map(sw)}
                </li>);
            })}</ol> : null}
          </section>
        );
      })}
    </div>
  );
}

// What actually landed per ticket: card status, PR, the executor's done summary, the Linear write-backs.
function Landed({ d }) {
  const summary = (id) => [...(d.events || [])].reverse().find((e) => e.identifier === id && e.kind === "done")?.body;
  return (
    <div className="fx-landed">
      <span className="fx-k">Landed</span>
      {(d.tickets || []).map((c) => (
        <div key={c.identifier}>
          <div className="fx-row"><span className="fx-id">{c.identifier}</span><Tone tone={CARD_TONE[c.card_status]}>{CARD[c.card_status]}</Tone>
            {c.pr_url ? <Ext href={c.pr_url}>PR</Ext> : null}</div>
          {summary(c.identifier) ? <div className="fx-hint">{summary(c.identifier)}</div> : null}
          <Writes writes={(d.writes || []).filter((w) => w.identifier === c.identifier)} />
        </div>
      ))}
    </div>
  );
}

const evidence = (e) => e.type === "file" ? `${e.path}${e.line ? `:${e.line}` : ""}` : e.type === "pr" ? e.url
  : e.type === "linear" ? e.ref : e.witness ? `${e.witness}: ${clip(e.query, 80)}` : e.type;

function TicketSheet({ d, tickets, onClose }) {
  // tickets in a draft can be missing from overview.tickets (owned, in scope only): the ledger has them
  const missing = (d.tickets || []).some((c) => !tickets[c.identifier]);
  const [more, setMore] = useState({});
  const [err, setErr] = useState(null);
  useEffect(() => {
    if (missing) SDK.fetchJSON(`${API}/tickets`).then((xs) => setMore(Object.fromEntries(xs.map((t) => [t.identifier, t]))), (e) => setErr(errText(e)));
  }, [missing]);
  const title = (id) => (tickets[id] || more[id])?.title || (d.tree || []).find((n) => n.id === id)?.title;
  return (
    <div className="fx-bsheet-bg" onClick={onClose}>
      <div className="fx-bsheet" onClick={stop} role="dialog" aria-label="Tickets">
        {(d.tickets || []).map((c) => {
          const t = tickets[c.identifier] || more[c.identifier], v = t?.verdict;
          return (
            <div key={c.identifier} className="fx-stack-v fx-bsheet-t">
              <div className="fx-row">{t?.url ? <Ext href={t.url}>{c.identifier}</Ext> : <span className="fx-id">{c.identifier}</span>}
                {v ? <Tone tone={v.kind === "valid" ? "green" : "amber"}>{v.kind}</Tone> : null}</div>
              <div className="fx-ttitle">{title(c.identifier)}</div>
              {v ? <div className="fx-hint">{v.reason}</div> : null}
              {v?.evidence?.length ? <ul className="fx-ev">{v.evidence.map((e, i) => <li key={i}>{evidence(e)}{e.note ? ` · ${e.note}` : ""}</li>)}</ul> : null}
            </div>
          );
        })}
        <ActErr err={err} />
        <Button size="sm" onClick={onClose}>Close</Button>
      </div>
    </div>
  );
}

function Review({ d, c }) {
  return (<>
    <div className="fx-q small">{d.question}</div>
    <DecisionBody d={d} busy={c.busy} err={c.err} compact hideHold onChoose={(o, n) => c.choose(o, n).catch(() => {})} />
  </>);
}

// ---- the whole thing: result header + outline (+ railway on desktop) + bottom bar in review ---------------------
export function Plan({ d: current, tickets = {}, onDone, onClose }) {
  const [snapshot, setSnapshot] = useState(null);
  const d = snapshot || current;
  const mode = stageMode(d);
  const qs = useMemo(() => planQs(d), [d]);
  const [flips, setFlips] = useState({});
  const [hold, setHold] = useState(false);
  const [sheet, setSheet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const free = (q) => mode === "learn" || (mode === "draft" && q.open);  // Learn: ghost flips, nothing is sent
  const pick = Object.fromEntries(qs.map((q) => [q.id, (free(q) && q.options.some((o) => o.id === flips?.[q.id]) && flips[q.id]) || q.chosen || q.recommended]));
  const ghost = mode === "learn" && qs.some((q) => pick[q.id] !== (q.chosen || q.recommended));
  const onPick = (qid, oid) => {
    if (!busy && !holdChoice.busy) setFlips(flip(qs, flips, qid, oid));
  };
  const cmp = vsStar(d.tree || [], qs, pick);
  const res = results(d, qs, pick);
  const focusQs = Object.fromEntries(qs.map((q) => [q.id, jevFocus(q)]));
  const open = qs.filter((q) => q.open);
  const review = (d.decisions || []).find((x) => x.kind === "review" && x.open);
  const holdChoice = useChoose(review, onDone);
  const editable = (q) => free(q) && !busy && !holdChoice.busy;
  const holdOption = d.review !== "held" && review?.options.find((o) => o.id === "hold");
  const ids = (d.tickets || []).map((c) => c.identifier);
  const holdOn = () => { timer.current = setTimeout(() => setHold(true), 300); };
  const holdOff = () => { clearTimeout(timer.current); setHold(false); };
  const lockIn = async () => {
    if (busy || holdChoice.busy) return;
    setSnapshot(d); setBusy(true); setErr(null);
    try {
      for (const q of open) await post(`/decisions/${q.id}`, { option: pick[q.id] });
      onDone({}, null, "Path locked in");
    } catch (e) {
      const message = `${errText(e)}. Previously saved answers remain; refresh to see what is still open.`;
      setErr(message);
      onDone({ after_error: message });
    }
    setSnapshot(null); setBusy(false);
  };
  const vs = [cmp.steps ? `${cmp.steps > 0 ? "+" : "−"}${plural(Math.abs(cmp.steps), "step")}` : "same steps", ...cmp.risks].join(" · ");
  const ticketBtn = <Button size="sm" ghost onClick={() => setSheet(true)}>{ids.length === 1 ? ids[0] : plural(ids.length, "ticket")}</Button>;
  return (
    <div className={`fx-cfg${qs.length ? " map" : ""}`}>
      {qs.length ? <aside className="fx-cfg-map"><Railway d={d} qs={qs} pick={pick} onPick={onPick} free={editable} /></aside> : null}
      <div className="fx-cfg-main">
        <header className={`fx-res${mode === "draft" ? " sticky" : ""}`}>
          <div className="fx-row between">
            <span className="fx-k">{mode === "draft" ? "Result" : mode === "run" ? "Chosen path" : ghost ? "Predicted · ghost path" : "Predicted"}</span>
            {onClose ? <button className="fx-x" onClick={onClose} aria-label="Close">✕</button> : null}
          </div>
          <div className={mode === "learn" ? "fx-res-cols" : undefined}>
            {res.length ? <ul className="fx-res-lines">{res.map((r, i) => (
              <li key={i} className={[hold && r.q && cmp.qs.has(r.q) && "diff", r.q && focusQs[r.q] === "result" && "jev-focus"].filter(Boolean).join(" ") || undefined}>{r.text}{r.q ? <span className="fx-id">#{r.q}</span> : null}</li>))}</ul>
              : <div className="fx-hint">{d.review === "planning" ? "The planner is writing the plan." : "No predicted result in this plan."}</div>}
            {mode === "learn" ? <Landed d={d} /> : null}
          </div>
          {qs.length ? <button className={`fx-crumb${hold ? " on" : ""}`} title="Press and hold: every difference from ★"
                               onPointerDown={holdOn} onPointerUp={holdOff} onPointerLeave={holdOff} onPointerCancel={holdOff}
                               onContextMenu={(e) => e.preventDefault()}>{breadcrumb(qs, pick)}</button> : null}
          {qs.length && mode !== "run" ? <div className="fx-hint">{cmp.same ? "★ path" : `vs ★: ${vs}`}</div> : null}
          {mode === "draft" ? <div className="fx-stack-v">
            {d.review === "held" ? <div className="fx-hint">Held: no automatic start. Your plan choices are not submitted by Hold.</div> : review ? <Silence d={review} /> : null}
            {holdOption ? <Option key={review.id} d={review} o={holdOption} busy={busy || holdChoice.busy}
                                  onChoose={(o, n) => holdChoice.choose(o, n).catch(() => {})} /> : null}
            <ActErr err={holdChoice.err} />
          </div> : null}
        </header>
        <Outline d={d} qs={qs} pick={pick} free={editable} onPick={onPick} cmp={hold ? cmp : null} tickets={tickets} onDone={onDone} />
        {mode === "draft" ? (
          <div className="fx-cfg-bar">
            {open.length ? (
              <div className="fx-row">{ticketBtn}<span className="fx-grow" />
                <Button size="sm" disabled={busy || holdChoice.busy} onClick={lockIn}>{busy ? "Locking in…" : `Lock in path → (${open.length})`}</Button></div>
            ) : review ? <Review d={review} c={holdChoice} /> : <div className="fx-row">{ticketBtn}<span className="fx-hint">Nothing left to decide.</span></div>}
            <ActErr err={err} />
          </div>
        ) : null}
      </div>
      {sheet ? <TicketSheet d={d} tickets={tickets} onClose={() => setSheet(false)} /> : null}
    </div>
  );
}

// ---- deck lane: open decisions whose ★ is light (not a plan, not a review, nothing weighty), ★ on all in one go --
function QuickRow({ d, ctx, onDone }) {
  const [open, setOpen] = useState(false);
  const c = useChoose(d, onDone);
  const rec = d.options.find((o) => o.id === d.recommended);
  const rel = d.kind === "learning" ? d.jev?.relation : null;
  return (
    <li className="fx-quick-row" onClick={() => setOpen(!open)}>
      <div><span className="star">★</span><b>{rec?.label}</b> · {d.question}</div>
      {ctx ? <div className="fx-hint">{ctx}</div> : null}
      {rel && rel.learning_id != null ? (
        <div className={`fx-hint${rel.kind === "conflicts" ? " fx-err" : ""}`}>{REL[rel.kind] || "related to"} <span className="fx-id">L{rel.learning_id}</span>{rel.body ? ` · ${rel.body}` : ""}</div>
      ) : null}
      {open ? <DecisionBody d={d} busy={c.busy} err={c.err} compact onChoose={(o, n) => c.choose(o, n).catch(() => {})} /> : null}
    </li>
  );
}

export function Quick({ items, context, onDone }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  if (!items.length) return null;
  const groupOf = (d) => (d.kind === "learning" && d.jev?.group ? d.jev.group : null);
  const sets = [];  // [group, [decisions…]]; related learnings share jev.group, each still answers on its own
  const seen = new Map();
  items.forEach((d) => {
    const g = groupOf(d);
    if (!g) return;
    if (seen.has(g)) sets[seen.get(g)][1].push(d);
    else { seen.set(g, sets.length); sets.push([g, [d]]); }
  });
  const grouped = sets.filter(([, ds]) => ds.length > 1);
  const inSet = new Set(grouped.flatMap(([, ds]) => ds.map((d) => d.id)));
  const row = (d) => <QuickRow key={d.id} d={d} ctx={context(d)} onDone={onDone} />;
  const conflicts = (ds) => ds.map((d) => d.jev?.relation).filter((r) => r?.kind === "conflicts" && r.learning_id != null);
  const all = () => {
    setBusy(true); setErr(null);
    post("/decisions/ok", { ids: items.map((x) => x.id) }).then((r) => {
      const bad = r.filter((x) => x.error);
      setBusy(false);
      if (bad.length) setErr(bad.map((x) => `#${x.decision}: ${x.error}`).join("; "));
      onDone({}, null, `★ taken on ${plural(items.length - bad.length, "decision")}`);
    }, (e) => { setBusy(false); setErr(errText(e)); });
  };
  return (
    <section className="fx-quick">
      <div className="fx-row between"><span className="fx-k">Quick · {items.length}</span>
        <Button size="sm" disabled={busy} onClick={all}>{busy ? "Taking ★…" : "Take all ★"}</Button></div>
      <ul>{items.filter((d) => !inSet.has(d.id)).map(row)}</ul>
      {grouped.map(([g, ds]) => {
        const cs = conflicts(ds);
        return (
          <details key={g} className="fx-fold fx-rel" open>
            <summary>
              Related learnings <span className="fx-count">{ds.length}</span>
              {cs.length ? <span className="fx-rel-conflict">{cs.map((r) => `conflicts with L${r.learning_id}`).join(" · ")}</span> : null}
            </summary>
            <ul>{ds.map(row)}</ul>
          </details>
        );
      })}
      <ActErr err={err} />
    </section>
  );
}

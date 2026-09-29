// Factory tab: one graph of everything the factory holds, Obsidian-style. Dispatches own tickets, tickets own
// plan steps, arrows are "must happen before", flags hang off what they're about; anything unrelated floats as
// a lone node. Select a node to see where it is in its lifecycle, what each action leads to, and to act.
// Actions go through the plugin API to the factory CLI, which enforces every invariant.
// Built by install.sh (`bun build`, classic JSX via tsconfig.json) to dist/index.js; React + components come
// from the dashboard SDK (shadcn-style Nous DS), nothing is bundled.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useCallback, useRef, useMemo } = SDK.hooks;
const { Button, Badge, Card, CardHeader, CardContent, Input, ConfirmDialog, Dialog, DialogContent,
        DialogHeader, DialogTitle, DialogDescription, DialogFooter, Tabs, TabsList, TabsTrigger, Toast } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;  // for <>…</>
const REFRESH_MS = 30000;
const API = "/api/plugins/factory";

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
const BADGE = { amber: "warning", green: "success", blue: "secondary", gray: "outline", red: "destructive" };
const Tone = ({ tone, children }) => <Badge tone={BADGE[tone] || "outline"}>{children}</Badge>;
const Ext = ({ href, children }) => <a className="fx-link" href={href} target="_blank" rel="noreferrer">{children}</a>;

// One request: busy while in flight, the API's refusal text kept for display.
function useAction(onDone) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const run = (path, body) => {
    setBusy(true); setErr(null);
    return post(path, body).then((r) => { setBusy(false); onDone(r); }, (e) => { setBusy(false); setErr(errText(e)); });
  };
  return { busy, err, run, clear: () => setErr(null) };
}
const ActErr = ({ err }) => (err ? <div className="fx-err" role="alert">{err}</div> : null);

// ---- plain-language status ----------------------------------------------------------------
const RECHECK = {
  new: "Not verified yet",
  "ticket-changed": "Ticket changed in Linear since it was verified",
  "evidence-changed": "Code it was verified against has changed",
  "context-changed": "Repo mapping changed; verifying again",
};
const CARD = { ready: "not started", running: "being worked on", done: "done", blocked: "blocked" };
const CARD_TONE = { ready: "gray", running: "blue", done: "green", blocked: "amber" };
const FLAG_LABEL = (f) => f.kind === "kanban-mirror" ? "Board out of sync"
  : ({ state: "Status held", description: "Note held", comment: "Comment held" })[f.op] || "Decide";

function ticketStatus(t, skipped) {
  const v = t.verdict;
  if (t.dispatch) return { group: "dispatch", tone: CARD_TONE[t.dispatch.card_status] || "blue", label: `Card ${CARD[t.dispatch.card_status] || t.dispatch.card_status}` };
  if (!t.context) return { group: "ignored", tone: "gray", label: "Not mapped", why: "No repo is mapped for this domain, so it isn't verified." };
  if (!v || t.freshness !== "fresh") return { group: "progress", tone: "blue", label: "Checking", why: RECHECK[t.freshness] || "Queued for verification." };
  switch (v.kind) {
    case "valid": return skipped[t.identifier]
      ? { group: "nothing", tone: "gray", label: "Not for the factory now", why: skipped[t.identifier] }
      : { group: "ready", tone: "green", label: "Ready", why: v.reason };
    case "needs-clarification": return { group: "you", tone: "amber", label: "Needs your answer", why: v.reason };
    case "invalid-references": return { group: "you", tone: "amber", label: "Refers to something missing", why: `${v.target}: ${v.reason}` };
    case "already-done": return { group: "nothing", tone: "gray", label: "Already done", why: v.reason };
    case "stale": return { group: "nothing", tone: "gray", label: "No longer applies", why: v.reason };
    case "duplicate-of": return { group: "nothing", tone: "gray", label: `Duplicate of ${v.target}`, why: v.reason };
    default: return { group: "progress", tone: "blue", label: v.kind, why: v.reason };
  }
}

function dispatchStatus(d) {
  const cards = d.tickets || [];
  const n = (s) => cards.filter((c) => c.card_status === s).length;
  switch (d.state) {
    case "draft": switch (d.review) {
      case "planning": return { tone: "gray", label: "Planning…", why: "A planner is writing the plan. Nothing runs yet." };
      case "in-review": return { tone: "amber", label: `Auto-starts in ${until(d.review_until)}`, why: `Starts on its own at ${localTime(d.review_until)} unless you hold or reject it.` };
      case "held": return { tone: "amber", label: "Held", why: `Held: ${d.held_reason || "no reason given"}. Starts only when you approve.` };
      default: return { tone: "amber", label: "Needs approval", why: "Nothing starts until you approve." };
    }
    case "staged": return { tone: "blue", label: "Starting", why: `Approved by ${d.approved_by || "?"}; starting on factory-fleet.` };
    case "executing": return { tone: "blue", label: "Being worked on", why: `${n("done")} of ${cards.length} done` + (n("blocked") ? `, ${n("blocked")} blocked.` : ".") };
    case "done": return { tone: "blue", label: "Writing back", why: `${n("done")} done, ${n("blocked")} blocked; results going to Linear.` };
    case "reconciled": return { tone: "green", label: "Written back", why: "Waiting to be archived." };
    case "archived": return { tone: "green", label: "Archived", why: "" };
    default: return { tone: "gray", label: d.state, why: "" };
  }
}

// A dispatch's name: the planner's theme if it set one, else its tickets' titles.
function dispatchTitle(d, titles) {
  const root = (d.tree || []).find((n) => n.id === "root");
  if (root && root.title && root.title !== d.run_id) return root.title;
  const ids = (d.tickets || []).map((c) => c.identifier);
  if (ids.length === 1) return titles[ids[0]] || ids[0];
  return ids.length ? `${ids.length} tickets: ${ids.join(", ")}` : d.run_id;
}

// ---- graph model -----------------------------------------------------------------------------
// Edge kinds: own (dispatch → ticket → step), after (prerequisite → dependent: the flow), about (flag → subject),
// dup (duplicate → original). own/after point left to right.
function buildGraph(data, show, titles) {
  const nodes = new Map();
  const edges = [];
  const add = (n) => { if (!nodes.has(n.id)) nodes.set(n.id, n); return nodes.get(n.id); };
  const link = (s, t, kind) => { if (nodes.has(s) && nodes.has(t) && s !== t) edges.push({ s, t, kind }); };
  const skipped = Object.fromEntries((data.candidates?.skipped || []).map((x) => [x.identifier, x.reason]));
  const byTicket = Object.fromEntries(data.tickets.map((t) => [t.identifier, t]));
  const ticket = (id, extra) => add({ id: `t:${id}`, kind: "ticket", ident: id, tone: "gray", label: id, title: titles[id] || id,
                                      t: byTicket[id], ...extra });

  data.tickets.forEach((t) => {
    const st = ticketStatus(t, skipped);
    if (!t.dispatch && (st.group === "nothing" || st.group === "ignored") && !show.handled) return;
    if (st.group === "progress" && !show.checking) return;
    ticket(t.identifier, { tone: st.tone, st, you: st.group === "you", ready: st.group === "ready" });
  });

  data.dispatches.forEach((d) => {
    const s = dispatchStatus(d);
    const did = `d:${d.run_id}`;
    add({ id: did, kind: "dispatch", tone: s.tone, st: s, d, you: d.state === "draft" && d.review !== "planning",
          label: dispatchTitle(d, titles), title: dispatchTitle(d, titles) });
    const cards = Object.fromEntries((d.tickets || []).map((c) => [c.identifier, c]));
    const tree = d.tree || [];
    const kindOf = Object.fromEntries(tree.map((n) => [n.id, n.kind]));
    const nid = (id) => (id == null || id === "root" ? did : kindOf[id] === "ticket" ? `t:${id}` : `s:${d.run_id}:${id}`);
    (d.tickets || []).forEach((c) => {
      const n = ticket(c.identifier, {});
      Object.assign(n, { tone: CARD_TONE[c.card_status] || "blue", card: c, run: d.run_id, you: false, ready: false });
    });
    tree.forEach((n) => {
      if (n.kind === "dispatch") return;
      if (n.kind === "step") {
        if (!show.steps) return;
        const tk = n.id.split("/")[0];
        add({ id: nid(n.id), kind: "step", tone: CARD_TONE[cards[tk]?.card_status] || "gray", label: n.id.split("/").slice(1).join("/"),
              title: n.title, node: n, d });
      }
      const me = nodes.get(nid(n.id));
      if (me) me.node = n;
    });
    tree.forEach((n) => {
      if (n.kind === "dispatch") return;
      // A step that waits on another hangs off that one, not its ticket: the picture shows the flow, not a fan.
      if (!(n.kind === "step" && n.depends_on?.length)) link(nid(n.parent), nid(n.id), "own");
      (n.depends_on || []).forEach((dep) => link(nid(dep), nid(n.id), "after"));
    });
    if (!tree.length) (d.tickets || []).forEach((c) => link(did, `t:${c.identifier}`, "own"));
  });

  (data.status.open_flags || []).forEach((f) => {
    add({ id: `f:${f.id}`, kind: "flag", tone: "red", you: true, label: FLAG_LABEL(f), title: `${FLAG_LABEL(f)}: ${f.title || f.identifier || f.kind}`, f });
    if (f.identifier) { ticket(f.identifier, {}); link(`f:${f.id}`, `t:${f.identifier}`, "about"); }
    else if (f.run_id) link(`f:${f.id}`, `d:${f.run_id}`, "about");
  });

  if (show.closed) {
    (data.status.archived || []).forEach((a) => {
      const ids = String(a.tickets || "").split(/[,\s]+/).filter(Boolean);
      add({ id: `d:${a.run_id}`, kind: "dispatch", tone: "green", closed: true, a, label: titles[ids[0]] && ids.length === 1 ? titles[ids[0]] : a.run_id,
            title: `Dispatch ${a.run_id}`, st: { tone: "green", label: "Archived", why: `${a.done} done` + (a.blocked ? `, ${a.blocked} blocked` : "") + ` · archived ${ago(a.archived_at)}` } });
      ids.forEach((id) => { const n = ticket(id, {}); if (n.tone === "gray") Object.assign(n, { tone: "green", closed: true }); link(`d:${a.run_id}`, `t:${id}`, "own"); });
    });
    (data.status.written_back || []).forEach((w) => {
      const n = ticket(w.identifier, {});
      Object.assign(n, { tone: "green", closed: true, w, title: w.title });
    });
  }
  // duplicates point at their original, which appears as a node even if it's otherwise out of view
  nodes.forEach((n) => {
    const target = n.t?.verdict?.kind === "duplicate-of" ? n.t.verdict.target : n.w?.kind === "duplicate-of" ? n.w.target : null;
    if (target) { ticket(target, {}); link(n.id, `t:${target}`, "dup"); }
  });
  return { nodes: [...nodes.values()], edges };
}

// ---- force layout ----------------------------------------------------------------------------
// ponytail: O(n²) repulsion; fine for the few hundred nodes the factory holds, use a quadtree if it grows past ~1000.
const R = { dispatch: 11, ticket: 7, step: 4.5, flag: 6 };
const LEN = { own: 70, after: 50, about: 40, dup: 90 };
const LBL = { dispatch: 13, ticket: 11, step: 10, flag: 11 };  // screen px at zoom 1; shrinks as you zoom in
const seed = (id) => { let x = 0; for (const c of id) x = (x * 31 + c.charCodeAt(0)) | 0; return x; };

function useLayout(graph) {
  const pos = useRef(new Map());
  const alpha = useRef(1);
  const raf = useRef(0);
  const [frame, setFrame] = useState(0);

  const tick = useCallback(() => {
    const ns = graph.nodes.map((n) => pos.current.get(n.id));
    const a = alpha.current;
    for (let i = 0; i < ns.length; i++) {
      const p = ns[i];
      for (let j = i + 1; j < ns.length; j++) {
        const q = ns[j];
        const dx = q.x - p.x, dy = q.y - p.y;
        const d2 = Math.max(dx * dx + dy * dy, 30);
        if (d2 > 160000) continue;
        const f = (900 * a) / d2, d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        p.vx -= fx; p.vy -= fy; q.vx += fx; q.vy += fy;
      }
      p.vx -= p.x * 0.01 * a; p.vy -= p.y * 0.012 * a;
    }
    graph.edges.forEach((e) => {
      const p = pos.current.get(e.s), q = pos.current.get(e.t);
      const dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = ((d - LEN[e.kind]) / d) * 0.08 * a;
      p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f;
      if (e.kind === "own" || e.kind === "after") {  // flows read left to right
        const lag = p.x + LEN[e.kind] * 0.8 - q.x;
        if (lag > 0) { q.vx += lag * 0.12 * a; p.vx -= lag * 0.12 * a; }
        q.vy += (p.y - q.y) * 0.02 * a;  // and stay roughly level with what they follow
      }
    });
    ns.forEach((p) => {
      if (p.fx != null) { p.x = p.fx; p.y = p.fy; p.vx = p.vy = 0; return; }
      p.vx *= 0.6; p.vy *= 0.6; p.x += p.vx; p.y += p.vy;
    });
    alpha.current *= 0.975;
  }, [graph]);

  const animate = useCallback(() => {
    cancelAnimationFrame(raf.current);
    const loop = () => {
      tick();
      setFrame((f) => f + 1);
      if (alpha.current > 0.004) raf.current = requestAnimationFrame(loop);
    };
    raf.current = requestAnimationFrame(loop);
  }, [tick]);

  const heat = useCallback((to) => { alpha.current = Math.max(alpha.current, to); animate(); }, [animate]);

  // Seed new nodes next to a placed neighbour so a refresh doesn't reshuffle the picture.
  const fresh = useMemo(() => {
    const P = pos.current;
    const first = P.size === 0;
    const live = new Set(graph.nodes.map((n) => n.id));
    [...P.keys()].forEach((id) => { if (!live.has(id)) P.delete(id); });
    let added = 0;
    graph.nodes.forEach((n) => {
      if (P.has(n.id)) return;
      const nb = graph.edges.find((e) => (e.s === n.id && P.has(e.t)) || (e.t === n.id && P.has(e.s)));
      const at = nb ? P.get(nb.s === n.id ? nb.t : nb.s) : { x: 0, y: 0 };
      const r = seed(n.id), ang = (r % 628) / 100, dist = nb ? 30 : 150 + (Math.abs(r) % 150);
      P.set(n.id, { x: at.x + Math.cos(ang) * dist, y: at.y + Math.sin(ang) * dist, vx: 0, vy: 0 });
      added++;
    });
    if (first) { alpha.current = 1; for (let i = 0; i < 260; i++) tick(); }  // settle before the first paint
    return { first, added };
  }, [graph, tick]);
  useEffect(() => { if (fresh.added && !fresh.first) heat(0.4); }, [fresh, heat]);
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  return { pos: pos.current, heat, frame, first: fresh.first };
}

// ---- graph view ------------------------------------------------------------------------------
function GraphView({ graph, selected, picked, onSelect, onPick, fitKey }) {
  const { pos, heat } = useLayout(graph);
  const box = useRef(null);
  const svg = useRef(null);
  const drag = useRef(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 800, h: 560 });
  const [hover, setHover] = useState(null);

  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);
  const fit = useCallback(() => {
    const ps = graph.nodes.map((n) => pos.get(n.id)).filter(Boolean);
    if (!ps.length) return;
    const xs = ps.map((p) => p.x), ys = ps.map((p) => p.y);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const k = Math.min(size.w / (x1 - x0 + 220), size.h / (y1 - y0 + 140), 1.8);
    setView({ k, x: size.w / 2 - ((x0 + x1) / 2) * k, y: size.h / 2 - ((y0 + y1) / 2) * k });
  }, [graph, pos, size]);
  useEffect(fit, [fitKey, size.w, size.h]);  // fit on open, on resize, and on the Fit button

  // Wheel zoom around the cursor (non-passive so the page doesn't scroll).
  useEffect(() => {
    const el = svg.current;
    const onWheel = (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      setView((v) => {
        const k = Math.min(4, Math.max(0.25, v.k * Math.exp(-e.deltaY * 0.0015)));
        return { k, x: mx - ((mx - v.x) * k) / v.k, y: my - ((my - v.y) * k) / v.k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const world = (e) => {
    const r = svg.current.getBoundingClientRect();
    return { x: (e.clientX - r.left - view.x) / view.k, y: (e.clientY - r.top - view.y) / view.k };
  };
  const down = (e, id) => {
    e.stopPropagation();
    svg.current.setPointerCapture(e.pointerId);
    drag.current = id ? { id, sx: e.clientX, sy: e.clientY, moved: false, shift: e.shiftKey }
                      : { pan: true, sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  };
  const move = (e) => {
    const g = drag.current;
    if (!g) return;
    if (Math.abs(e.clientX - g.sx) + Math.abs(e.clientY - g.sy) > 4) g.moved = true;
    if (!g.moved) return;
    if (g.pan) { setView((v) => ({ ...v, x: g.vx + e.clientX - g.sx, y: g.vy + e.clientY - g.sy })); return; }
    const p = pos.get(g.id), w = world(e);
    p.fx = w.x; p.fy = w.y;
    heat(0.25);
  };
  const up = () => {
    const g = drag.current;
    drag.current = null;
    if (!g) return;
    if (g.id) {
      const p = pos.get(g.id);
      if (p) p.fx = p.fy = null;
      if (!g.moved) (g.shift ? onPick(g.id) : onSelect(g.id));
    } else if (!g.moved) onSelect(null);
  };

  const focus = hover || selected;
  const near = useMemo(() => {
    if (!focus) return null;
    // everything it touches, plus everything downstream of it: what it leads to
    const s = new Set([focus]);
    graph.edges.forEach((e) => { if (e.s === focus) s.add(e.t); if (e.t === focus) s.add(e.s); });
    const seen = new Set([focus]), down = [focus];
    while (down.length) {
      const id = down.pop();
      graph.edges.forEach((e) => {
        if (e.s === id && (e.kind === "own" || e.kind === "after") && !seen.has(e.t)) { seen.add(e.t); s.add(e.t); down.push(e.t); }
      });
    }
    return s;
  }, [focus, graph]);

  const showLabel = (n) => n.id === focus || near?.has(n.id) || n.kind === "dispatch" || n.you
    || (n.kind === "ticket" && view.k >= 0.9) || (n.kind === "step" && view.k >= 1.6);
  const labelOf = (n) => (n.id === focus ? clip(n.title, 48) : n.kind === "dispatch" ? clip(n.label, 34)
    : n.kind === "ticket" && view.k >= 2 ? `${n.ident} ${clip(n.title, 28)}` : n.label);

  return (
    <div className="fx-canvas" ref={box}>
      <svg ref={svg} width={size.w} height={size.h} onPointerDown={(e) => down(e, null)} onPointerMove={move}
           onPointerUp={up} onPointerCancel={up} role="img" aria-label="Factory graph">
        <defs>
          <marker id="fx-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" className="fx-arrowhead" />
          </marker>
        </defs>
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {graph.edges.map((e, i) => {
            const p = pos.get(e.s), q = pos.get(e.t);
            if (!p || !q) return null;
            const dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
            const rq = (R[graph.nodes.find((n) => n.id === e.t)?.kind] || 6) + 3;
            const dim = near && !(near.has(e.s) && near.has(e.t));
            return <line key={i} x1={p.x} y1={p.y} x2={q.x - (dx / d) * rq} y2={q.y - (dy / d) * rq}
                         className={`fx-edge ${e.kind}${dim ? " dim" : ""}${near && !dim ? " lit" : ""}`}
                         markerEnd={e.kind === "after" || e.kind === "about" ? "url(#fx-arrow)" : undefined} />;
          })}
          {graph.nodes.map((n) => {
            const p = pos.get(n.id);
            if (!p) return null;
            const r = R[n.kind];
            const dim = near && !near.has(n.id);
            const cls = `fx-node ${n.kind} t-${n.tone}${n.closed ? " closed" : ""}${n.you ? " you" : ""}${dim ? " dim" : ""}` +
                        `${n.id === selected ? " sel" : ""}${picked.includes(n.ident) && n.kind === "ticket" ? " picked" : ""}`;
            return (
              <g key={n.id} className={cls} transform={`translate(${p.x},${p.y})`}
                 onPointerDown={(e) => down(e, n.id)} onPointerEnter={() => setHover(n.id)} onPointerLeave={() => setHover(null)}>
                {n.you ? <circle className="halo" r={r + 6} /> : null}
                {n.kind === "flag" ? <path className="dot" d={`M0,${-r - 2} L${r + 2},0 L0,${r + 2} L${-r - 2},0 z`} />
                  : <circle className="dot" r={r} />}
                {showLabel(n) ? <text y={r + 12} className={`lbl ${n.kind}`} style={{ fontSize: `${LBL[n.kind] / Math.sqrt(view.k)}px` }}>{labelOf(n)}</text> : null}
                <title>{n.title}</title>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="fx-legend">
        <span><i className="t-amber" />needs you</span><span><i className="t-green" />ready / done</span>
        <span><i className="t-blue" />in progress</span><span><i className="t-gray" />idle / not ours</span>
        <span><i className="t-red diamond" />held write</span><span className="arrow">→ must happen first</span>
        <span className="muted">drag · scroll to zoom · shift-click ready tickets to group them</span>
      </div>
    </div>
  );
}

// ---- side panel ----------------------------------------------------------------------------
// Where the thing is in its lifecycle: done stages muted, the current one lit, the rest ahead.
const TICKET_FLOW = ["Checking", "Verified", "In a dispatch", "Built", "Written to Linear"];
const DISPATCH_FLOW = ["Draft", "Approved", "Being worked on", "Finished", "Written to Linear", "Archived"];
const DISPATCH_AT = { draft: 0, staged: 1, executing: 2, done: 3, reconciled: 4, archived: 5 };
function Flow({ steps, at, stuck }) {
  return (
    <ol className="fx-flow">
      {steps.map((s, i) => (
        <li key={s} className={i < at ? "past" : i === at ? `now${stuck ? " stuck" : ""}` : "next"}>
          {i === at && stuck ? stuck : s}
        </li>
      ))}
    </ol>
  );
}
const Leads = ({ items }) => (
  <div className="fx-leads">
    <div className="fx-k">What happens next</div>
    <ul>{items.filter(Boolean).map(([act, result], i) => <li key={i}><b>{act}</b><span>→ {result}</span></li>)}</ul>
  </div>
);

function Evidence({ items }) {
  const KIND = { file: "Code", sql: "Data", dagster: "Pipeline run", linear: "Ticket", pr: "Pull request" };
  if (!items || !items.length) return null;
  return (
    <details className="fx-more"><summary>Evidence ({items.length})</summary>
      <ul className="fx-evidence">
        {items.map((e, i) => (
          <li key={i}><b>{KIND[e.type] || e.type}</b>{": "}
            {e.path ? <code>{e.path}</code> : e.url ? <Ext href={e.url}>{e.url}</Ext> : e.ref ? e.ref : e.witness ? <code>{e.witness}</code> : null}
            {e.note ? ` — ${e.note}` : ""}</li>
        ))}
      </ul>
    </details>
  );
}

function NoteBox({ d, nodeId, onDone }) {
  const [text, setText] = useState("");
  const a = useAction(() => { setText(""); onDone("Note added"); });
  const go = () => text.trim() && a.run(`/drafts/${encodeURIComponent(d.run_id)}/notes`, { node: nodeId, body: text.trim() });
  return (
    <div className="fx-note-box">
      <div className="fx-row">
        <Input value={text} maxLength={4000} disabled={a.busy} placeholder={nodeId === "root" ? "Note for the whole dispatch" : `Note on ${nodeId}`}
               onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
        <Button size="sm" outlined disabled={a.busy || !text.trim()} onClick={go}>{a.busy ? "Adding…" : "Add"}</Button>
      </div>
      <div className="fx-hint">Goes to the executor word for word; can't be edited or removed.</div>
      <ActErr err={a.err} />
    </div>
  );
}

const Notes = ({ notes }) => (notes || []).map((n) => (
  <div key={n.id} className="fx-note"><div className="fx-hint">{`${n.author} · ${ago(n.at)}`}</div>{n.body}</div>
));

// The plan as an indented tree; click a line to select that node in the graph.
function PlanTree({ d, onSelect }) {
  const tree = d.tree || [];
  const depth = {};
  tree.forEach((n) => { depth[n.id] = n.parent == null ? 0 : (depth[n.parent] ?? 0) + 1; });
  const idOf = (n) => (n.kind === "dispatch" ? `d:${d.run_id}` : n.kind === "ticket" ? `t:${n.id}` : `s:${d.run_id}:${n.id}`);
  if (tree.length <= 1) return <div className="fx-hint">No plan yet.</div>;
  return (
    <ul className="fx-tree">
      {tree.filter((n) => n.kind !== "dispatch").map((n) => (
        <li key={n.id} style={{ paddingLeft: `${(depth[n.id] - 1) * 14}px` }} onClick={() => onSelect(idOf(n))}>
          <span className="fx-id">{n.id}</span> {n.title}
          {n.notes?.length ? <span className="fx-count">{n.notes.length}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function ReasonDialog({ verb, open, onClose, onSubmit, a }) {
  const [text, setText] = useState("");
  useEffect(() => { if (open) setText(""); }, [open]);
  const go = () => text.trim() && onSubmit(text.trim());
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{verb === "hold" ? "Hold this dispatch?" : "Reject this dispatch?"}</DialogTitle>
          <DialogDescription>{verb === "hold" ? "The automatic start is cancelled; it waits until you approve."
            : "The draft is archived and its tickets go back to Ready."}</DialogDescription>
        </DialogHeader>
        <div className="fx-dialog-body">
          <Input autoFocus value={text} maxLength={4000} placeholder="Reason (required)" disabled={a.busy}
                 onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
          <ActErr err={a.err} />
        </div>
        <DialogFooter>
          <Button size="sm" ghost onClick={onClose} disabled={a.busy}>Cancel</Button>
          <Button size="sm" destructive={verb === "reject"} disabled={a.busy || !text.trim()} onClick={go}>
            {a.busy ? "Working…" : verb === "hold" ? "Hold" : "Reject"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DraftActions({ d, onDone }) {
  const [mode, setMode] = useState(null);
  const a = useAction((r) => {
    const verb = mode;
    setMode(null);
    onDone(r && r.handoff_error ? `Approved, but starting failed: ${r.handoff_error}. The factory will retry.`
      : { approve: "Approved; factory-fleet is starting", hold: "Held", reject: "Rejected" }[verb], !!(r && r.handoff_error));
  });
  const id = encodeURIComponent(d.run_id);
  const steps = (d.tree || []).filter((n) => n.kind === "step").length;
  const n = (d.tickets || []).length;
  return (
    <>
      <div className="fx-row">
        <Button size="sm" onClick={() => { a.clear(); setMode("approve"); }}>Approve & start</Button>
        {d.review === "held" ? null : <Button size="sm" outlined onClick={() => { a.clear(); setMode("hold"); }}>Hold</Button>}
        <Button size="sm" ghost className="text-destructive" onClick={() => { a.clear(); setMode("reject"); }}>Reject</Button>
      </div>
      <Leads items={[
        ["Approve", `plan and notes freeze; factory-fleet builds ${steps ? plural(steps, "step") + " across " : ""}${plural(n, "ticket")}, opens PRs, then results are written to Linear`],
        d.review !== "held" && ["Hold", "no automatic start; it waits for you"],
        ["Reject", "draft archived; its tickets return to Ready"],
        d.review === "in-review" && ["Do nothing", `starts on its own at ${localTime(d.review_until)}`],
      ]} />
      <ConfirmDialog open={mode === "approve"} title="Approve and start?" confirmLabel="Approve & start" loading={a.busy}
        description={`Freezes the plan and your notes, then starts real work on factory-fleet: branches, commits and pull requests for ${plural(n, "ticket")}. Can take a few minutes.` + (a.err ? `\n\n${a.err}` : "")}
        onCancel={() => setMode(null)} onConfirm={() => a.run(`/drafts/${id}/approve`, {})} />
      <ReasonDialog verb={mode} open={mode === "hold" || mode === "reject"} a={a} onClose={() => setMode(null)}
                    onSubmit={(reason) => a.run(`/drafts/${id}/${mode}`, { reason })} />
    </>
  );
}

function FlagResolve({ f, onDone }) {
  const [text, setText] = useState("");
  const a = useAction(() => onDone("Flag resolved"));
  const go = () => text.trim() && a.run(`/flags/${f.id}/resolve`, { resolution: text.trim() });
  return (
    <>
      <div className="fx-row">
        <Input value={text} maxLength={2000} placeholder="What you decided" disabled={a.busy}
               onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
        <Button size="sm" disabled={a.busy || !text.trim()} onClick={go}>{a.busy ? "Resolving…" : "Resolve"}</Button>
      </div>
      <ActErr err={a.err} />
      <Leads items={[["Resolve", "records your decision; nothing is written to Linear"]]} />
    </>
  );
}

function Panel({ n, graph, picked, onPick, onSelect, onDone }) {
  if (!n) return null;
  const draft = (n.d || (n.run && graph.nodes.find((x) => x.id === `d:${n.run}`)?.d));
  const inDraft = draft && draft.state === "draft";
  const header = (kind, badge, title, meta) => (
    <CardHeader>
      <div className="fx-row between"><span className="fx-k">{kind}</span>{badge}</div>
      <div className="fx-title">{title}</div>
      {meta ? <div className="fx-hint">{meta}</div> : null}
    </CardHeader>
  );

  if (n.kind === "dispatch") {
    const d = n.d, s = n.st;
    if (!d) return (  // archived: only the summary survives
      <Card>{header("Dispatch", <Tone tone="green">Archived</Tone>, n.label, `${n.a.run_id} · ${s.why}`)}
        <CardContent><Flow steps={DISPATCH_FLOW} at={5} /></CardContent></Card>);
    const repos = (() => { try { return JSON.parse(d.repos_json || "[]").map((r) => r.repo.split("/").pop()); } catch { return []; } })();
    return (
      <Card>
        {header("Dispatch", <Tone tone={s.tone}>{s.label}</Tone>, n.title,
          [d.run_id, ...repos, d.auto ? "drafted by the factory" : d.drafted_by, `created ${ago(d.created_at)}`].filter(Boolean).join(" · "))}
        <CardContent className="fx-stack">
          <Flow steps={DISPATCH_FLOW} at={DISPATCH_AT[d.state] ?? 0} stuck={d.state === "draft" ? s.label : null} />
          <div>{s.why}</div>
          {d.emergency ? <Tone tone="amber">Emergency: no review window</Tone> : null}
          {d.hash_ok === false ? <div className="fx-err">The dispatch file was modified after approval.</div> : null}
          {d.state === "draft" && d.review !== "planning" ? <DraftActions d={d} onDone={onDone} /> : null}
          <div className="fx-k">Tickets</div>
          <ul className="fx-cards">
            {(d.tickets || []).map((c) => (
              <li key={c.identifier} onClick={() => onSelect(`t:${c.identifier}`)}>
                <Tone tone={CARD_TONE[c.card_status] || "gray"}>{CARD[c.card_status] || c.card_status}</Tone>
                <span className="fx-id">{c.identifier}</span>
                {c.pr_url ? <Ext href={c.pr_url}>{c.pr_url.replace("https://github.com/", "")}</Ext> : null}
              </li>
            ))}
          </ul>
          <div className="fx-k">Plan</div>
          <PlanTree d={d} onSelect={onSelect} />
          {inDraft ? <><Notes notes={(d.tree || []).find((x) => x.id === "root")?.notes} /><NoteBox d={d} nodeId="root" onDone={onDone} /></> : null}
          <details className="fx-more"><summary>History</summary>
            <ul className="fx-evidence">{(d.transitions || []).map((x, i) => <li key={i}>{`${x.to_state} — by ${x.actor}, ${ago(x.at)}`}</li>)}</ul>
          </details>
        </CardContent>
      </Card>
    );
  }

  if (n.kind === "step") {
    const node = n.node;
    return (
      <Card>
        {header("Plan step", <span className="fx-id">{node.id}</span>, node.title,
          node.depends_on?.length ? `after ${node.depends_on.join(", ")}` : "no prerequisites")}
        <CardContent className="fx-stack">
          <div className="fx-pre">{node.detail}</div>
          <Notes notes={node.notes} />
          {inDraft ? <NoteBox d={draft} nodeId={node.id} onDone={onDone} /> : null}
          <Button size="sm" ghost onClick={() => onSelect(`d:${n.d.run_id}`)}>← Dispatch</Button>
        </CardContent>
      </Card>
    );
  }

  if (n.kind === "flag") {
    const f = n.f;
    return (
      <Card>
        {header("Held write", <Tone tone="red">{FLAG_LABEL(f)}</Tone>, f.identifier ? `${f.identifier}: ${f.title || ""}` : f.kind,
          `Flag ${f.id}` + (f.run_id ? ` · from ${f.run_id}` : ""))}
        <CardContent className="fx-stack">
          <div>{f.reason}</div>
          {f.url ? <Ext href={f.url}>Open in Linear</Ext> : null}
          <FlagResolve f={f} onDone={onDone} />
        </CardContent>
      </Card>
    );
  }

  // ticket
  const t = n.t, st = n.st;
  const at = n.closed ? 4 : n.card ? (n.card.card_status === "done" ? 3 : 2) : st?.group === "ready" ? 1 : 0;
  const stuck = st?.group === "you" ? st.label : st?.group === "nothing" || st?.group === "ignored" ? st.label : null;
  const meta = t ? `${t.domain} · ${t.linear_state} · ${t.assignee ? t.assignee.split("@")[0] : "unassigned"}` : n.w ? `now ${n.w.linear_state} in Linear` : null;
  const badge = n.card ? <Tone tone={n.tone}>{CARD[n.card.card_status] || n.card.card_status}</Tone>
    : st ? <Tone tone={st.tone}>{st.label}</Tone> : n.closed ? <Tone tone="green">Closed</Tone> : <Tone tone="gray">Not in view</Tone>;
  const isPicked = picked.includes(n.ident);
  const treeNode = n.node;
  return (
    <Card>
      {header(n.ident, badge, n.title, meta)}
      <CardContent className="fx-stack">
        <Flow steps={TICKET_FLOW} at={at} stuck={stuck} />
        {st?.why ? <div>{st.why}</div> : null}
        {treeNode?.detail ? <div className="fx-pre">{treeNode.detail}</div> : null}
        <Notes notes={treeNode?.notes} />
        {inDraft && treeNode ? <NoteBox d={draft} nodeId={n.ident} onDone={onDone} /> : null}
        {n.ready ? <>
          <div className="fx-row"><Button size="sm" outlined={isPicked} onClick={() => onPick(n.id)}>{isPicked ? "Remove from selection" : "Add to a new dispatch"}</Button></div>
          <Leads items={[["Group & draft", "a planner writes one plan for the group; you review it before anything runs"]]} />
        </> : st?.group === "you" ? <Leads items={[["Answer in Linear", "it's verified again on the next sync, within ~20 minutes"]]} />
          : st?.group === "progress" ? <Leads items={[["Wait", "verification runs every 20 minutes"]]} /> : null}
        {n.run ? <Button size="sm" ghost onClick={() => onSelect(`d:${n.run}`)}>← Dispatch</Button> : null}
        <div className="fx-row">
          {t?.url || n.w?.url ? <Ext href={t?.url || n.w?.url}>Open in Linear</Ext> : null}
          {t?.verdict ? <span className="fx-hint">{`Verified ${ago(t.verdict.created_at)} by ${t.verdict.created_by}` + (t.repo ? ` against ${t.repo}` : "")}</span> : null}
        </div>
        <Evidence items={t?.verdict?.evidence} />
      </CardContent>
    </Card>
  );
}

// With nothing selected the panel lists what needs you, in order.
function Queue({ graph, onSelect }) {
  const items = graph.nodes.filter((n) => n.you);
  return (
    <Card>
      <CardHeader><div className="fx-title">{items.length ? "Needs you" : "Nothing needs you"}</div></CardHeader>
      <CardContent className="fx-stack">
        {items.length ? items.map((n) => (
          <button key={n.id} className="fx-queue" onClick={() => onSelect(n.id)}>
            <Tone tone={n.tone}>{n.kind === "dispatch" ? n.st.label : n.kind === "flag" ? "Held write" : n.st.label}</Tone>
            <span>{clip(n.title, 90)}</span>
          </button>
        )) : <div className="fx-hint">Select any node to see where it is and what each action leads to.</div>}
      </CardContent>
    </Card>
  );
}

// ---- health + throughput -------------------------------------------------------------------
const JOB_NAME = { "factory-ingest": "Linear sync", "factory-prune": "Verification", "factory-plan": "Planning",
                   "factory-propose": "Proposals", "factory-reconcile": "Write-back", "factory-backup": "Backup" };
function Health({ jobs }) {
  const name = (j) => JOB_NAME[j.name] || j.name;
  const bad = jobs.filter((j) => j.last_status && !["ok", "success", "succeeded"].includes(j.last_status));
  return (
    <span className={`fx-health ${bad.length ? "bad" : "ok"}`}
          title={jobs.map((j) => `${name(j)}: ${j.last_status || "not run"}, ${epochAgo(j.last_run_at)}`).join("\n")}>
      <i />{bad.length ? bad.map((j) => `${name(j)} failed: ${j.last_error || j.last_status}`).join(" · ") : `${jobs.length} jobs healthy`}
    </span>
  );
}

const pct = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
const hours = (v) => (v == null ? "—" : v < 48 ? `${v.toFixed(1)} h` : `${(v / 24).toFixed(1)} days`);
const Stat = ({ k, v }) => <Card><CardContent className="fx-stat"><div className="v">{v}</div><div className="fx-hint">{k}</div></CardContent></Card>;
const Table = ({ head, rows }) => (
  <table className="fx-tbl">
    <thead><tr>{head.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
    <tbody>{rows.map((r, j) => <tr key={j}>{r.map((c, i) => <td key={i}>{c}</td>)}</tr>)}</tbody>
  </table>
);

function Throughput() {
  const [m, setM] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { SDK.fetchJSON(`${API}/metrics?days=28`).then(setM, (e) => setErr(errText(e))); }, []);
  if (!m) return err ? <div className="fx-err">Throughput unavailable: {err}</div> : <div className="fx-hint">Loading…</div>;
  return (
    <div className="fx-stack">
      <div className="fx-hint">Last 28 days.</div>
      <div className="fx-stats">
        <Stat k="tickets done" v={m.tickets.done} />
        <Stat k="tickets blocked" v={m.tickets.blocked} />
        <Stat k="block rate" v={pct(m.block_rate)} />
        <Stat k="approve → done, median" v={hours(m.hours.stage_to_done_p50)} />
        <Stat k="approve → done, slowest" v={hours(m.hours.stage_to_done_max)} />
        <Stat k="done → archived, median" v={hours(m.hours.done_to_archived_p50)} />
      </div>
      <div className="fx-hint">{`Dispatches: ${m.dispatches.staged} approved, ${m.dispatches.archived} archived · Linear writes: ${m.writeback.confirmed} confirmed, ${m.writeback.failed} failed, ${m.writeback.flagged} flagged`}</div>
      {m.per_week.length ? <Table head={["Week of", "Approved", "Done", "Blocked"]} rows={m.per_week.map((w) => [w.week, w.staged, w.done, w.blocked])} /> : null}
      {m.per_repo.length ? <Table head={["Repo", "Done", "Blocked"]} rows={m.per_repo.map((r) => [r.repo, r.done, r.blocked])} /> : null}
    </div>
  );
}

// ---- page --------------------------------------------------------------------------------
const TOGGLES = [["steps", "Plan steps"], ["checking", "Being checked"], ["closed", "Closed"], ["handled", "Not for the factory"]];

function FactoryPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [selected, setSelected] = useState(null);
  const [picked, setPicked] = useState([]);
  const [show, setShow] = useState({ steps: true, checking: true, closed: false, handled: false });
  const [fitKey, setFitKey] = useState(0);
  const [toast, setToast] = useState(null);
  const load = useCallback(() => {
    SDK.fetchJSON(`${API}/overview`)
      .then((d) => { setData(d); setError(null); setLoadedAt(new Date().toISOString()); })
      .catch((e) => setError(errText(e)));
  }, []);
  useEffect(() => { load(); const t = setInterval(load, REFRESH_MS); return () => clearInterval(t); }, [load]);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 3500); return () => clearTimeout(t); }, [toast]);
  useEffect(() => {
    const esc = (e) => { if (e.key === "Escape" && !document.querySelector("[role=dialog]")) setSelected(null); };
    window.addEventListener("keydown", esc, true);  // capture: runs before a dialog closes itself on Escape
    return () => window.removeEventListener("keydown", esc, true);
  }, []);

  const titles = useMemo(() => {
    if (!data) return {};
    const m = {};
    data.tickets.forEach((t) => { m[t.identifier] = t.title; });
    (data.candidates?.candidates || []).forEach((c) => { m[c.identifier] ||= c.title; });
    (data.status.written_back || []).forEach((w) => { m[w.identifier] ||= w.title; });
    return m;
  }, [data]);
  const graph = useMemo(() => (data ? buildGraph(data, show, titles) : { nodes: [], edges: [] }), [data, show, titles]);
  useEffect(() => { setFitKey((k) => k + 1); }, [show]);

  if (!data) return <div className="fx">{error ? <div className="fx-err">{error}</div> : <div className="fx-hint">Loading…</div>}</div>;

  const max = data.candidates?.max_tickets || 0;
  const stageable = new Set((data.candidates?.candidates || []).map((c) => c.identifier));
  const sel = picked.filter((i) => stageable.has(i));
  const pick = (id) => {
    const n = graph.nodes.find((x) => x.id === id);
    if (!n || !n.ready || !stageable.has(n.ident)) return;
    setPicked((prev) => {
      const cur = prev.filter((i) => stageable.has(i));
      return cur.includes(n.ident) ? cur.filter((i) => i !== n.ident) : cur.length < max ? [...cur, n.ident] : cur;
    });
  };
  const done = (msg, bad) => { setToast({ type: bad ? "error" : "success", message: msg }); load(); };
  const needYou = graph.nodes.filter((n) => n.you).length;
  const node = graph.nodes.find((n) => n.id === selected);

  return (
    <div className="fx">
      <Toast toast={toast} />
      <Tabs defaultValue="graph">
        {(tab, setTab) => (
          <>
            <div className="fx-top">
              <div>
                <div className={`fx-summary${needYou ? " you" : ""}`}>{needYou ? `${plural(needYou, "thing")} need${needYou === 1 ? "s" : ""} you` : "Nothing needs you"}</div>
                <div className="fx-hint">{`${data.tickets.length} tickets in your domains · ${data.status.ignored_other_leads} in other leads' domains ignored`}</div>
              </div>
              <div className="fx-row">
                <Health jobs={data.jobs} />
                <Button size="sm" ghost onClick={load} title="Refreshes every 30 seconds">{`Refreshed ${loadedAt ? ago(loadedAt) : ""}`}</Button>
              </div>
            </div>
            {error ? <div className="fx-err">Last refresh failed: {error}</div> : null}
            <TabsList>
              <TabsTrigger active={tab === "graph"} value="graph" onClick={() => setTab("graph")}>Graph</TabsTrigger>
              <TabsTrigger active={tab === "stats"} value="stats" onClick={() => setTab("stats")}>Throughput</TabsTrigger>
            </TabsList>
            {tab === "stats" ? <Throughput /> : (
              <>
                <div className="fx-toolbar">
                  {TOGGLES.map(([k, label]) => (
                    <Button key={k} size="sm" outlined={show[k]} ghost={!show[k]} aria-pressed={show[k]}
                            onClick={() => setShow({ ...show, [k]: !show[k] })}>{label}</Button>
                  ))}
                  <Button size="sm" ghost onClick={() => setFitKey((k) => k + 1)}>Fit</Button>
                  <span className="grow" />
                  {sel.length ? <DraftBar sel={sel} max={max} clear={() => setPicked([])} onDone={done} /> : null}
                </div>
                <div className="fx-main">
                  <GraphView graph={graph} selected={selected} picked={sel} onSelect={setSelected} onPick={pick} fitKey={fitKey} />
                  <aside className="fx-side">
                    {node ? <Panel n={node} graph={graph} picked={sel} onPick={pick} onSelect={setSelected} onDone={done} />
                      : <Queue graph={graph} onSelect={setSelected} />}
                  </aside>
                </div>
              </>
            )}
          </>
        )}
      </Tabs>
    </div>
  );
}

function DraftBar({ sel, max, clear, onDone }) {
  const a = useAction(() => { clear(); onDone(`Drafted a dispatch of ${plural(sel.length, "ticket")}; a planner is writing the plan`); });
  return (
    <div className="fx-row">
      <span className="fx-hint">{`${sel.length} of max ${max}: ${sel.join(", ")}`}</span>
      <ActErr err={a.err} />
      <Button size="sm" ghost disabled={a.busy} onClick={clear}>Clear</Button>
      <Button size="sm" disabled={a.busy} onClick={() => a.run("/stage", { identifiers: sel })}>{a.busy ? "Drafting…" : "Draft dispatch"}</Button>
    </div>
  );
}

window.__HERMES_PLUGINS__.register("factory", FactoryPage);

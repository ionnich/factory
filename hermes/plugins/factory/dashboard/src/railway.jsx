// Desktop map of a plan's choices, railway style: NOW → question → its options on parallel tracks → rejoin → next question
// … → RESULT; a question that only matters under one answer hangs under that branch. The ★ path is solid, the picked
// path glows, the rest fade. One selection with the outline: click a branch = flip that switch. Drawn by the Rail
// (index.jsx), one node per row; lanes are assigned here (each branch its own track).
import { LANE_W, RAIL_X0, Rail, clip, sortOptions } from "./index.jsx";
import { activeQs, starPick } from "./plan.jsx";

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useMemo } = SDK.hooks;
const h = React.createElement;
const Fragment = React.Fragment;
const ROW = 34;

// The nodes on a pick's path.
export const pathOf = (qs, pick) => new Set(["now", "result",
  ...activeQs(qs, pick).flatMap((q) => [`q:${q.id}`, `o:${q.id}:${pick[q.id]}`, `j:${q.id}`])]);

export function railway(d, qs) {
  const nodes = [], edges = [], lane = {}, track = new Map();
  const add = (n, l) => { lane[n.id] = l; nodes.push(n); return n.id; };
  const link = (s, t) => edges.push({ s, t });
  const kids = (q, o) => qs.filter((x) => x.depends_on?.question === q.key && x.depends_on?.option === o.id);
  const width = (list) => Math.max(1, ...list.map((q) => sortOptions(q).reduce((w, o) => w + width(kids(q, o)), 0)));
  const chain = (list, from, L) => list.reduce((prev, q) => {
    const qn = add({ id: `q:${q.id}`, kind: "gate", decision: q }, L);
    link(prev, qn);
    let l = L;  // ★ first, on the line it came in on; the others to its right, each as wide as what hangs under it
    const ends = sortOptions(q).map((o) => {
      const on = add({ id: `o:${q.id}:${o.id}`, kind: "step", q, o }, l);
      link(qn, on);
      const k = kids(q, o), end = k.length ? chain(k, on, l) : on;
      l += width(k);
      return end;
    });
    const j = add({ id: `j:${q.id}`, kind: "step", join: true }, L);
    ends.forEach((e) => link(e, j));
    return j;
  }, from);
  const keys = new Set(qs.map((q) => q.key));
  const now = add({ id: "now", kind: "root", d }, 0);
  const last = chain(qs.filter((q) => !q.depends_on || !keys.has(q.depends_on.question)), now, 0);
  link(last, add({ id: "result", kind: "outcome", writes: [] }, 0));
  const row = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const star = pathOf(qs, starPick(qs));
  edges.forEach((e) => {
    e.kind = star.has(e.s) && star.has(e.t) ? "star" : "alt";
    if (lane[e.s] !== lane[e.t] && row[e.t] - row[e.s] > 1) track.set(e, Math.max(lane[e.s], lane[e.t]));  // run down the branch
  });
  return { nodes, edges, lane, track, count: Math.max(...Object.values(lane)) + 1 };
}

export function Railway({ d, qs, pick, onPick, free }) {
  const map = useMemo(() => railway(d, qs), [d, qs]);
  const lit = pathOf(qs, pick);
  const ys = map.nodes.map((_, i) => i * ROW + ROW / 2);
  const width = RAIL_X0 * 2 + (map.count - 1) * LANE_W + 4;
  const root = (d.tree || []).find((n) => n.id === "root");
  const label = (n) => {
    if (n.id === "now") return <><b>Now</b><span className="fx-hint">{clip(root?.title || d.run_id, 48)}</span></>;
    if (n.id === "result") return <b>Result</b>;
    if (n.kind === "gate") return <><span className="fx-id">#{n.decision.id}</span><span className="fx-rw-q">{clip(n.decision.question, 64)}</span></>;
    if (n.join) return null;
    return (
      <button className="fx-rw-o" disabled={!free(n.q)} onClick={() => onPick(n.q.id, n.o.id)} aria-pressed={pick[n.q.id] === n.o.id}>
        {n.o.id === n.q.recommended ? <span className="star">★</span> : null}{n.o.label}
      </button>
    );
  };
  return (
    <div className="fx-flow fx-railway">
      <Rail flow={map} layout={map} ys={ys} lit={lit} width={width} />
      {map.nodes.map((n) => (
        <div key={n.id} className={`fx-rw-row${lit.has(n.id) ? " lit" : ""}`} style={{ height: `${ROW}px`, paddingLeft: `${width}px` }}>{label(n)}</div>
      ))}
    </div>
  );
}

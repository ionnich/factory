// Learn tab: what the factory learned (overview `learnings`: active and proposed, from `factory propose`), by kind
// with how often agents cited each (`L<id>`), next to what a verdict and a plan cost (metrics.cost.per_unit), so
// the two can be weighed. No savings figure: the factory can't observe tokens not spent. Proposed ones are
// answered in the deck (decision kind 'learning'); here they are only listed.
import { REL } from "./jev.jsx";

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect } = SDK.hooks;
const h = React.createElement;

const KINDS = [["house_rule", "House rules"], ["pitfall", "Pitfalls"], ["codemap", "Code map"]];
const usd = (v) => (v == null ? "—" : `$${v.toFixed(2)}`);
const times = (n) => (n === 1 ? "used once" : `used ${n}×`);

const Line = ({ l }) => {
  const j = l.jev;
  const rel = j?.relation;
  return (
    <div className="fx-note">
      <span className="fx-id">L{l.id}</span> {l.body}
      <div className="fx-hint">{l.scope.split("/").pop()} · {l.status === "proposed" ? "awaiting you · proposed" : `${times(l.uses)} · observed`} from {l.source}</div>
      {rel && rel.learning_id != null ? (
        <div className={`fx-hint${rel.kind === "conflicts" ? " fx-err" : ""}`}>{REL[rel.kind] || "related to"} <span className="fx-id">L{rel.learning_id}</span>{rel.body ? ` · ${rel.body}` : ""}</div>
      ) : null}
      {j && j.status === "unavailable" ? <div className="fx-hint">Jev unavailable: {j.error || "no comparison run"}</div> : null}
    </div>
  );
};

// items: [{id, kind: codemap|pitfall|house_rule, scope, body, anchors: [path], source, status: active|proposed,
//          created_at, uses}]
export function Learnings({ items }) {
  const [unit, setUnit] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    SDK.fetchJSON("/api/plugins/factory/metrics?days=28").then((m) => setUnit(m.cost.per_unit), (e) => setErr(String(e?.message || e)));
  }, []);
  const active = items.filter((l) => l.status === "active");
  const proposed = items.filter((l) => l.status === "proposed");
  const uses = active.reduce((n, l) => n + l.uses, 0);
  return (
    <div className="fx-stack-v">
      <div className="fx-stats">
        <div className="fx-stat"><b>{uses}</b><span>times agents cited one</span></div>
        <div className="fx-stat"><b>{usd(unit?.verdict)}</b><span>per verdict</span></div>
        <div className="fx-stat"><b>{usd(unit?.plan)}</b><span>per plan</span></div>
      </div>
      <div className="fx-hint">{err ? `Cost unavailable: ${err}` : "Cost: last 28 days of agent spend. Agents are handed the learnings for their repo and paths and cite L<id> when one saved them work."}</div>
      {proposed.length ? (
        <details className="fx-fold" open>
          <summary>Awaiting you <span className="fx-count">{proposed.length}</span> <span className="fx-hint">keep or drop them in Needs you</span></summary>
          <div className="fx-list">{proposed.map((l) => <Line key={l.id} l={l} />)}</div>
        </details>
      ) : null}
      {KINDS.map(([kind, label]) => {
        const ls = active.filter((l) => l.kind === kind);
        return ls.length ? (
          <details key={kind} className="fx-fold" open={kind !== "codemap"}>
            <summary>{label} <span className="fx-count">{ls.length}</span></summary>
            <div className="fx-list">{ls.map((l) => <Line key={l.id} l={l} />)}</div>
          </details>
        ) : null;
      })}
      {!items.length ? <div className="fx-empty">Nothing learned yet. The code map fills in as tickets are verified and planned.</div> : null}
    </div>
  );
}

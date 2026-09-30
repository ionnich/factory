// Jev: the model's read of one decision — compact guidance before the options and the long "why?" text. It never
// answers, never voids, never preselects; the server keeps review authority. Consumes the decision's top-level
// `jev` ({status, category, rule, model, confidence, focus, error, relation, group}) — core exposes it top-level
// from the jev_advice table (never decision.detail_json);
// absent or disabled renders nothing, so no guidance is ever fabricated. Focus highlights are applied by
// jevFocus() in the option/switch renderers, not here.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const h = React.createElement;

const CATEGORY = {
  investigate: ["amber", "Investigation requested", "Jev sees missing factual investigation before this can be answered."],
  policy: ["blue", "Policy", "Jev matches an approved house rule."],
  human: ["amber", "Human tradeoff", "Jev sees a genuine preference or permission call."],
  unclear: ["gray", "Uncertain", "Jev could not place this as investigation, policy or preference."],
};

export const REL = { duplicate: "same as", supports: "backs", conflicts: "conflicts with" };

export const jevFocus = (d) => (d.jev && d.jev.status === "ok" ? d.jev.focus : null);

const pct = (c) => (c == null ? null : `${Math.round(c * 100)}%`);

export function Jev({ d }) {
  const j = d.jev;
  if (!j || j.status === "disabled") return null;
  if (j.status !== "ok") return (
    <div className="fx-jev fx-jev-off" role="status">
      <span className="fx-jev-badge t-gray">Jev unavailable</span>
      {j.error ? <span className="fx-jev-line">{j.error}</span> : null}
    </div>
  );
  const [tone, label, hint] = CATEGORY[j.category] || CATEGORY.unclear;
  const ruleOpt = j.rule ? (d.options || []).find((o) => o.id === j.rule.option_id) : null;
  const head = j.rule && j.category === "policy"
    ? (<span>matches approved rule <span className="fx-id">L{j.rule.id}</span>: {j.rule.body}{ruleOpt ? <b>{` ${ruleOpt.label}`}</b> : null}</span>)
    : <span className="fx-hint">{hint}</span>;
  return (
    <div className="fx-jev">
      <span className={`fx-jev-badge t-${tone}`}>{label}</span>
      <span className="fx-jev-line">{head}</span>
      <details className="fx-jev-det">
        <summary>how Jev read this</summary>
        <div className="fx-jev-meta">
          {j.model ? <span className="fx-hint">model {j.model}</span> : null}
          {pct(j.confidence) ? <span className="fx-hint">confidence {pct(j.confidence)} · advisory, not approval</span> : null}
          {j.rule ? <span className="fx-hint">evidence: rule <span className="fx-id">L{j.rule.id}</span> {j.rule.body}</span> : null}
          {j.focus && j.focus !== "none" ? <span className="fx-hint">focus: {j.focus}</span> : null}
          {j.relation && j.relation.learning_id != null ? (
            <span className="fx-hint">related to <span className="fx-id">L{j.relation.learning_id}</span> · {REL[j.relation.kind] || "related"}</span>) : null}
          {j.error ? <span className="fx-err">{j.error}</span> : null}
        </div>
      </details>
    </div>
  );
}

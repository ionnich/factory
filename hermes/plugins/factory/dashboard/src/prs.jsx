// Read-only GitHub review inbox. The hook is intentionally independent of Factory's SSE refresh: GitHub is fetched
// once when FactoryPage mounts and only again when the user presses Refresh.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useCallback, useRef } = SDK.hooks;
const { Button } = SDK.components;
const h = React.createElement;
const API = "/api/plugins/factory";

const errText = (e) => String(e && e.message ? e.message : e);
const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "unknown");
const stop = (e) => e.stopPropagation();
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function usePRReviews() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const mounted = useRef(true);
  const refresh = useCallback(() => {
    const id = ++request.current;
    setLoading(true);
    setError(null);
    return SDK.fetchJSON(`${API}/pr-reviews`).then(
      (next) => { if (mounted.current && id === request.current) setData((current) => next.error && current ? {
        ...next, items: current.items, count: null, fetched_at: current.fetched_at, stale: true,
        warnings: [...(next.warnings || []), "Showing the last successfully loaded review list."],
      } : next); },
      (err) => { if (mounted.current && id === request.current) setError(errText(err)); },
    ).finally(() => { if (mounted.current && id === request.current) setLoading(false); });
  }, []);
  useEffect(() => {
    mounted.current = true;
    refresh();
    return () => { mounted.current = false; request.current += 1; };
  }, [refresh]);
  return { data, error, loading, refresh };
}

function Checks({ state }) {
  const label = { passed: "Checks passed", failed: "Checks failed", pending: "Checks pending", unknown: "Checks unknown" }[state]
    || "Checks unknown";
  return <span className="fx-pr-checks" data-state={state || "unknown"}>{label}</span>;
}

function ReviewRow({ item, onNavigate }) {
  const dispatch = item.dispatch;
  return (
    <div className="fx-trow fx-pr-row">
      <div className="fx-grow">
        <div className="fx-row-title fx-pr-title">{item.title}</div>
        <div className="fx-row fx-row-meta">
          <span className="fx-id">{item.repository}#{item.number}</span>
          <span>{item.author ? `by ${item.author}` : "author unknown"}</span>
          <span className="fx-grow" />
          <span>updated {ago(item.updated_at)}</span>
        </div>
        <div className="fx-row fx-row-status">
          <Checks state={item.checks} />
          <span className="fx-hint">{item.request_context}</span>
        </div>
        <div className="fx-row fx-pr-actions">
          <a className="fx-link-btn" href={item.url} target="_blank" rel="noreferrer" onClick={stop}>Open GitHub</a>
          {dispatch ? <button className="fx-link-btn" onClick={() => onNavigate({ stage: dispatch.phase, run: dispatch.run_id })}>
            Open dispatch {dispatch.run_id}
          </button> : null}
        </div>
      </div>
    </div>
  );
}

export function PRReviews({ state, onNavigate, onBack }) {
  const { data, error, loading, refresh } = state;
  const items = data?.items || [];
  const count = data?.count;
  const incomplete = data?.partial || data?.truncated;
  return (
    <section className="fx-pr-reviews" aria-labelledby="fx-pr-heading">
      <div className="fx-row between fx-pr-heading">
        <div>
          <div className="fx-k">Needs you</div>
          <h2 className="fx-title" id="fx-pr-heading">Pull request reviews</h2>
          <div className="fx-hint">
            {count == null ? (items.length ? `${plural(items.length, "review")} shown · total unknown` : "Count unknown")
              : `${plural(count, "review")}${incomplete ? " shown" : ""}`}
            {data?.fetched_at ? ` · checked ${ago(data.fetched_at)}` : ""}
          </div>
        </div>
        <div className="fx-row">
          <Button size="sm" ghost onClick={onBack}>Back</Button>
          <Button size="sm" onClick={refresh} disabled={loading}>{loading ? "Refreshing…" : "Refresh"}</Button>
        </div>
      </div>
      {error ? <div className="fx-err" role="alert">Review inbox did not load: {error}</div> : null}
      {data?.error ? <div className="fx-err" role="alert">{data.error}</div> : null}
      {(data?.warnings || []).length ? (
        <ul className="fx-pr-warnings" role="status">{data.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>
      ) : null}
      {!data && loading ? <div className="fx-hint">Loading review requests…</div>
        : !items.length && !data?.error && !error ? <div className="fx-empty">{incomplete ? "No matching reviews in the available results. The inbox is incomplete." : "No open, non-draft pull requests currently request your review."}</div>
        : <div className="fx-list fx-pr-list">{items.map((item) => <ReviewRow key={item.url} item={item} onNavigate={onNavigate} />)}</div>}
    </section>
  );
}

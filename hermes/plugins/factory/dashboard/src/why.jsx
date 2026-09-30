// "why?" on a decision: ask the planner, see its answer inline. Asks come from overview.asks
// ({decision id: [{id, question, answer, status: pending|answered|failed, error, asked_at, answered_at}]}) via
// WhyContext, with the open review decision per run (a planned draft still in review can be replanned with an
// answer); the answer lands in factory.db, /stream fires and the tab reloads, so there is no polling here.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useContext } = SDK.hooks;
const { Button, Input } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;
const API = "/api/plugins/factory";
const post = (path, body) => SDK.fetchJSON(API + path,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const errText = (e) => String(e && e.message ? e.message : e);

export const WhyContext = React.createContext({ asks: {}, reviews: {} });

function Thinking({ since }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  const s = Math.max(0, Math.round((Date.now() - Date.parse(since)) / 1000));
  return <div className="fx-hint">planner is thinking… {Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")}</div>;
}

export function Why({ d }) {
  const { asks, reviews } = useContext(WhyContext);
  const thread = asks[d.id] || [];
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const pending = thread.some((a) => a.status === "pending");
  const review = reviews[d.run_id];
  const canReplan = !!review;  // an open review = a planned draft, not yet approved or rejected
  const send = (q, after) => {
    setBusy(true); setErr(null);
    return post(q.path, q.body).then(() => { setBusy(false); after && after(); }, (e) => { setBusy(false); setErr(errText(e)); });
  };
  const ask = (q) => q.trim() && send({ path: `/decisions/${d.id}/asks`, body: { text: q.trim() } }, () => setText(""));
  if (!open && !thread.length) return <button className="fx-link-btn" onClick={(e) => { e.stopPropagation(); setOpen(true); }}>why?</button>;
  return (
    <div className="fx-stack-v" onClick={(e) => e.stopPropagation()}>
      {thread.map((a, i) => (
        <div key={a.id} className="fx-note">
          <div><b>{a.question}</b></div>
          {a.status === "pending" ? <Thinking since={a.asked_at} />
            : a.status === "failed" ? <div className="fx-err">{a.error}{" "}
              {!pending ? <button className="fx-link-btn" disabled={busy} onClick={() => ask(a.question)}>retry</button> : null}</div>
            : <div className="fx-pre">{a.answer}</div>}
          {a.status === "answered" && i === thread.length - 1 && canReplan ? (
            <button className="fx-link-btn" disabled={busy}
                    onClick={() => send({ path: `/drafts/${d.run_id}/replan`,
                                          body: { reason: `${a.question}\n${a.answer}`.slice(0, 3900) } })}>
              replan with this</button>) : null}
        </div>
      ))}
      {!pending ? (
        <div className="fx-row">
          <Input value={text} maxLength={2000} disabled={busy} placeholder={thread.length ? "Follow-up" : "Why…?"}
                 onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") ask(text); }} />
          <Button size="sm" disabled={busy || !text.trim()} onClick={() => ask(text)}>{busy ? "…" : "Ask"}</Button>
        </div>
      ) : null}
      {err ? <div className="fx-err">{err}</div> : null}
    </div>
  );
}

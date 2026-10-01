// Strategy: a supporting workspace (beside the lifecycle stages, at ?stage=strategy) that turns source tickets into
// approved, immutable, versioned work briefs (intent). Factory keeps verification, implementation planning and
// execution; Strategy keeps source grooming and human-published intent, so a dispatch never reconstructs intent by
// re-reading a Linear narrative.
//
//   GET /strategy is a pure cached read: brief summaries (no body), the source list, execution policy, and active
//   dispatches. Selecting a brief GETs /strategy/{id} for its full body + captured sources + the compiled Markdown
//   render. Grooming runs real DeepSeek through the CLI (long), so it shows busy and any error truthfully — never a
//   fake placeholder. Editing persists a new draft revision (immutable versions; an amendment needs a reason).
//   Publishing (approve) is intent only, distinct from staging (execution) — each its own action with a
//   resource-review warning. Holding/unholding is an explicit readiness change.
//
// <StrategyTab data view onViewChange onDone onNavigate />: data is the overview (its identity changes on every
//   refresh, which re-fetches /strategy). view {q, picked, open, busy, err} is the parent's (one, kept while
//   unmounted): q = source search, picked = source identifiers selected for grooming, open = the selected brief id,
//   busy/err = the in-flight action and its error. busy and err are live state, not location: leaving Strategy and
//   coming back keeps them; a late reply patches only this view. onViewChange is the parent's React-style setter;
//   onDone(result, null, toast) after a write; onNavigate({stage, run?, brief?, sources?}) owns history and the pane.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useMemo, useRef } = SDK.hooks;
const { Button, Badge, Input } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;
const API = "/api/plugins/factory";

const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "never");
const errText = (e) => String(e && e.message ? e.message : e);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const clip = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
const localTime = (iso) => new Date(iso).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
const stop = (e) => e.stopPropagation();
const BADGE = { amber: "warning", green: "success", blue: "secondary", gray: "outline", red: "destructive" };
const Tone = ({ tone, children }) => <Badge tone={BADGE[tone] || "outline"}>{children}</Badge>;
const Ext = ({ href, children }) => <a className="fx-link" href={href} target="_blank" rel="noreferrer" onClick={stop}>{children}</a>;

const post = (path, body) => SDK.fetchJSON(API + path,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// The brief body's editable fields: one short title, one outcome, and eight lists (one item per line).
const FIELDS = [
  ["title", "Title", "one"], ["outcome", "Outcome", "text"],
  ["acceptance", "Acceptance (one observable check per line)", "list"],
  ["scope", "Scope", "list"], ["exclusions", "Exclusions", "list"],
  ["decisions", "Decisions (resolved rulings + rationale)", "list"],
  ["dependencies", "Dependencies (source identifiers)", "list"],
  ["resources", "Resources (resource keys)", "list"], ["risks", "Risks", "list"],
  ["evidence", "Evidence (strings)", "list"],
];

const toForm = (body) => {
  const lines = (k) => (body?.[k] || []).join("\n");
  return { title: body?.title || "", outcome: body?.outcome || "", acceptance: lines("acceptance"),
           scope: lines("scope"), exclusions: lines("exclusions"), decisions: lines("decisions"),
           dependencies: lines("dependencies"), resources: lines("resources"), risks: lines("risks"),
           evidence: lines("evidence") };
};

const formToBody = (f) => {
  const lines = (s) => (s || "").split("\n").map((x) => x.trim()).filter(Boolean);
  return { title: (f.title || "").trim(), outcome: (f.outcome || "").trim(), acceptance: lines(f.acceptance),
           scope: lines(f.scope), exclusions: lines(f.exclusions), decisions: lines(f.decisions),
           dependencies: lines(f.dependencies), resources: lines(f.resources), risks: lines(f.risks),
           evidence: lines(f.evidence) };
};

const canonical = (f) => JSON.stringify(formToBody(f));

const STATE_TONE = { draft: "blue", approved: "green", held: "amber" };
const stateLabel = (b) => (b.state === "approved" ? "approved" : b.state === "held" ? "held" : "draft");
const DISPATCH_STAGE = { draft: "draft", staged: "run", executing: "run", done: "reconcile",
                         reconciled: "reconcile", archived: "archive" };

// One source ticket: compact and selectable. Its state (including Backlog), lead/assignee and why it is not ready are
// the server's (`reason`). `verdict` is the current verdict kind; `stale` its freshness signal.
function SourceRow({ s, checked, onToggle }) {
  return (
    <div className="fx-trow">
      <input type="checkbox" className="fx-pick" checked={checked} onChange={onToggle}
             aria-label={`Select ${s.identifier}`} />
      <div className="fx-grow">
        <div className="fx-row fx-tmeta">
          <Ext href={s.url}>{s.identifier}</Ext>
          {s.state ? <Tone tone="gray">{s.state}</Tone> : null}
          {s.repo ? <Tone tone="blue">{s.repo}</Tone> : null}
          {s.assignee ? <span className="fx-hint">{s.assignee}</span> : null}
        </div>
        <div className="fx-ttitle clamp">{s.title}</div>
        {s.reason ? <div className="fx-hint">{s.reason}</div> : null}
        {!s.reason && s.verdict ? <div className="fx-hint">verdict {s.verdict}{s.stale ? ` · ${s.stale}` : ""}</div> : null}
      </div>
    </div>
  );
}

// One brief summary: state, title/revision, its readiness blockers, and its downstream dispatch (if dispatched).
function BriefRow({ b, selected, onSelect, onDispatch }) {
  const blockers = b.blockers || [];
  return (
    <div id={`fx-brief-${b.id}`} className={`fx-trow${selected ? " picked" : ""}`} role="button" tabIndex={0}
         aria-current={selected ? "true" : undefined} onClick={onSelect}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { stop(e); onSelect(); } }}>
      <div className="fx-grow">
        <div className="fx-row fx-tmeta">
          <Tone tone={STATE_TONE[b.state] || "gray"}>{stateLabel(b)}</Tone>
          {b.source_changed?.length ? <Tone tone="red">needs amendment</Tone> : null}
          {blockers.length ? <Tone tone="amber">{plural(blockers.length, "blocker")}</Tone> : null}
        </div>
        <div className="fx-ttitle clamp">{b.title || `Brief #${b.id}`}</div>
        <div className="fx-hint">#{b.id} · revision {b.revision}{b.created_by ? ` · ${b.created_by}` : ""}
          {b.created_at ? ` · ${ago(b.created_at)}` : ""}
          {b.sources?.length ? ` · ${plural(b.sources.length, "source")}` : ""}</div>
        {blockers.length ? <div className="fx-hint">{clip(blockers.join("; "), 120)}</div> : null}
      </div>
      <div className="fx-tc-ne">
        {b.dispatch?.run_id ? (
          <button className="fx-link-btn" onClick={(e) => { stop(e); onDispatch(b.dispatch); }}>
            {b.dispatch.run_id} ›
          </button>
        ) : <span className="fx-hint">not dispatched</span>}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, kind, disabled }) {
  return (
    <label className="fx-field">
      <span className="fx-k">{label}</span>
      {kind === "one"
        ? <Input value={value} maxLength={200} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
        : <textarea className="fx-ta" rows={3} value={value} disabled={disabled}
                    onChange={(e) => onChange(e.target.value)} />}
    </label>
  );
}

// Captured sources: the server's provenance, read-only (never model-edited).
function Provenance({ sources }) {
  if (!sources || !sources.length) return <div className="fx-hint">No sources captured.</div>;
  return (
    <ul className="fx-src">
      {sources.map((s) => (
        <li key={s.issue_id || s.identifier}>
          <div className="fx-row fx-tmeta"><Ext href={s.url}>{s.identifier}</Ext>
            {s.repo ? <span className="fx-hint">{s.repo}</span> : null}
            {s.route ? <span className="fx-hint">route {s.route}</span> : null}
            {s.snapshot_updated_at ? <span className="fx-hint">snapshot {localTime(s.snapshot_updated_at)}</span> : null}
          </div>
          {s.verdict_kind ? <div className="fx-hint">verdict {s.verdict_kind}</div> : null}
        </li>
      ))}
    </ul>
  );
}

function Preview({ md, err, busy }) {
  if (busy) return <div className="fx-hint">Compiling preview…</div>;
  if (err) return <div className="fx-err">Preview unavailable: {err}</div>;
  if (!md) return <div className="fx-hint">No preview.</div>;
  return <pre className="fx-pre">{md}</pre>;
}

export function StrategyTab({ data, view, onViewChange, onDone, onNavigate }) {
  const q = view?.q || "", picked = view?.picked || [], open = view?.open || null;
  const busy = view?.busy || null, err = view?.err || null;
  const update = (patch) => onViewChange((v) => ({ ...v, ...patch }));

  // The overview: brief summaries, the source list, execution policy and active dispatches. Refetched on every
  // overview refresh (`data` is a new object each time) and on mount.
  const [all, setAll] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  useEffect(() => {
    let live = true;  // a reply after unmount is dropped
    SDK.fetchJSON(`${API}/strategy`).then((x) => { if (live) { setAll(x); setLoadErr(null); } },
                                          (e) => { if (live) setLoadErr(errText(e)); });
    return () => { live = false; };
  }, [data]);

  const briefs = all?.briefs || [];
  const tickets = all?.tickets || [];
  const policy = all?.policy || {};
  const active = all?.active || [];
  const summary = open != null ? briefs.find((b) => b.id === open) : null;

  // The open brief's full body + captured sources + compiled render, from GET /strategy/{id} (a pure read).
  const [detail, setDetail] = useState(null);
  const [detailErr, setDetailErr] = useState(null);
  const lastOpen = useRef(null);
  useEffect(() => {
    if (lastOpen.current !== open) {  // a different brief: drop the old detail; a same-version re-read keeps it
      lastOpen.current = open;
      setDetail(null); setDetailErr(null);
    }
    if (open == null) return;
    let live = true;
    SDK.fetchJSON(`${API}/strategy/${open}`).then((x) => { if (live) { setDetail(x); setDetailErr(null); } },
                                                   (e) => { if (live) setDetailErr(errText(e)); });
    return () => { live = false; };
  }, [open, summary?.revision, summary?.state]);
  const current = detail?.brief || null;

  // The editable form, reset when the open brief's version changes (a new revision is its own row).
  const [edit, setEdit] = useState(null);
  const [reason, setReason] = useState("");
  const [arm, setArm] = useState(null);  // the weighty action awaiting its second tap: publish|stage|amend|hold
  useEffect(() => { setEdit(current ? toForm(current.body) : null); setReason(""); setArm(null); },
            [current?.id, current?.revision]);  // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = !!current && !!edit && canonical(edit) !== canonical(toForm(current.body || {}));

  const call = async (path, body, what) => {  // one write at a time; busy/err live in the parent view (survive leaving)
    update({ busy: what, err: null });
    try {
      const r = await post(path, body);
      update({ busy: null });
      return r;
    } catch (e) {
      update({ busy: null, err: errText(e) });
      return null;
    }
  };

  // A write that returns a brief becomes the open detail at once (the overview refetch reconciles the summary).
  const applyResult = (r) => {
    if (!r) return false;
    setDetail({ brief: r, render: null });  // the fresh render is fetched by the detail effect
    setEdit(toForm(r.body));
    setReason(""); setArm(null);
    if (r.id !== open) { lastOpen.current = r.id; update({ open: r.id }); }
    return true;
  };

  const groom = async () => {
    const n = picked.length;
    const r = await call("/strategy/groom", { identifiers: picked }, "groom");
    if (!r) return;
    update({ picked: [] });
    applyResult(r);
    onDone(r, null, `Groomed a draft brief #${r.id} from ${n} source${n === 1 ? "" : "s"}`);
  };

  const save = async () => {
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "edited draft" }, "save");
    if (!applyResult(r)) return;
    onDone(r, null, `Saved draft #${r.id}`);
  };

  const approve = async (target) => {
    const r = await call(`/strategy/${target.id}/approve`, {}, "approve");
    if (applyResult(r)) onDone(r, null, `Published #${target.id} as approved intent (not execution)`);
  };

  const publish = async () => {
    if (arm !== "publish") { setArm("publish"); return; }
    setArm(null);
    let target = current;
    if (dirty) {  // publish the exact edited version: persist it, then approve it
      target = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "publish" }, "publish");
      if (!target) return;
      applyResult(target);
    }
    await approve(target);
  };

  const amend = async () => {  // published -> new draft revision; the reason is required
    if (arm !== "amend") { setArm("amend"); return; }
    if (!reason.trim()) return;
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() }, "amend");
    if (!applyResult(r)) return;
    onDone(r, null, `Amendment #${r.id} drafted; review and publish`);
  };

  const hold = async () => {
    if (arm !== "hold") { setArm("hold"); return; }
    if (!reason.trim()) return;
    const r = await call(`/strategy/${open}/hold`, { reason: reason.trim() }, "hold");
    if (applyResult(r)) onDone(r, null, `Held #${open}`);
  };

  const unhold = async () => {
    const r = await call(`/strategy/${open}/unhold`, {}, "unhold");
    if (applyResult(r)) onDone(r, null, `Unheld #${open}`);
  };

  const alive = useRef(true);  // a late reply never steals navigation: staging only moves the page while Strategy is open
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const stage = async () => {
    if (arm !== "stage") { setArm("stage"); return; }
    setArm(null);
    const r = await call(`/strategy/${open}/stage`, {}, "stage");
    if (!r) return;
    onDone(r, null, `Staged ${r.run_id || ""} for review`);
    if (r.run_id && alive.current) onNavigate({ stage: "draft", run: r.run_id });
  };

  const refresh = async () => {
    const r = await call("/strategy/refresh", {}, "refresh");
    if (r) onDone(r, null, "Sources refreshed");
  };

  // Sources: searchable, compact, multi-select for grooming.
  const needle = q.trim().toLowerCase();
  const list = needle ? tickets.filter((t) => `${t.identifier} ${t.title || ""}`.toLowerCase().includes(needle)) : tickets;
  const pick = (id) => update({ picked: picked.includes(id) ? picked.filter((i) => i !== id) : [...picked, id] });

  const openBrief = (id) => onNavigate({ stage: "strategy", brief: id });
  const openDispatch = (d) => onNavigate({ stage: DISPATCH_STAGE[d.state] || "draft", run: d.run_id });

  return (
    <div className="fx-stack-v">
      <div className="fx-hint">Strategy grooms sources into approved work briefs (intent). Factory keeps verification,
        planning and execution; publishing intent never starts work.</div>

      {loadErr ? <div className="fx-err" role="alert">{all ? `Refreshing strategy failed: ${loadErr}. Showing the last loaded.` : `Strategy did not load: ${loadErr}`}</div> : null}

      {/* ---- sources: select for grooming ---- */}
      <div className="fx-k">{plural(tickets.length, "source")} · grooming runs DeepSeek on the selection</div>
      <Input className="fx-search" type="search" placeholder="Search source id or title" value={q}
             onChange={(e) => update({ q: e.target.value })} />
      {busy === "groom" ? <div className="fx-err" role="status">Grooming with DeepSeek (this takes a while)…</div> : null}
      <div className="fx-row">
        <Button size="sm" disabled={!picked.length || !!busy} onClick={groom}>
          {busy === "groom" ? "Grooming…" : `Groom ${picked.length} source${picked.length === 1 ? "" : "s"}`}
        </Button>
        <Button size="sm" ghost disabled={!!busy} onClick={refresh}>{busy === "refresh" ? "Refreshing…" : "Refresh sources"}</Button>
        {picked.length ? <Button size="sm" ghost onClick={() => update({ picked: [] })}>Clear</Button> : null}
      </div>
      {err ? <div className="fx-err" role="alert">{err}</div> : null}
      <div className="fx-list">
        {!all && !loadErr ? <div className="fx-hint">Loading…</div>
          : list.length ? list.map((t) => (
            <SourceRow key={t.identifier} s={t} checked={picked.includes(t.identifier)} onToggle={() => pick(t.identifier)} />
          )) : <div className="fx-empty">{needle ? "No source matches." : "No sources yet; refresh sources first."}</div>}
      </div>

      {/* ---- active dispatches and capacity ---- */}
      {all && active.length ? (
        <details className="fx-sec fx-fold">
          <summary>Active dispatches <span className="fx-count">{active.length}</span></summary>
          <div className="fx-hint fx-line">parallel cap {policy.max_parallel ?? 2} · {active.length} active</div>
          {active.map((r) => {
            const b = briefs.find((x) => x.dispatch?.run_id === r.run_id);
            return (
              <div key={r.run_id} className="fx-hint fx-line">
                <span className="fx-id">{r.run_id}</span> · {r.state}
                {r.route ? ` · route ${r.route}` : ""}{r.pane ? ` · pane ${r.pane}` : ""}
                {b ? <> · brief <button className="fx-link-btn" onClick={() => openBrief(b.id)}>#{b.id} ›</button></> : null}
                {r.resources?.length ? <div className="fx-hint">claims: {r.resources.join(", ")}</div> : null}
              </div>
            );
          })}
        </details>
      ) : null}

      {/* ---- briefs: compact list, selectable detail ---- */}
      <div className="fx-k">{plural(briefs.length, "brief")}</div>
      <div className="fx-list">
        {briefs.length ? briefs.map((b) => (
          <BriefRow key={b.id} b={b} selected={open === b.id} onSelect={() => openBrief(b.id)} onDispatch={openDispatch} />
        )) : <div className="fx-empty">No briefs yet. Groom a source, or create one from the CLI.</div>}
      </div>

      {/* ---- selected brief: editable body, provenance, preview, actions ---- */}
      {open != null && !current && !detailErr ? <div className="fx-hint">Loading brief #{open}…</div>
        : detailErr ? <div className="fx-err" role="alert">Brief #{open} did not load: {detailErr}</div>
        : current && edit ? (
        <section className="fx-sec fx-stack-v" aria-label={`Brief #${current.id}`}>
          <div className="fx-row between">
            <div className="fx-row">
              <span className="fx-id">#{current.id}</span><span className="fx-hint">revision {current.revision}</span>
              <Tone tone={STATE_TONE[current.state] || "gray"}>{stateLabel(current)}</Tone>
              {summary?.source_changed?.length ? <Tone tone="red">needs amendment</Tone> : null}
            </div>
            <div className="fx-hint">{current.created_by} · {ago(current.created_at)}
              {current.approved_by ? ` · approved by ${current.approved_by} ${ago(current.approved_at)}` : ""}</div>
          </div>
          {current.amendment_reason ? <div className="fx-why">Amended: {current.amendment_reason}</div> : null}
          {current.hold_reason ? <div className="fx-why">Hold: {current.hold_reason}</div> : null}
          {summary?.source_changed?.length ? (
            <div className="fx-err">A source changed since this brief was captured: {summary.source_changed.join(", ")}. Amend to re-capture, or review the discrepancy before dispatching.</div>
          ) : null}
          {summary?.blockers?.length ? <div className="fx-err">Not ready: {summary.blockers.join("; ")}</div> : null}
          {current.state === "approved" ? (
            <div className="fx-why">Approved is intent only. Execution needs its own review and fresh verification; publishing does not start work.</div>
          ) : null}

          {FIELDS.map(([key, label, kind]) => (
            <Field key={key} label={label} kind={kind} value={edit[key]} disabled={!!busy}
                   onChange={(v) => setEdit((e) => ({ ...e, [key]: v }))} />
          ))}
          {dirty ? <div className="fx-hint">Unsaved edits.</div> : null}

          <div className="fx-k">Captured sources (server-captured, not model-edited)</div>
          <Provenance sources={current.sources} />

          <details className="fx-sec fx-fold">
            <summary>Compiled preview (self-contained intent)</summary>
            <Preview md={detail?.render} err={detailErr} busy={current != null && detail?.render == null && !detailErr} />
          </details>

          <div className="fx-stack-v">
            {current.state === "draft" ? (
              <>
                <div className="fx-row">
                  <Button size="sm" disabled={!!busy || !dirty} onClick={save}>{busy === "save" ? "Saving…" : "Save draft"}</Button>
                  <Button size="sm" disabled={!!busy} onClick={publish}>{busy === "publish" ? "Publishing…" : arm === "publish" ? "Confirm publish" : "Publish"}</Button>
                </div>
                {arm === "publish" ? (
                  <div className="fx-sw">
                    <div className="fx-sw-q">Publish as approved intent?</div>
                    <div className="fx-sw-detail">This approves the exact version above as the brief's intent. It does not
                      start execution, override missing evidence, answer questions, or change Linear. Unreviewed resources
                      default to <code>global:*</code> (serializes everything) until you review them.</div>
                  </div>
                ) : null}
              </>
            ) : null}

            {current.state !== "draft" ? (
              <>
                <div className="fx-row">
                  <Button size="sm" disabled={!!busy} onClick={amend}>{busy === "amend" ? "Amending…" : arm === "amend" ? "Confirm amendment" : "Amend"}</Button>
                  {current.state === "held"
                    ? <Button size="sm" disabled={!!busy} onClick={unhold}>{busy === "unhold" ? "…" : "Unhold"}</Button>
                    : <Button size="sm" ghost disabled={!!busy} onClick={hold}>{busy === "hold" ? "…" : arm === "hold" ? "Confirm hold" : "Hold"}</Button>}
                  {current.state === "approved"
                    ? <Button size="sm" disabled={!!busy} onClick={stage}>{busy === "stage" ? "Staging…" : arm === "stage" ? "Confirm stage" : "Stage"}</Button>
                    : null}
                </div>
                {(arm === "amend" || arm === "hold") ? (
                  <div className="fx-row">
                    <Input autoFocus value={reason} maxLength={2000} disabled={!!busy} placeholder="Reason (required)"
                           onChange={(e) => setReason(e.target.value)} />
                    <Button size="sm" disabled={!!busy || !reason.trim()} onClick={arm === "amend" ? amend : hold}>
                      {arm === "amend" ? "Create amendment" : "Hold"}
                    </Button>
                  </div>
                ) : null}
                {arm === "stage" ? (
                  <div className="fx-sw">
                    <div className="fx-sw-q">Stage this approved brief for execution?</div>
                    <div className="fx-sw-detail">Staging pins this brief and hands it to the draft review flow; nothing
                      runs until the review is approved. Verification may run first, and fresh code/data evidence is
                      required.</div>
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

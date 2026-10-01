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
// Layout: the brief list and the selected brief's editor come first (a deep link to a brief lands on it at once);
// the source browser is a collapsed <details> with a paged list, so hundreds of sources never bury the review.
//
// <StrategyTab data view onViewChange onDone onNavigate />: data is the overview (its identity changes on every
//   refresh, which re-fetches /strategy). view {q, picked, open, stateFilter, ctxFilter, busy, err} is the parent's
//   (one, kept while unmounted): q = source search, picked = source identifiers selected for grooming,
//   stateFilter/ctxFilter = the source list's state/context filters, open = the selected brief id, busy/err = the
//   in-flight action and its error. busy and err are live state, not location: leaving Strategy and coming back
//   keeps them; a late reply patches only this view. onViewChange is the parent's React-style setter;
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

const PAGE = 50;  // how many source rows render at a time inside the (collapsed) source browser

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
let NAV_TOKEN = 0;  // latest navigation generation; the parent bumps it synchronously on every move (go/Back)
export function bumpNavToken() { NAV_TOKEN += 1; }  // shared across remounts so a late reply sees the newest move

// One source ticket: compact and selectable. Its state (including Backlog), repo/context and why it is not ready are
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
  const superseded = b.readiness === "superseded";
  return (
    <div id={`fx-brief-${b.id}`} className={`fx-trow${selected ? " picked" : ""}`} role="button" tabIndex={0}
         aria-current={selected ? "true" : undefined} onClick={onSelect}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { stop(e); onSelect(); } }}>
      <div className="fx-grow">
        <div className="fx-row fx-tmeta">
          <Tone tone={STATE_TONE[b.state] || "gray"}>{stateLabel(b)}</Tone>
          {superseded ? <Tone tone="gray">superseded</Tone> : null}
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
  const stateFilter = view?.stateFilter || "all", ctxFilter = view?.ctxFilter || "all";
  const busy = view?.busy || null, err = view?.err || null;
  const update = (patch) => onViewChange((v) => ({ ...v, ...patch }));

  // The overview: brief summaries, the source list, and the execution scheduler's read. Refetched on every overview
  // refresh (`data` is a new object each time) and on mount.
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
  // Real execution-scheduler read (scheduler.status): capacity in use, running dispatches, launch reservations and
  // held resource claims. Held claims are not running slots — they persist through done/reconcile until archive.
  const sched = all?.scheduler || {};
  const running = sched.running || [];
  const launches = sched.launches || [];
  const holders = sched.holders || [];
  const capMax = sched.max_parallel ?? 2;
  const capUsed = sched.capacity_used ?? 0;
  const summary = open != null ? briefs.find((b) => b.id === open) : null;

  // The open brief's full body + captured sources + compiled render, from GET /strategy/{id} (a pure read).
  // `mut` bumps on every successful write so the detail (and its render) refetch even for a same-id mutation.
  const [detail, setDetail] = useState(null);
  const [detailErr, setDetailErr] = useState(null);
  const [mut, setMut] = useState(0);
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
  }, [open, summary?.revision, summary?.state, mut]);
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

  // Apply a write's result only if the operator is still where they submitted it. `atNav` is the navigation
  // generation captured at submission; a move (including away-and-back) bumps it, so a late reply never changes the
  // selection, never acts on a newly selected brief, and never steals navigation. No autosave, no auto-approval.
  const applyGuarded = (r, atNav) => {
    if (!r || NAV_TOKEN !== atNav) return false;
    setDetail({ brief: r, render: null });
    setDetailErr(null);
    setEdit(toForm(r.body));
    setReason(""); setArm(null);
    setMut((m) => m + 1);  // refetch the compiled render even for same-id mutations (hold/unhold/approve)
    return true;
  };
  // When a write returned a new revision id and the context is still eligible, the URL/history follows that id.
  const settleId = (r, atOpen) => {
    if (r.id !== atOpen) { lastOpen.current = r.id; onNavigate({ stage: "strategy", brief: r.id }); }
  };

  // Editing any field (title, outcome, resources, …) disarms an armed publish so the human always re-confirms the
  // values actually shown; nothing is auto-saved or auto-approved.
  const editField = (key, v) => { setEdit((e) => ({ ...e, [key]: v })); setArm(null); };

  const groom = async () => {
    const submitted = [...picked];
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call("/strategy/groom", { identifiers: submitted }, "groom");
    if (!r) return;
    // Remove only the identifiers this request submitted; picks made since are left alone.
    onViewChange((s) => ({ picked: (s.picked || []).filter((i) => !submitted.includes(i)) }));
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Groomed a draft brief #${r.id} from ${submitted.length} source${submitted.length === 1 ? "" : "s"}`);
  };

  const save = async () => {
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "edited draft" }, "save");
    if (!applyGuarded(r, atNav)) return;
    settleId(r, atOpen);
    onDone(r, null, `Saved draft #${r.id}`);
  };

  const publish = async () => {
    if (arm !== "publish") { setArm("publish"); return; }
    setArm(null);
    const atOpen = open, atNav = NAV_TOKEN;
    let target = current;
    if (dirty) {  // publish the exact edited version: persist it, then approve it
      target = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "publish" }, "publish");
      if (!target) return;
    }
    const approved = await call(`/strategy/${target.id}/approve`, {}, "approve");
    if (!approved) return;
    if (!applyGuarded(approved, atNav)) return;
    settleId(approved, atOpen);
    onDone(approved, null, `Published #${approved.id} as approved intent (not execution)`);
  };

  const amend = async () => {  // published -> new draft revision; the reason is required
    if (arm !== "amend") { setArm("amend"); return; }
    if (!reason.trim()) return;
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() }, "amend");
    if (!applyGuarded(r, atNav)) return;
    settleId(r, atOpen);
    onDone(r, null, `Amendment #${r.id} drafted; review and publish`);
  };

  const hold = async () => {
    if (arm !== "hold") { setArm("hold"); return; }
    if (!reason.trim()) return;
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/hold`, { reason: reason.trim() }, "hold");
    if (!applyGuarded(r, atNav)) return;
    onDone(r, null, `Held #${open}`);
  };

  const unhold = async () => {
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/unhold`, {}, "unhold");
    if (!applyGuarded(r, atNav)) return;
    onDone(r, null, `Unheld #${open}`);
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

  // Sources: searchable, filterable by state/context, compact, multi-select, paged inside a collapsed <details>.
  const needle = q.trim().toLowerCase();
  const states = useMemo(() => [...new Set(tickets.map((t) => t.state).filter(Boolean))].sort(), [tickets]);
  const contexts = useMemo(() => [...new Set(tickets.map((t) => t.context).filter(Boolean))].sort(), [tickets]);
  const filtered = tickets.filter((t) =>
    (stateFilter === "all" || t.state === stateFilter) &&
    (ctxFilter === "all" || t.context === ctxFilter));
  const list = needle ? filtered.filter((t) => `${t.identifier} ${t.title || ""}`.toLowerCase().includes(needle)) : filtered;
  const [srcOpen, setSrcOpen] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => { setLimit(PAGE); }, [needle, stateFilter, ctxFilter]);
  const shown = list.slice(0, limit);
  const pick = (id) => update({ picked: picked.includes(id) ? picked.filter((i) => i !== id) : [...picked, id] });

  const openBrief = (id) => onNavigate({ stage: "strategy", brief: id });
  const openDispatch = (d) => onNavigate({ stage: d.phase || DISPATCH_STAGE[d.state] || "draft", run: d.run_id });

  return (
    <div className="fx-stack-v">
      <div className="fx-hint">Strategy grooms sources into approved work briefs (intent). Factory keeps verification,
        planning and execution; publishing intent never starts work.</div>

      {loadErr ? <div className="fx-err" role="alert">{all ? `Refreshing strategy failed: ${loadErr}. Showing the last loaded.` : `Strategy did not load: ${loadErr}`}</div> : null}
      {busy ? <div className="fx-hint" role="status">{busy === "groom" ? "Grooming with DeepSeek (this takes a while)…" : "Working…"}</div> : null}
      {err ? <div className="fx-err" role="alert">{err}</div> : null}

      {/* ---- briefs: compact list, then the selected detail (a deep link lands here at once) ---- */}
      <div className="fx-k">{plural(briefs.length, "brief")}</div>
      <div className="fx-list">
        {briefs.length ? briefs.map((b) => (
          <BriefRow key={b.id} b={b} selected={open === b.id}
                    onSelect={() => openBrief(b.id)} onDispatch={openDispatch} />
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
              {summary?.readiness === "superseded" ? <Tone tone="gray">superseded</Tone> : null}
              {summary?.source_changed?.length ? <Tone tone="red">needs amendment</Tone> : null}
            </div>
            <div className="fx-hint">{current.created_by} · {ago(current.created_at)}
              {current.approved_by ? ` · approved by ${current.approved_by} ${ago(current.approved_at)}` : ""}</div>
          </div>
          {current.amendment_reason ? <div className="fx-why">Amended: {current.amendment_reason}</div> : null}
          {current.hold_reason ? <div className="fx-why">Hold: {current.hold_reason}</div> : null}
          {summary?.readiness === "superseded" ? (
            <div className="fx-err">Superseded — a newer revision exists; a draft with a child cannot be published.</div>
          ) : null}
          {summary?.source_changed?.length ? (
            <div className="fx-err">A source changed since this brief was captured: {summary.source_changed.join(", ")}. Amend to re-capture, or review the discrepancy before dispatching.</div>
          ) : null}
          {summary?.blockers?.length ? <div className="fx-err">Not ready: {summary.blockers.join("; ")}</div> : null}
          {current.state === "approved" ? (
            <div className="fx-why">Approved is intent only. Execution needs its own review and fresh verification; publishing does not start work.</div>
          ) : null}

          {FIELDS.map(([key, label, kind]) => (
            <Field key={key} label={label} kind={kind} value={edit[key]} disabled={!!busy}
                   onChange={(v) => editField(key, v)} />
          ))}
          {dirty ? (current.state === "draft"
            ? <div className="fx-hint">Unsaved edits.</div>
            : <div className="fx-err">Unsaved edits are not the approved intent — amend (with a reason) before staging.</div>
          ) : null}

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
                  {summary?.readiness !== "superseded"
                    ? <Button size="sm" disabled={!!busy} onClick={publish}>{busy === "publish" ? "Publishing…" : arm === "publish" ? "Confirm publish" : "Publish"}</Button>
                    : null}
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
                    ? <Button size="sm" disabled={!!busy || dirty} onClick={stage}>{busy === "stage" ? "Staging…" : arm === "stage" ? "Confirm stage" : "Stage"}</Button>
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

      {/* ---- capacity and scheduling (real scheduler.status, shown even at zero use) ---- */}
      {all ? (
        <details className="fx-sec fx-fold">
          <summary>Capacity & scheduling <span className="fx-count">{capUsed}/{capMax}</span></summary>
          <div className="fx-hint fx-line">parallel cap {capMax} · {capUsed} in use · {plural(running.length, "dispatch")} running · {plural(launches.length, "launch reservation")}</div>
          {running.length ? <>
            <div className="fx-k">Running</div>
            {running.map((r) => (
              <div key={r.run_id} className="fx-hint fx-line">
                <span className="fx-id">{r.run_id}</span> · executing{r.route ? ` · route ${r.route}` : ""}{r.executor_pane ? ` · pane ${r.executor_pane}` : ""}
                {r.brief_id ? <> · brief <button className="fx-link-btn" onClick={() => openBrief(r.brief_id)}>#{r.brief_id} ›</button></> : null}
              </div>
            ))}
          </> : null}
          {launches.length ? <>
            <div className="fx-k">Launch reservations</div>
            {launches.map((l) => {
              const b = briefs.find((x) => x.dispatch?.run_id === l.run_id);
              return (
                <div key={l.run_id} className="fx-hint fx-line">
                  <span className="fx-id">{l.run_id}</span> · launch {l.state} · pane {l.pane_id || "—"}
                  {l.state === "uncertain" ? <span className="fx-err"> · uncertain — confirm unsent before handing off again</span> : null}
                  {b ? <> · brief <button className="fx-link-btn" onClick={() => openBrief(b.id)}>#{b.id} ›</button></> : null}
                </div>
              );
            })}
          </> : null}
          {holders.length ? <>
            <div className="fx-k">Held resource claims (not running slots)</div>
            {holders.map((h, i) => (
              <div key={`${h.run_id}-${h.resource}-${i}`} className="fx-hint fx-line">
                <span className="fx-id">{h.resource}</span> · {h.run_id} · {h.state}
                {h.brief_id ? <> · brief <button className="fx-link-btn" onClick={() => openBrief(h.brief_id)}>#{h.brief_id} ›</button></> : null}
              </div>
            ))}
          </> : null}
        </details>
      ) : null}

      {/* ---- sources: collapsed, paged browser (never buries the brief review above) ---- */}
      <details className="fx-sec fx-fold" onToggle={(e) => setSrcOpen(e.target.open)}>
        <summary>Sources <span className="fx-count">{tickets.length}</span>{picked.length ? ` · ${picked.length} picked` : ""}</summary>
        {srcOpen ? (
          <>
            <Input className="fx-search" type="search" placeholder="Search source id or title" value={q}
                   onChange={(e) => update({ q: e.target.value })} />
            <div className="fx-row fx-filters">
              <select className="fx-select" value={stateFilter} aria-label="State filter"
                      onChange={(e) => update({ stateFilter: e.target.value })}>
                <option value="all">All states</option>
                {states.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select className="fx-select" value={ctxFilter} aria-label="Context filter"
                      onChange={(e) => update({ ctxFilter: e.target.value })}>
                <option value="all">All contexts</option>
                {contexts.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div className="fx-row">
              <Button size="sm" disabled={!picked.length || !!busy} onClick={groom}>
                {busy === "groom" ? "Grooming…" : `Groom ${picked.length} source${picked.length === 1 ? "" : "s"}`}
              </Button>
              <Button size="sm" ghost disabled={!!busy} onClick={refresh}>{busy === "refresh" ? "Refreshing…" : "Refresh sources"}</Button>
              {picked.length ? <Button size="sm" ghost onClick={() => update({ picked: [] })}>Clear</Button> : null}
            </div>
            <div className="fx-list">
              {!all && !loadErr ? <div className="fx-hint">Loading…</div>
                : shown.length ? shown.map((t) => (
                  <SourceRow key={t.identifier} s={t} checked={picked.includes(t.identifier)} onToggle={() => pick(t.identifier)} />
                )) : <div className="fx-empty">{needle || stateFilter !== "all" || ctxFilter !== "all" ? "No source matches." : "No sources yet; refresh sources first."}</div>}
            </div>
            {list.length > shown.length ? (
              <div className="fx-row">
                <Button size="sm" ghost onClick={() => setLimit((l) => l + PAGE)}>Show {list.length - shown.length} more</Button>
              </div>
            ) : null}
          </>
        ) : null}
      </details>
    </div>
  );
}

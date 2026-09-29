// Factory tab. Plain words first; ids, hashes and evidence one click away. Actions (draft, note, approve/hold/
// reject a draft, resolve flag) go through the plugin API to the factory CLI, which enforces every invariant.
// Built by install.sh (`bun build`, classic JSX via tsconfig.json) to dist/index.js; React comes from the SDK.
const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useState, useEffect, useCallback } = SDK.hooks;
const { Button } = SDK.components;
const h = React.createElement;
const Fragment = React.Fragment;  // for <>…</>
const REFRESH_MS = 30000;

const ago = (iso) => (iso ? SDK.utils.isoTimeAgo(iso) : "never");
const epochAgo = (v) => (v == null ? "never" : typeof v === "number" ? SDK.utils.timeAgo(v) : ago(v));
const errText = (e) => String(e && e.message ? e.message : e);
const stop = (e) => e.stopPropagation();
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const API = "/api/plugins/factory";
const post = (path, body) => SDK.fetchJSON(API + path,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const localTime = (iso) => new Date(iso).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
const until = (iso) => {
  const m = Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
};

// One button's request: busy while in flight, the API's refusal text shown next to it.
function useAction(onDone) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const run = (path, body) => {
    setBusy(true); setErr(null);
    return post(path, body).then((r) => { setBusy(false); onDone(r); }, (e) => { setBusy(false); setErr(errText(e)); });
  };
  return { busy, err, run, clear: () => setErr(null) };
}
const ActErr = ({ err }) => (err ? <span className="act-err" role="alert">{err}</span> : null);
const Chip = ({ tone, children, title }) => <span className={`chip ${tone}`} title={title}>{children}</span>;
const Ext = ({ href, children }) => <a href={href} target="_blank" rel="noreferrer" onClick={stop}>{children}</a>;

// Text box + submit, used for notes, flag resolutions and hold/reject reasons. Enter submits single-line boxes.
function TextAction({ label, placeholder, submit, busyText, multiline, tone, a, onCancel, autoFocus, maxLength = 4000 }) {
  const [text, setText] = useState("");
  const go = () => text.trim() && submit(text.trim());
  const Box = multiline ? "textarea" : "input";
  return (
    <div className="act" onClick={stop}>
      <Box type={multiline ? undefined : "text"} rows={multiline ? 3 : undefined} value={text} maxLength={maxLength}
           disabled={a.busy} aria-label={placeholder} placeholder={placeholder} autoFocus={autoFocus}
           onChange={(e) => setText(e.target.value)}
           onKeyDown={(e) => {
             if (e.key === "Escape" && onCancel) onCancel();
             if (e.key === "Enter" && (!multiline || e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); }
           }} />
      <Button size="sm" variant={tone === "danger" ? "destructive" : undefined} disabled={a.busy || !text.trim()} onClick={go}>
        {a.busy ? busyText : label}
      </Button>
      {onCancel ? <Button size="sm" variant="ghost" disabled={a.busy} onClick={onCancel}>Cancel</Button> : null}
      <ActErr err={a.err} />
    </div>
  );
}

// ---- plain-language status for one ticket ------------------------------------------------
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
  if (t.dispatch) return { group: "dispatch", tone: "blue", label: "In a dispatch", why: `Card ${CARD[t.dispatch.card_status] || t.dispatch.card_status}` };
  if (!t.context) return { group: "ignored", tone: "gray", label: "Not mapped", why: "No repo is mapped for this domain, so it isn't verified" };
  if (!v || t.freshness !== "fresh") return { group: "progress", tone: "blue", label: "Checking", why: RECHECK[t.freshness] || "Queued for verification" };
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

// ---- plain-language status for one dispatch ----------------------------------------------
const STEP_LABEL = { draft: "Drafted", staged: "Approved", executing: "Being worked on",
                     done: "Finished", reconciled: "Written to Linear", archived: "Archived" };

function reviewStatus(d) {
  switch (d.review) {
    case "planning": return { tone: "gray", label: "Planning…", why: "A planner is writing the plan. Nothing runs yet." };
    case "in-review": return { tone: "amber", label: `Auto-starts in ${until(d.review_until)}`,
                               why: `Starts on its own at ${localTime(d.review_until)} unless you hold or reject it.` };
    case "held": return { tone: "amber", label: "Held", why: `Held: ${d.held_reason || "no reason given"}. Starts only when you approve.` };
    default: return { tone: "amber", label: "Needs approval", why: "Nothing starts until you approve." };
  }
}

function dispatchStatus(d) {
  const cards = d.tickets || [];
  const n = (s) => cards.filter((c) => c.card_status === s).length;
  switch (d.state) {
    case "draft": return reviewStatus(d);
    case "staged": return { tone: "blue", label: "Starting", why: `Approved by ${d.approved_by || "?"}; starting on factory-fleet` };
    case "executing": return { tone: "blue", label: "Being worked on",
                               why: `${n("done")} of ${cards.length} done` + (n("blocked") ? `, ${n("blocked")} blocked` : "") };
    case "done": return { tone: "blue", label: "Writing back", why: `${n("done")} done, ${n("blocked")} blocked; results going to Linear` };
    case "reconciled": return { tone: "green", label: "Written back", why: "Waiting to be archived" };
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
const repos = (d) => { try { return JSON.parse(d.repos_json || "[]").map((r) => r.repo.split("/").pop()); } catch { return []; } };

// ---- rows ----------------------------------------------------------------------------------
function Evidence({ items }) {
  const KIND = { file: "Code", sql: "Data", dagster: "Pipeline run", linear: "Ticket", pr: "Pull request" };
  return (
    <ul className="evidence">
      {(items || []).map((e, i) => (
        <li key={i}>
          <strong>{KIND[e.type] || e.type}</strong>{": "}
          {e.path ? <code>{e.path}</code> : e.url ? <Ext href={e.url}>{e.url}</Ext> : e.ref ? e.ref : e.witness ? <code>{e.witness}</code> : null}
          {e.note ? ` — ${e.note}` : ""}
        </li>
      ))}
    </ul>
  );
}

function Row({ chip, title, meta, why, open, onToggle, lead, children }) {
  return (
    <div className={`row${onToggle ? " clickable" : ""}${open ? " open" : ""}`} onClick={onToggle}
         aria-expanded={onToggle ? !!open : undefined}>
      {lead ? <div className="lead">{lead}</div> : null}
      <div className="body">
        <div className="head"><div className="title">{title}</div>{chip}</div>
        {meta ? <div className="meta">{meta}</div> : null}
        {why ? <div className={`why${open ? "" : " clamp"}`}>{why}</div> : null}
        {children}
      </div>
    </div>
  );
}

function TicketRow({ t, pick }) {
  const [open, setOpen] = useState(false);
  const s = t._s;
  return (
    <Row open={open} onToggle={() => setOpen(!open)} chip={<Chip tone={s.tone}>{s.label}</Chip>} title={t.title} why={s.why}
         lead={pick ? <input type="checkbox" className="pick" checked={pick.checked} disabled={pick.disabled} onClick={stop}
                             onChange={pick.toggle} aria-label={`Select ${t.identifier}`} /> : null}
         meta={<><Ext href={t.url}>{t.identifier}</Ext>{` · ${t.domain} · ${t.linear_state} · ${t.assignee ? t.assignee.split("@")[0] : "unassigned"}`}</>}>
      {open ? (
        <div className="detail">
          {t.verdict
            ? <><div className="meta">{`Verified ${ago(t.verdict.created_at)} by ${t.verdict.created_by}` + (t.repo ? ` against ${t.repo}` : "")}</div>
                <Evidence items={t.verdict.evidence} /></>
            : <div className="meta">No verification yet.</div>}
        </div>
      ) : null}
    </Row>
  );
}

// Floating bar for the Ready list: shows once something is ticked.
function StageBar({ picked, max, all, setPicked, onDone }) {
  const a = useAction(() => { setPicked([]); onDone(); });
  if (!picked.length) {
    return (
      <div className="hintbar">
        Tick related tickets to plan them as one dispatch (up to {max}).
        <Button size="sm" variant="ghost" onClick={() => setPicked(all.slice(0, max))}>Select {Math.min(all.length, max) === all.length ? "all" : `first ${max}`}</Button>
      </div>
    );
  }
  return (
    <div className="stagebar" role="region" aria-label="Selection">
      <span><strong>{picked.length}</strong> of max {max} selected: {picked.join(", ")}</span>
      <span className="grow" />
      <ActErr err={a.err} />
      <Button size="sm" variant="ghost" disabled={a.busy} onClick={() => setPicked([])}>Clear</Button>
      <Button size="sm" disabled={a.busy} onClick={() => a.run("/stage", { identifiers: picked })}>
        {a.busy ? "Drafting…" : "Draft dispatch"}
      </Button>
    </div>
  );
}

// ---- review --------------------------------------------------------------------------------
function PlanNode({ d, node, depth, onDone, open, toggle }) {
  const [noting, setNoting] = useState(false);
  const a = useAction(() => { setNoting(false); onDone(); });
  const root = node.kind === "dispatch";
  const title = root ? (node.title === d.run_id ? "Whole dispatch" : node.title) : node.title;
  return (
    <div className={`node ${node.kind}`} style={{ marginLeft: `${Math.max(0, depth - 1) * 1.1}rem` }}>
      <div className="node-head" onClick={node.detail ? toggle : undefined}>
        <span className="node-id">{root ? "dispatch" : node.id}</span>
        <strong>{title}</strong>
        {node.depends_on?.length ? <span className="meta">after {node.depends_on.join(", ")}</span> : null}
        {node.notes?.length ? <Chip tone="amber">{plural(node.notes.length, "note")}</Chip> : null}
      </div>
      {node.detail ? <div className={`why${open ? "" : " clamp"}`} onClick={toggle}>{node.detail}</div> : null}
      {(node.notes || []).map((n) => (
        <div key={n.id} className="note">
          <div className="meta">{`${n.author} · `}<span title={localTime(n.at)}>{ago(n.at)}</span></div>{n.body}
        </div>
      ))}
      {d.state !== "draft" ? null : noting
        ? <TextAction a={a} autoFocus multiline={root} label="Add note" busyText="Adding…" onCancel={() => setNoting(false)}
                      placeholder={root ? "Note for the whole dispatch (⌘↵ to add)" : `Note on ${node.id}`}
                      submit={(body) => a.run(`/drafts/${encodeURIComponent(d.run_id)}/notes`, { node: node.id, body })} />
        : <button className="link" onClick={() => setNoting(true)}>+ note</button>}
    </div>
  );
}

function Plan({ d, onDone }) {
  const tree = d.tree || [];
  const [open, setOpen] = useState({});
  const depth = {};
  tree.forEach((n) => { depth[n.id] = n.parent == null ? 0 : (depth[n.parent] ?? 0) + 1; });
  const steps = tree.filter((n) => n.kind === "step").length;
  const notes = tree.reduce((k, n) => k + (n.notes?.length || 0), 0);
  const withDetail = tree.filter((n) => n.detail).map((n) => n.id);
  const allOpen = withDetail.length && withDetail.every((id) => open[id]);
  return (
    <div className="plan" onClick={stop}>
      <div className="plan-head">
        <strong>Plan</strong>
        <span className="meta">{steps ? plural(steps, "step") : "no steps yet"} · {plural(notes, "note")} · notes go to the executor word for word and can't be edited</span>
        <span className="grow" />
        {withDetail.length ? <button className="link" onClick={() => setOpen(allOpen ? {} : Object.fromEntries(withDetail.map((id) => [id, true])))}>
          {allOpen ? "Collapse all" : "Expand all"}</button> : null}
      </div>
      <div className="tree">
        {tree.map((n) => <PlanNode key={n.id} d={d} node={n} depth={depth[n.id]} onDone={onDone} open={!!open[n.id]}
                                   toggle={() => setOpen({ ...open, [n.id]: !open[n.id] })} />)}
      </div>
    </div>
  );
}

function ReviewActions({ d, onDone }) {
  const [mode, setMode] = useState(null);  // null | approve | hold | reject
  const [warn, setWarn] = useState(null);
  const a = useAction((r) => { setMode(null); setWarn(r && r.handoff_error ? `Approved, but starting failed: ${r.handoff_error}. The factory will retry.` : null); onDone(); });
  const id = encodeURIComponent(d.run_id);
  const pick = (m) => { a.clear(); setMode(mode === m ? null : m); };
  const n = (d.tickets || []).length;
  return (
    <div className="review-actions" onClick={stop}>
      <div className="act">
        <Button size="sm" disabled={a.busy} onClick={() => pick("approve")}>Approve & start</Button>
        {d.review === "held" ? null : <Button size="sm" variant="outline" disabled={a.busy} onClick={() => pick("hold")}>Hold</Button>}
        <Button size="sm" variant="outline" disabled={a.busy} onClick={() => pick("reject")}>Reject</Button>
        {warn ? <span className="act-err" role="alert">{warn}</span> : null}
      </div>
      {mode === "approve" ? (
        <div className="confirm">
          <div>Approving freezes the plan and your notes, then starts real work on factory-fleet: branches, commits and
            pull requests for {plural(n, "ticket")}. This can take a few minutes.</div>
          <div className="act">
            <Button size="sm" disabled={a.busy} onClick={() => a.run(`/drafts/${id}/approve`, {})}>{a.busy ? "Approving…" : "Confirm: approve & start"}</Button>
            <Button size="sm" variant="ghost" disabled={a.busy} onClick={() => setMode(null)}>Cancel</Button>
            <ActErr err={a.err} />
          </div>
        </div>
      ) : mode ? (
        <div className="confirm">
          <div>{mode === "hold" ? "Holding stops the automatic start; it then starts only when you approve." : "Rejecting discards the draft; its tickets can be drafted again later."}</div>
          <TextAction a={a} autoFocus label={mode === "hold" ? "Hold" : "Reject"} busyText={mode === "hold" ? "Holding…" : "Rejecting…"}
                      tone={mode === "reject" ? "danger" : undefined} placeholder={`Why ${mode}? (required)`} onCancel={() => setMode(null)}
                      submit={(reason) => a.run(`/drafts/${id}/${mode}`, { reason })} />
        </div>
      ) : null}
    </div>
  );
}

// ---- dispatches, flags, closed -------------------------------------------------------------
function Progress({ cards }) {
  if (cards.length < 2) return null;
  return (
    <div className="progress" aria-label="Ticket progress">
      {cards.map((c) => <span key={c.identifier} className={`seg ${CARD_TONE[c.card_status] || "gray"}`} title={`${c.identifier}: ${CARD[c.card_status] || c.card_status}`} />)}
    </div>
  );
}

function DispatchRow({ d, titles, onDone }) {
  const [open, setOpen] = useState(false);
  const s = dispatchStatus(d);
  const cards = d.tickets || [];
  const who = d.auto ? "drafted by the factory" : d.drafted_by ? `drafted by ${d.drafted_by.replace(/^user:/, "")}` : null;
  return (
    <Row open={open} onToggle={() => setOpen(!open)} title={dispatchTitle(d, titles)} why={s.why}
         chip={<Chip tone={s.tone} title={s.why}>{s.label}</Chip>}
         meta={[d.run_id, ...repos(d), who, `created ${ago(d.created_at)}`].filter(Boolean).join(" · ")}>
      {d.emergency ? <Chip tone="amber">Emergency: no review window</Chip> : null}
      {d.hash_ok === false ? <div className="act-err">Warning: the dispatch file was modified after approval.</div> : null}
      <Progress cards={cards} />
      <ul className="cards">
        {cards.map((c) => (
          <li key={c.identifier}>
            <Chip tone={CARD_TONE[c.card_status] || "gray"}>{CARD[c.card_status] || c.card_status}</Chip>
            <strong>{c.identifier}</strong>
            {titles[c.identifier] && cards.length > 1 ? <span className="meta">{titles[c.identifier]}</span> : null}
            {c.pr_url ? <Ext href={c.pr_url}>{c.pr_url.replace("https://github.com/", "")}</Ext> : null}
          </li>
        ))}
      </ul>
      {d.state === "draft" ? <><ReviewActions d={d} onDone={onDone} /><Plan d={d} onDone={onDone} /></> : null}
      {open ? (
        <ul className="history" onClick={stop}>
          {(d.transitions || []).map((x, i) => <li key={i} className="meta">{`${STEP_LABEL[x.to_state] || x.to_state} — by ${x.actor}, ${ago(x.at)}`}</li>)}
        </ul>
      ) : null}
    </Row>
  );
}

function FlagRow({ f, onDone }) {
  const a = useAction(onDone);
  return (
    <Row chip={<Chip tone="amber">{FLAG_LABEL(f)}</Chip>} why={f.reason || ""}
         title={f.identifier ? <><Ext href={f.url}>{f.identifier}</Ext>{`: ${f.title || ""}`}</> : f.kind}
         meta={`Flag ${f.id}` + (f.run_id ? ` · from ${f.run_id}` : "") + " · once handled, say what you decided (never written to Linear)"}>
      <TextAction a={a} label="Resolve" busyText="Resolving…" placeholder="What you decided" maxLength={2000}
                  submit={(resolution) => a.run(`/flags/${f.id}/resolve`, { resolution })} />
    </Row>
  );
}

const CLOSED_LABEL = { "already-done": "Already done", "duplicate-of": "Duplicate" };
const ClosedTicketRow = ({ w }) => (
  <Row chip={<Chip tone="green">{CLOSED_LABEL[w.kind] || w.kind}</Chip>} title={w.title}
       meta={<><Ext href={w.url}>{w.identifier}</Ext>{` · now ${w.linear_state} in Linear` + (w.target ? ` · duplicate of ${w.target}` : "") + ` · ${w.run_id}`}</>} />
);
const ArchivedRow = ({ a, titles }) => (
  <Row chip={<Chip tone="green">Archived</Chip>} title={titles[a.tickets] || `Dispatch ${a.run_id}`}
       meta={`${a.run_id} · ${a.tickets} · ${a.done} done` + (a.blocked ? `, ${a.blocked} blocked` : "") + ` · archived ${ago(a.archived_at)}`} />
);

// ---- health + throughput -------------------------------------------------------------------
const JOB_NAME = { "factory-ingest": "Linear sync", "factory-prune": "Verification", "factory-plan": "Planning",
                   "factory-propose": "Proposals", "factory-reconcile": "Write-back", "factory-backup": "Backup" };
function Health({ jobs }) {
  const name = (j) => JOB_NAME[j.name] || j.name;
  const bad = jobs.filter((j) => j.last_status && !["ok", "success", "succeeded"].includes(j.last_status));
  const detail = jobs.map((j) => `${name(j)}: ${j.last_status || "not run"}, ${epochAgo(j.last_run_at)}`).join("\n");
  return (
    <span className={`health ${bad.length ? "bad" : "ok"}`} title={detail}>
      <span className="dot" />
      {bad.length ? bad.map((j) => `${name(j)} failed: ${j.last_error || j.last_status}`).join(" · ") : `All ${jobs.length} jobs healthy`}
    </span>
  );
}

const pct = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
const hours = (v) => (v == null ? "—" : v < 48 ? `${v.toFixed(1)} h` : `${(v / 24).toFixed(1)} days`);
const Stat = ({ k, v }) => <div className="stat"><div className="v">{v}</div><div className="k">{k}</div></div>;
const Table = ({ head, rows }) => (
  <table className="tbl">
    <thead><tr>{head.map((c, i) => <th key={i} className={i ? "r" : ""}>{c}</th>)}</tr></thead>
    <tbody>{rows.map((r, j) => <tr key={j}>{r.map((c, i) => <td key={i} className={i ? "r" : ""}>{c}</td>)}</tr>)}</tbody>
  </table>
);

function Throughput({ stamp }) {
  const [m, setM] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    SDK.fetchJSON(`${API}/metrics?days=28`).then((d) => { setM(d); setErr(null); }, (e) => setErr(errText(e)));
  }, [stamp]);
  if (!m) return err ? <div className="err">Throughput unavailable: {err}</div> : <div className="empty">Loading…</div>;
  const verdicts = Object.entries(m.verdicts || {}).map(([k, n]) => `${k} ${n}`).join(" · ");
  return (
    <div>
      {err ? <div className="act-err">Last refresh failed: {err}</div> : null}
      <div className="stats">
        <Stat k="tickets done" v={m.tickets.done} />
        <Stat k="tickets blocked" v={m.tickets.blocked} />
        <Stat k="block rate" v={pct(m.block_rate)} />
        <Stat k="approve → done, median" v={hours(m.hours.stage_to_done_p50)} />
        <Stat k="approve → done, slowest" v={hours(m.hours.stage_to_done_max)} />
        <Stat k="done → archived, median" v={hours(m.hours.done_to_archived_p50)} />
      </div>
      <div className="sub">{`Dispatches: ${m.dispatches.staged} approved, ${m.dispatches.archived} archived · ` +
        `Linear writes: ${m.writeback.confirmed} confirmed, ${m.writeback.failed} failed, ${m.writeback.flagged} flagged` +
        (verdicts ? ` · Verdicts: ${verdicts}` : "")}</div>
      {m.per_week.length ? <Table head={["Week of", "Approved", "Done", "Blocked"]} rows={m.per_week.map((w) => [w.week, w.staged, w.done, w.blocked])} /> : null}
      {m.per_repo.length ? <Table head={["Repo", "Done", "Blocked"]} rows={m.per_repo.map((r) => [r.repo, r.done, r.blocked])} /> : null}
    </div>
  );
}

// ---- page --------------------------------------------------------------------------------
// One section per lifecycle stage, in order. `tone` colors the stage pill; `you` marks stages that wait on you.
const STAGES = [
  { key: "answer", title: "Needs your answer", tone: "amber", you: true, hint: "The check couldn't decide; fix or answer in Linear.", empty: "No questions for you." },
  { key: "review", title: "In review", tone: "amber", you: true, hint: "Read the plan, leave notes, then approve, hold or reject.", empty: "No draft waiting for review." },
  { key: "writeback", title: "Writing back", tone: "blue", hint: "Finished; results going to Linear. Held writes need you.", empty: "Nothing waiting to be written to Linear." },
  { key: "working", title: "Being worked on", tone: "blue", hint: "factory-fleet is building it.", empty: "No dispatch running." },
  { key: "staged", title: "Starting", tone: "blue", hint: "Approved and frozen; starting on factory-fleet.", empty: "No dispatch waiting to start." },
  { key: "ready", title: "Ready", tone: "green", hint: "Verified and free to draft into a dispatch.", empty: "No verified tickets waiting. New verifications land every 20 minutes." },
  { key: "checking", title: "Checking", tone: "blue", hint: "Verifying tickets against code and data.", empty: "Nothing being verified." },
  { key: "closed", title: "Closed", tone: "green", hint: "Written to Linear and archived.", empty: "Nothing closed yet.", cap: 5 },
];
// Strip order follows the lifecycle; sections list what needs you first.
const FLOW = ["checking", "answer", "ready", "review", "staged", "working", "writeback", "closed"];
const DISPATCH_STAGE = { draft: "review", staged: "staged", executing: "working", done: "writeback", reconciled: "writeback" };
const TICKET_STAGE = { progress: "checking", you: "answer", ready: "ready" };

function Section({ s, rows, extra }) {
  const [all, setAll] = useState(false);
  const shown = s.cap && !all ? rows.slice(0, s.cap) : rows;
  return (
    <section id={`fx-${s.key}`}>
      <h2><span className={`dot ${rows.length ? s.tone : "idle"}`} />{s.title}<span className="n">{rows.length}</span><span className="hint">{s.hint}</span></h2>
      {extra}
      {rows.length ? <div className="list">{shown}</div> : <div className="empty">{s.empty}</div>}
      {shown.length < rows.length ? <button className="link more" onClick={() => setAll(true)}>Show all {rows.length}</button> : null}
    </section>
  );
}

function FactoryPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [picked, setPicked] = useState([]);
  const [focus, setFocus] = useState(null);
  const [showTp, setShowTp] = useState(false);  // metrics load only once the fold is opened
  const [, tick] = useState(0);
  const load = useCallback(() => {
    SDK.fetchJSON(`${API}/overview`)
      .then((d) => { setData(d); setError(null); setLoadedAt(new Date().toISOString()); })
      .catch((e) => setError(errText(e)));
  }, []);
  useEffect(() => { load(); const t = setInterval(load, REFRESH_MS); return () => clearInterval(t); }, [load]);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 60000); return () => clearInterval(t); }, []);  // countdowns

  if (!data) return <div className="fx">{error ? <div className="err">{error}</div> : <div className="empty">Loading…</div>}</div>;

  const max = data.candidates?.max_tickets || 0;
  const stageable = (data.candidates?.candidates || []).map((c) => c.identifier);
  const sel = picked.filter((i) => stageable.includes(i));  // drop picks that stopped being candidates
  const pickFor = (id) => stageable.includes(id) ? {
    checked: sel.includes(id), disabled: !sel.includes(id) && sel.length >= max,
    toggle: () => setPicked(sel.includes(id) ? sel.filter((i) => i !== id) : [...sel, id]),
  } : null;

  const titles = {};
  data.tickets.forEach((t) => { titles[t.identifier] = t.title; });
  (data.candidates?.candidates || []).forEach((c) => { titles[c.identifier] ||= c.title; });
  (data.status.written_back || []).forEach((w) => { titles[w.identifier] ||= w.title; });

  const rows = Object.fromEntries(STAGES.map((s) => [s.key, []]));
  const aside = [];
  const skipped = Object.fromEntries((data.candidates?.skipped || []).map((x) => [x.identifier, x.reason]));
  data.tickets.forEach((t) => {
    t._s = ticketStatus(t, skipped);
    if (t.dispatch) return;  // shown on its dispatch's card list
    const stage = TICKET_STAGE[t._s.group];
    (stage ? rows[stage] : aside).push(<TicketRow key={t.identifier} t={t} pick={stage === "ready" ? pickFor(t.identifier) : null} />);
  });
  data.dispatches.forEach((d) => rows[DISPATCH_STAGE[d.state] || "working"].push(<DispatchRow key={d.run_id} d={d} titles={titles} onDone={load} />));
  (data.status.open_flags || []).forEach((f) => rows.writeback.unshift(<FlagRow key={`f${f.id}`} f={f} onDone={load} />));
  (data.status.archived || []).forEach((a) => rows.closed.push(<ArchivedRow key={a.run_id} a={a} titles={titles} />));
  (data.status.written_back || []).forEach((w) => rows.closed.push(<ClosedTicketRow key={`w${w.identifier}`} w={w} />));

  const flags = (data.status.open_flags || []).length;
  const drafts = data.dispatches.filter((d) => d.state === "draft" && d.review !== "planning").length;
  const needYou = flags + rows.answer.length + drafts;
  const byKey = Object.fromEntries(STAGES.map((s) => [s.key, s]));
  const visible = focus ? [byKey[focus]] : STAGES.filter((s) => rows[s.key].length);
  const idle = focus ? [] : STAGES.filter((s) => !rows[s.key].length);
  const stageExtra = (s) => s.key === "ready" && stageable.length
    ? <StageBar picked={sel} max={max} all={stageable} setPicked={setPicked} onDone={load} /> : null;

  return (
    <div className="fx">
      <header className="bar">
        <div>
          <div className={`summary${needYou ? " you" : ""}`}>
            {needYou ? `${plural(needYou, "thing")} need${needYou === 1 ? "s" : ""} you` : "Nothing needs you right now"}
          </div>
          <div className="sub">
            {[drafts && plural(drafts, "draft") + " to review", rows.answer.length && plural(rows.answer.length, "question"),
              flags && plural(flags, "held write")].filter(Boolean).join(" · ") || `${data.tickets.length} tickets in your domains`}
            {` · ${data.status.ignored_other_leads} in other leads' domains ignored`}
          </div>
        </div>
        <div className="bar-right">
          <Health jobs={data.jobs} />
          <Button variant="outline" size="sm" onClick={load} title="Refreshes every 30 seconds">↻ {loadedAt ? ago(loadedAt) : ""}</Button>
        </div>
      </header>
      {error ? <div className="err">Last refresh failed: {error}</div> : null}

      <nav className="pipeline" aria-label="Lifecycle stages">
        <button className={`stage ${focus ? "idle" : "all"}`} aria-pressed={!focus} onClick={() => setFocus(null)}>All</button>
        {FLOW.map((k) => (
          <button key={k} className={`stage ${rows[k].length ? byKey[k].tone : "idle"}${focus === k ? " focused" : ""}`} aria-pressed={focus === k}
                  onClick={() => setFocus(focus === k ? null : k)}>
            <span className="count">{rows[k].length}</span>{byKey[k].title}
          </button>
        ))}
      </nav>

      {visible.map((s) => <Section key={s.key} s={s} rows={rows[s.key]} extra={stageExtra(s)} />)}
      {idle.length ? <div className="idle-line">Nothing in {idle.map((s) => s.title.toLowerCase()).join(", ")}.</div> : null}

      <details className="fold" onToggle={(e) => setShowTp(e.currentTarget.open)}>
        <summary>Throughput, last 28 days</summary>
        {showTp ? <Throughput stamp={loadedAt} /> : null}
      </details>
      <details className="fold">
        <summary>{`Not for the factory (${aside.length}): already handled, held elsewhere, or not mapped`}</summary>
        <div className="list">{aside}</div>
      </details>
    </div>
  );
}

window.__HERMES_PLUGINS__.register("factory", FactoryPage);

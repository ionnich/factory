// Strategy: a supporting workspace (beside the lifecycle stages, at ?stage=strategy) that turns source tickets into
// approved, immutable, versioned work briefs (intent). Factory keeps verification, implementation planning and
// execution; Strategy keeps source grooming and human-approved intent, so a dispatch never reconstructs intent by
// re-reading a Linear narrative.
//
//   GET /strategy is a pure cached read: brief summaries (no body), the source list, execution policy, and active
//   dispatches. Selecting a brief GETs /strategy/{id} for its full body + captured sources + the compiled Markdown
//   render. Grooming runs real DeepSeek through the CLI (long), so it shows busy and any error truthfully — never a
//   fake placeholder. Editing persists a new draft revision (immutable versions; an amendment needs a reason).
//   Approval freezes intent for planning only; creating a draft dispatch is a separate readiness-gated action.
//   Holding/unholding is an explicit readiness change. Human dismissal requires confirmation and a reason, preserving
//   draft history as read-only. Automatic proposals enter human review; they never imply verification or approval.
//
// Layout: sources are the default landing surface, with grouped relationship-aware browsing first. The brief list is
// opened intentionally, and a selected brief replaces browsing until the operator returns to sources. Grouped mode
// uses the backend's typed relationship groups as a native collapsed outline, with Flat and desktop Dependency DAG
// alternatives.
//
// <StrategyTab data view onViewChange onDone onNavigate />: data is the overview (its identity changes on every
//   refresh, which re-fetches /strategy). view {q, picked, open, stateFilter, ctxFilter, assigneeFilter, sort,
//   sourceMode, expandedGroups, limit, busy, err} is the parent's (one, kept while unmounted): q = source search,
//   picked = source identifiers selected for grooming, stateFilter/ctxFilter/assigneeFilter = source filters,
//   sort = source order (priority by default), sourceMode = groups|flat|dag, expandedGroups = open group ids,
//   briefsOpen = whether the operator intentionally opened the brief index, limit = pagination, open = selected brief
//   id, busy/err = the in-flight action and its error. busy and err are live state, not location: leaving Strategy and
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
const exactTime = (iso) => new Date(iso).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const stop = (e) => e.stopPropagation();
// A source's calendar due date is exactly YYYY-MM-DD (never a timestamp), so it compares lexically and never shifts a
// day across a timezone parse. Only a real, valid calendar date (e.g. not 2026-02-30) counts; the original string is
// kept for display and sorting, never a locally-parsed date.
const DUE_RE = /^\d{4}-\d{2}-\d{2}$/;
const isDue = (d) => {
  if (typeof d !== "string" || !DUE_RE.test(d)) return false;
  const dt = new Date(`${d}T00:00:00Z`);  // UTC parse: no local-time shift; the roundtrip rejects impossible days
  return !Number.isNaN(dt.getTime()) && dt.toISOString().slice(0, 10) === d;
};
// A real timestamp (epoch number or parseable ISO string), else null: missing/invalid dates sort last in BOTH
// directions and are never guessed from fetched/updated.
const ts = (v) => {
  if (v == null || v === "") return null;
  const t = typeof v === "number" ? v : Date.parse(v);
  return Number.isFinite(t) ? t : null;
};
// Linear priority: 1 Urgent … 4 Low; 0/null/anything else is "No priority" and sorts after 4.
const PRIORITY = { 1: ["Urgent", "red"], 2: ["High", "amber"], 3: ["Normal", "blue"], 4: ["Low", "gray"] };
const priorityOf = (p) => PRIORITY[p] || ["No priority", "gray"];
const PRIORITY_RANK = { 1: 1, 2: 2, 3: 3, 4: 4 };
const pRank = (p) => PRIORITY_RANK[p] ?? 5;
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
// Null keys are missing/invalid dates and go last in both directions; real keys then compare ascending or descending.
const byAsc = (a, b) => (a == null && b == null ? 0 : a == null ? 1 : b == null ? -1 : cmp(a, b));
const byDesc = (a, b) => (a == null && b == null ? 0 : a == null ? 1 : b == null ? -1 : cmp(b, a));
// Stable, deterministic sort over an already-filtered copy. Filter first, sort second, paginate third.
function sortSources(rows, sort) {
  const key = (t) => ({
    rank: pRank(t.priority),
    due: isDue(t.due_date) ? t.due_date : null,
    created: ts(t.created_at),
    updated: ts(t.updated_at),
    id: t.identifier || "",
  });
  const order = {
    // Priority 1-4 first, then due soonest, then oldest created, then identifier (deterministic); No priority after 4.
    priority: (A, B) => cmp(A.rank, B.rank) || byAsc(A.due, B.due) || byAsc(A.created, B.created) || cmp(A.id, B.id),
    due: (A, B) => byAsc(A.due, B.due) || cmp(A.id, B.id),
    oldest: (A, B) => byAsc(A.created, B.created) || cmp(A.id, B.id),
    newest: (A, B) => byDesc(A.created, B.created) || cmp(A.id, B.id),
    updated: (A, B) => byDesc(A.updated, B.updated) || cmp(A.id, B.id),
  };
  return [...rows].sort((a, b) => (order[sort] || order.priority)(key(a), key(b)));
}

// Group kind: the backend enum is exactly parent/dependency/related/project/context. An unknown kind keeps a truthful
// fallback label (its raw kind), never an invented relationship. `type` is the single type/reason shown in a summary.
const GROUP_KIND = {
  parent: { type: "Parent family", detail: "parent/child links" },
  dependency: { type: "Dependency chain", detail: "blocking links" },
  related: { type: "Related candidate", detail: "one-hop related links" },
  project: { type: "Project bucket", detail: "organizational" },
  context: { type: "Context bucket", detail: "organizational" },
};
const GROUP_KINDS = new Set(Object.keys(GROUP_KIND));
function groupKind(g) {
  const k = g?.kind ?? "";
  return GROUP_KINDS.has(k) ? k : "unknown";
}
// A prerequisite is "open" (unresolved) only on an explicit Linear state type: backlog/unstarted/started. A missing
// status is unknown (never a block); completed/canceled is not open. Verification (verdict/stale) is never completion.
function isUnresolvedPrereq(t) {
  return !!(t && t.state_type && ["backlog", "unstarted", "started"].includes(t.state_type));
}
// ticket.reason (strategy.py _ticket_list) is a single readiness blocker: null = ready, "no verdict" = not checked,
// "verdict stale …" = outdated, anything else = not ready (completed, in QA, live dispatch, unmapped, no owner,
// assigned elsewhere, non-valid verdict). This is the truthful source for the summary counts.
function reasonState(t) {
  const r = t.reason;
  if (!r) return "ready";
  if (r === "no verdict") return "notchecked";
  if (r.startsWith("verdict stale")) return "outdated";
  return "notready";
}
// True when the recorded edges form a cycle (used to refuse pretending a cyclic graph is a DAG).
function hasCycle(edges) {
  const nodes = new Set();
  for (const e of edges) { nodes.add(e.source); nodes.add(e.target); }
  const adj = {}; const indeg = {};
  for (const n of nodes) { adj[n] = []; indeg[n] = 0; }
  for (const e of edges) { adj[e.source].push(e.target); indeg[e.target]++; }
  const q = [...nodes].filter((n) => indeg[n] === 0);
  let seen = 0;
  while (q.length) { const n = q.shift(); seen++; for (const m of adj[n]) if (--indeg[m] === 0) q.push(m); }
  return seen !== nodes.size;
}
// Parent family members as a depth-first outline (roots first, children indented); siblings keep the active sort order.
// Cycles are broken via a seen set (the backend's `cycles` surface them separately, never a fake tree).
function parentHierarchy(d) {
  const order = d.orderedMembers.map((t) => t.identifier);
  const memberSet = new Set(order);
  const idx = new Map(order.map((id, i) => [id, i]));
  const children = new Map();
  const hasParent = new Set();
  for (const e of d.parentEdges) {
    if (!memberSet.has(e.source) || !memberSet.has(e.target)) continue;
    if (!children.has(e.source)) children.set(e.source, []);
    children.get(e.source).push(e.target);
    hasParent.add(e.target);
  }
  for (const kids of children.values()) kids.sort((a, b) => (idx.get(a) ?? 0) - (idx.get(b) ?? 0));
  const roots = order.filter((id) => !hasParent.has(id));
  const out = [];
  const seen = new Set();
  const visit = (id, depth, parent) => {
    if (seen.has(id)) return;
    seen.add(id); out.push({ id, depth, parent });
    for (const c of children.get(id) || []) visit(c, depth + 1, id);
  };
  for (const id of roots) visit(id, 0, null);
  for (const id of order) if (!seen.has(id)) visit(id, 0, null);
  return out;
}
// Topological order of matching members by literal blocks edges (prerequisite before dependent), with the active sort
// as the tie-break among ready peers. Excluded members are traversed as intermediate nodes so true ordering survives
// filtering. Cycles fall back deterministically (lowest sort rank first) — no dropped or re-emitted members.
function dependencyOrder(matchingMembers, allMembers, blocks, sort) {
  const allIds = allMembers.map((t) => t.identifier);
  const idSet = new Set(allIds);
  const edges = blocks.filter((e) => idSet.has(e.source) && idSet.has(e.target));
  const adj = {}; const indeg = {};
  allIds.forEach((id) => { adj[id] = []; indeg[id] = 0; });
  for (const e of edges) { adj[e.source].push(e.target); indeg[e.target]++; }
  const byId = new Map(allMembers.map((t) => [t.identifier, t]));
  const rank = new Map(sortSources(allMembers, sort).map((t, i) => [t.identifier, i]));
  const matchingIds = new Set(matchingMembers.map((t) => t.identifier));
  const remaining = new Set(allIds);
  const deg = { ...indeg };
  const ready = allIds.filter((id) => deg[id] === 0).sort((a, b) => (rank.get(a) ?? 0) - (rank.get(b) ?? 0));
  const out = [];
  while (remaining.size) {
    let pick;
    if (ready.length) pick = ready.shift();
    else pick = [...remaining].sort((a, b) => (rank.get(a) ?? 0) - (rank.get(b) ?? 0))[0];
    remaining.delete(pick);
    if (matchingIds.has(pick)) out.push(byId.get(pick));
    for (const m of adj[pick]) if (--deg[m] === 0) ready.push(m);
    ready.sort((a, b) => (rank.get(a) ?? 0) - (rank.get(b) ?? 0));
  }
  return out;
}
const BADGE = { amber: "warning", green: "success", blue: "secondary", gray: "outline", red: "destructive" };
const Tone = ({ tone, children }) => <Badge tone={BADGE[tone] || "outline"}>{children}</Badge>;
const Ext = ({ href, children }) => <a className="fx-link" href={href} target="_blank" rel="noreferrer" onClick={stop}>{children}</a>;

const post = (path, body) => SDK.fetchJSON(API + path,
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const PAGE_FLAT = 50;    // flat-mode source rows per page
const PAGE_GROUPS = 12;  // grouped-mode groups per page

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

const STATE_TONE = { draft: "blue", approved: "green", held: "amber", dismissed: "gray" };
const stateLabel = (b) => (b.dismissal || b.readiness === "dismissed" ? "dismissed"
  : b.state === "approved" ? "approved" : b.state === "held" ? "held" : "draft");
const needsReview = (b) => stateLabel(b) === "draft" && b.readiness !== "superseded";
const DISPATCH_STAGE = { draft: "draft", staged: "run", executing: "run", done: "reconcile",
                         reconciled: "reconcile", archived: "archive" };
let NAV_TOKEN = 0;  // latest navigation generation; the parent bumps it synchronously on every move (go/Back)
export function bumpNavToken() { NAV_TOKEN += 1; }  // shared across remounts so a late reply sees the newest move

// One source ticket: compact and selectable. Readiness (`reason`) and validity are separate lines: a ticket can be
// not-ready for one reason and its verdict fresh or stale for another. `verdict` is the current verdict kind, `stale`
// its freshness signal (null or a reason; a missing verdict is never "outdated"), `verdict_at` when that verdict was
// made. Priority/created/updated/due are the source's own Linear facts, never the factory's fetch time.
function SourceRow({ s, checked, onToggle, depth = 0, cycle = false, parent = null }) {
  const [pLabel, pTone] = priorityOf(s.priority);
  return (
    <div className={`fx-trow${cycle ? " fx-cycle" : ""}`} data-depth={depth || undefined}
         style={depth ? { "--fx-depth": depth } : undefined}>
      <label className="fx-check-target">
        <input type="checkbox" className="fx-pick" checked={checked} onChange={onToggle}
               aria-label={`Select ${s.identifier}`} />
      </label>
      <div className="fx-grow">
        <div className="fx-row fx-row-title">
          <div className="fx-ttitle clamp">{s.title}</div>
          <span className="fx-row fx-row-status">
            {cycle ? <Tone tone="red">cycle</Tone> : null}
            <Tone tone={pTone}>{pLabel}</Tone>
            {s.state ? <Tone tone="gray">{s.state}</Tone> : null}
          </span>
        </div>
        {s.reason ? <div className="fx-hint">{s.reason}</div> : null}
        {!s.verdict ? <div className="fx-hint">Not checked</div>
          : s.stale ? <div className="fx-hint">Check outdated · {s.stale} · prior verdict {s.verdict}</div>
          : <div className="fx-hint">verdict {s.verdict}{s.verdict_at ? ` · checked ${ago(s.verdict_at)}` : ""}</div>}
        <div className="fx-row fx-row-meta">
          <Ext href={s.url}>{s.identifier}</Ext>
          {parent && depth > 3 ? <span className="fx-hint">child of {parent} · level {depth + 1}</span> : null}
          {s.repo ? <span className="fx-hint">{s.repo}</span> : null}
          {s.assignee ? <span className="fx-hint">{s.assignee}</span> : null}
          {s.created_at ? <span className="fx-hint" title={exactTime(s.created_at)}>Created {ago(s.created_at)}</span>
                        : <span className="fx-hint">Created unknown</span>}
          {s.updated_at ? <span className="fx-hint" title={exactTime(s.updated_at)}>Updated {ago(s.updated_at)}</span>
                        : <span className="fx-hint">Updated unknown</span>}
          {isDue(s.due_date) ? <span className="fx-hint">Due {s.due_date}</span> : null}
        </div>
      </div>
    </div>
  );
}

// One brief summary: state, title/revision, its readiness blockers, and its downstream dispatch (if dispatched).
function BriefRow({ b, selected, onSelect, onDispatch }) {
  const blockers = b.blockers || [];
  const relWarnings = b.relationship_warnings || [];
  const superseded = b.readiness === "superseded";
  return (
    <div id={`fx-brief-${b.id}`} className={`fx-trow${selected ? " picked" : ""}`} role="button" tabIndex={0}
         aria-current={selected ? "true" : undefined} onClick={onSelect}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { stop(e); onSelect(); } }}>
      <div className="fx-grow">
        <div className="fx-row fx-row-status">
          <Tone tone={STATE_TONE[stateLabel(b)] || "gray"}>{stateLabel(b)}</Tone>
          {superseded ? <Tone tone="gray">superseded</Tone> : null}
          {!b.dismissal && b.source_changed?.length ? <Tone tone="red">needs amendment</Tone> : null}
          {blockers.length ? <Tone tone="amber">{plural(blockers.length, "blocker")}</Tone> : null}
        </div>
        <div className="fx-row-title fx-ttitle clamp">{b.title || `Brief #${b.id}`}</div>
        <div className="fx-row-meta fx-hint">#{b.id} · revision {b.revision}{b.created_by ? ` · ${b.created_by}` : ""}
          {b.created_at ? ` · ${ago(b.created_at)}` : ""}
          {b.sources?.length ? ` · ${plural(b.sources.length, "source")}` : ""}</div>
        {b.created_by === "agent:brief-proposer" ? <div className="fx-hint">Auto-proposed by DeepSeek · {needsReview(b) ? "human review required" : stateLabel(b)}</div> : null}
        {b.dismissal ? <div className="fx-hint">Dismissed: {b.dismissal.reason}</div> : null}
        {!b.dismissal && blockers.length ? <div className="fx-hint">Open for grouped readiness details and recovery actions.</div> : null}
        {relWarnings.length ? <div className="fx-err">{clip(relWarnings.join("; "), 160)}</div> : null}
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

const READINESS_GROUPS = {
  completed: ["Completed sources", "Completed work cannot be dispatched again. Review a replacement without these sources."],
  canceled: ["Canceled sources", "Canceled work cannot be dispatched. Review a replacement without these sources."],
  "human-review": ["QA / human review", "These tickets are owned by the human review flow, not execution."],
  ownership: ["Ownership and routing", "Resolve assignment, domain mapping, or route ownership before including these sources."],
  source: ["Other source conflicts", "Resolve the current source conflict before including these tickets."],
};

function FactRows({ rows }) {
  return (
    <div className="fx-stack-v">
      {rows.map((row, i) => (
        <div className="fx-row fx-row-meta" key={`${row.identifier || "fact"}-${i}`}>
          {row.identifier ? <span className="fx-id">{row.identifier}</span> : null}
          <span>{row.reason || row.status}</span>
        </div>
      ))}
    </div>
  );
}

// Structured backend facts keep safety detail scannable without parsing or flattening refusal strings.
function ReadinessPanel({ summary }) {
  const facts = summary?.readiness_facts;
  if (!facts) return null;
  const sourceGroups = Object.entries(READINESS_GROUPS).map(([category, meta]) => ({
    category, meta, rows: (facts.sources || []).filter((row) => row.category === category),
  })).filter((group) => group.rows.length);
  const groups = [
    ...sourceGroups,
    facts.drift?.length ? { category: "drift", meta: ["Source drift", "Review the changed source, then amend or groom a replacement that captures current truth."], rows: facts.drift } : null,
    facts.dependencies?.length ? {
      category: "dependencies", meta: ["Unmet dependencies", "Complete the prerequisite or revise the brief's dependency decision."],
      rows: facts.dependencies.map((d) => ({ identifier: d.identifier, reason: d.status })),
    } : null,
    facts.held ? { category: "held", meta: ["Held", "Resolve the hold reason, then explicitly unhold. The approved brief remains intact."],
                   rows: [{ reason: facts.held }] } : null,
    facts.verification?.length ? {
      category: "verification", meta: ["Verification pending", "Factory must bind fresh valid evidence to this exact approved version before a draft dispatch can be created."],
      rows: facts.verification,
    } : null,
  ].filter(Boolean);
  if (!groups.length) return <div className="fx-hint">Ready to create a draft dispatch.</div>;
  return (
    <div className="fx-stack-v" role="region" aria-label="Dispatch readiness">
      <div className="fx-k">What blocks a draft dispatch</div>
      {groups.some((group) => group.category !== "verification") ? (
        <div className="fx-hint">Resolve these blockers first. Waiting for verification alone will not make this brief ready.</div>
      ) : null}
      {groups.map((group) => (
        <details className="fx-fold" key={group.category}>
          <summary>{group.meta[0]} ({group.rows.length})</summary>
          <div className="fx-hint">{group.meta[1]}</div>
          <FactRows rows={group.rows} />
        </details>
      ))}
    </div>
  );
}

function InvestigationPanel({ investigation, eligible, dirty, busy, onInvestigate, onOpenProposal }) {
  const status = investigation?.status || null;
  const active = status === "pending" || status === "running";
  const failed = status === "failed";
  const completed = status === "completed";
  const result = investigation?.result || null;
  const evidence = result?.evidence || [];
  const followups = result?.followups || [];
  const statusLabel = status === "pending" ? "queued"
    : status === "running" ? "investigating"
    : status === "completed" ? "completed"
    : status === "failed" ? "failed"
    : status;
  return (
    <section className="fx-sec fx-stack-v" aria-label="Agent blocker investigation">
      <div className="fx-row between">
        <div className="fx-k">Agent blocker investigation</div>
        {statusLabel ? <Tone tone={failed ? "red" : completed ? "green" : "amber"}>{statusLabel}</Tone> : null}
      </div>
      <div className="fx-hint">A read-only agent investigates blockers and proposes a corrected draft or human follow-up.
        It uses cached snapshots and repository mirrors, not live production checks. This brief and Linear stay unchanged.</div>
      {active ? (
        <div className="fx-hint" role="status">
          {status === "pending" ? "Investigation queued" : "Investigation in progress"}
          {investigation.requested_at ? ` · requested ${ago(investigation.requested_at)}` : ""}
        </div>
      ) : null}
      {failed ? (
        <div className="fx-err" role="alert">
          Investigation failed{investigation.error ? `: ${investigation.error}` : "."}
        </div>
      ) : null}
      {completed ? (
        <>
          {result?.summary
            ? <div className="fx-why">{result.summary}</div>
            : <div className="fx-err">Investigation completed without a result summary.</div>}
          {evidence.length ? (
            <details className="fx-fold">
              <summary>Evidence ({evidence.length})</summary>
              <ul className="fx-src">{evidence.map((item, i) => <li key={i}>{item}</li>)}</ul>
            </details>
          ) : null}
          {followups.length ? (
            <details className="fx-fold">
              <summary>Follow-up proposals ({followups.length})</summary>
              <div className="fx-stack-v">
                {followups.map((item, i) => (
                  <div key={i}>
                    <div className="fx-k">{item.title}</div>
                    <div className="fx-hint">{item.description}</div>
                  </div>
                ))}
              </div>
            </details>
          ) : null}
          {investigation.proposal_brief_id ? (
            <div className="fx-row">
              <Button size="sm" onClick={() => onOpenProposal(investigation.proposal_brief_id)}>
                Open proposed brief #{investigation.proposal_brief_id}
              </Button>
            </div>
          ) : <div className="fx-hint">No corrected draft was created.</div>}
        </>
      ) : null}
      {eligible && (!investigation || failed) ? (
        <div className="fx-row">
          <Button size="sm" disabled={!!busy || dirty} onClick={onInvestigate}>
            {busy === "investigate" ? "Starting…" : failed ? "Retry investigation" : "Investigate blockers"}
          </Button>
          {dirty ? <span className="fx-hint">Save or discard edits first.</span> : null}
        </div>
      ) : null}
    </section>
  );
}

function Field({ label, value, onChange, kind, disabled, readOnly = false }) {
  return (
    <label className="fx-field fx-brief-section">
      <span className="fx-k">{label}</span>
      {kind === "one"
        ? <Input value={value} maxLength={200} disabled={disabled} readOnly={readOnly} onChange={(e) => onChange(e.target.value)} />
        : <textarea className="fx-ta" rows={3} value={value} disabled={disabled} readOnly={readOnly}
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

// A closed group's summary stays scannable: title, relationship kind, matching size, nearest due date and the most
// relevant truthful readiness state. The complete priority/readiness/ownership picture is shown after expansion.
function GroupSummary({ d }) {
  const meta = GROUP_KIND[d.kind] || { type: String(d.g.kind || "Group"), detail: "unrecognized relationship kind" };
  const n = d.matchingMembers.length, m = d.memberTickets.length;
  const readiness = d.depBlocked ? `${d.depBlocked} dependency-blocked`
    : d.notReady ? `${d.notReady} not ready`
    : d.outdated ? `${d.outdated} outdated`
    : d.notChecked ? `${d.notChecked} not checked`
    : `${n} ready`;
  return (
    <div className="fx-group-summary">
      <div className="fx-group-head">
        <span className="fx-ttitle clamp">{d.g.title || d.g.id || "Untitled group"}</span>
      </div>
      <div className="fx-group-meta">
        <span>{meta.type}</span>
        <span>{n !== m ? `${n}/${m} sources` : plural(m, "source")}</span>
        <span>{readiness}</span>
        {d.earliestDue ? <span>Due {d.earliestDue}</span> : null}
        {d.g.continued ? <span>Continuation</span> : null}
      </div>
    </div>
  );
}

// One recorded link line: kind, source -> target, with titles and a read-only reason on any endpoint that is outside
// the current filters or outside the owned source scope. A blocks link marks an explicit unresolved prerequisite.
function EdgeLine({ e, nodeInfo, reasonOf }) {
  const s = nodeInfo(e.source), t = nodeInfo(e.target);
  const endpoint = (n, id) => (
    <>
      {n?.url ? <Ext href={n.url}>{id}</Ext> : <span className="fx-id">{id}</span>}
      {n?.title ? <span className="fx-hint">{clip(n.title, 48)}</span> : null}
      {reasonOf(id) ? <Tone tone={reasonOf(id) === "outside source scope" ? "amber" : "gray"}>{reasonOf(id)}</Tone> : null}
    </>
  );
  const open = e.kind === "blocks" && isUnresolvedPrereq(nodeInfo(e.source));
  const sep = e.kind === "related" ? "↔" : "→";
  return (
    <div className="fx-edge">
      <span className="fx-id">{e.kind}</span>
      {endpoint(s, e.source)}
      <span className="fx-hint">{sep}</span>
      {endpoint(t, e.target)}
      {open ? <Tone tone="amber">open prerequisite</Tone> : null}
    </div>
  );
}

// A native collapsed <details> whose contents render lazily only while open — keeps a group's 100+ recorded edges and
// context rows from being materialized until the operator asks for them.
function FxDetails({ summary, children }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="fx-edge-sec" open={open}
             onToggle={(e) => { if (e.target !== e.currentTarget) return; setOpen(e.target.open); }}>
      <summary>{summary}</summary>
      {open ? children : null}
    </details>
  );
}

// The group's typed recorded links as separate, labelled, collapsed sections (blocks/parent/related/duplicate + any
// unknown kinds). In DAG mode the blocks section is visualised above (desktop only); these lists stay the accessible
// form everywhere.
function EdgeSections({ d, nodeInfo, reasonOf }) {
  const sections = [
    ["blocks", "Dependency (blocks)", d.blocks],
    ["parent", "Parent", d.parentEdges],
    ["related", "Related", d.related],
    ["duplicate", "Duplicate", d.duplicate],
  ];
  const present = sections.filter(([, , es]) => es.length);
  const others = Object.keys(d.otherByKind || {}).sort();
  if (!present.length && !others.length) return null;
  return (
    <>
      {present.map(([k, label, es]) => (
        <FxDetails key={k} summary={<span className="fx-k">{label} ({es.length})</span>}>
          {es.map((e, i) => <EdgeLine key={`${k}-${e.source}-${e.target}-${i}`} e={e} nodeInfo={nodeInfo} reasonOf={reasonOf} />)}
        </FxDetails>
      ))}
      {others.map((k) => (
        <FxDetails key={`other-${k}`} summary={<span className="fx-k">{k} ({d.otherByKind[k].length})</span>}>
          {d.otherByKind[k].map((e, i) => <EdgeLine key={`${k}-${e.source}-${e.target}-${i}`} e={e} nodeInfo={nodeInfo} reasonOf={reasonOf} />)}
        </FxDetails>
      ))}
    </>
  );
}

// Read-only linked context: members filtered out by the current filters, plus linked endpoints (context nodes) that are
// not already shown as matching members — each with its reason (outside filters vs outside source scope).
function ReadOnlyContext({ d, nodeInfo, reasonOf }) {
  const rows = [];
  const seen = new Set();
  for (const t of d.excludedMembers) {
    if (seen.has(t.identifier)) continue;
    seen.add(t.identifier);
    rows.push({ id: t.identifier, n: t, reason: "outside filters" });
  }
  for (const n of d.contextNodes) {
    if (seen.has(n.identifier)) continue;
    seen.add(n.identifier);
    rows.push({ id: n.identifier, n: nodeInfo(n.identifier) || n, reason: reasonOf(n.identifier) || "linked endpoint" });
  }
  if (!rows.length) return null;
  return (
    <FxDetails summary={<span className="fx-k">Linked context & excluded (read-only) ({rows.length})</span>}>
      <div className="fx-ctx-list">
        {rows.map(({ id, n, reason }) => (
          <div className="fx-ctx-row" key={id}>
            {n?.url ? <Ext href={n.url}>{id}</Ext> : <span className="fx-id">{id}</span>}
            {n?.title ? <span className="fx-hint">{clip(n.title, 48)}</span> : null}
            {n?.state ? <span className="fx-hint">{n.state}</span> : null}
            {n?.assignee ? <span className="fx-hint">{n.assignee}</span> : null}
            {n?.repo ? <span className="fx-hint">{n.repo}</span> : n?.project?.name ? <span className="fx-hint">{n.project.name}</span> : null}
            <Tone tone={reason === "outside source scope" ? "amber" : "gray"}>{reason}</Tone>
          </div>
        ))}
      </div>
    </FxDetails>
  );
}

// A small layered dependency DAG over blocks edges only (desktop): prerequisites on the left, dependents on the right,
// every incoming arrow preserved (multiple parents allowed). Cycles are refused here (the caller shows an edge list).
function DagGraph({ gid, blocks, nodeTitle, statusOf }) {
  const ids = [];
  const idSet = new Set();
  for (const e of blocks) for (const id of [e.source, e.target]) if (!idSet.has(id)) { idSet.add(id); ids.push(id); }
  const adj = {}; const indeg = {};
  ids.forEach((n) => { adj[n] = []; indeg[n] = 0; });
  for (const e of blocks) { adj[e.source].push(e.target); indeg[e.target]++; }
  const layer = {}; ids.forEach((n) => { layer[n] = 0; });
  const deg = { ...indeg };
  const queue = ids.filter((n) => indeg[n] === 0);
  while (queue.length) { const n = queue.shift(); for (const m of adj[n]) { layer[m] = Math.max(layer[m], layer[n] + 1); if (--deg[m] === 0) queue.push(m); } }
  const maxLayer = ids.reduce((m, n) => Math.max(m, layer[n]), 0);
  const byLayer = {};
  ids.forEach((n) => { (byLayer[layer[n]] ||= []).push(n); });
  const maxRows = Math.max(...Object.values(byLayer).map((a) => a.length), 1);
  const MARGIN = 18, NODE_W = 96, NODE_H = 30, LAYER_GAP = 168, ROW_GAP = 44;
  const width = MARGIN * 2 + maxLayer * LAYER_GAP + NODE_W;
  const height = MARGIN * 2 + (maxRows - 1) * ROW_GAP + NODE_H;
  const pos = {};
  ids.forEach((n) => {
    const l = layer[n], arr = byLayer[l], i = arr.indexOf(n);
    pos[n] = { x: MARGIN + l * LAYER_GAP, y: MARGIN + (maxRows - arr.length) * ROW_GAP / 2 + i * ROW_GAP };
  });
  const mid = `fxdag-${String(gid).replace(/[^a-zA-Z0-9_-]/g, "_")}`;
  return (
    <div className="fx-dag" tabIndex={0} role="region" aria-label="Dependency DAG (blocks links)">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img"
           aria-label="Dependency DAG (blocks links)">
        <defs>
          <marker id={mid} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" className="fx-dag-arrow" />
          </marker>
        </defs>
        {blocks.map((e, i) => {
          const a = pos[e.source], b = pos[e.target];
          return <line key={i} className="fx-dag-edge" x1={a.x + NODE_W} y1={a.y + NODE_H / 2}
                       x2={b.x} y2={b.y + NODE_H / 2} markerEnd={`url(#${mid})`} />;
        })}
        {ids.map((n) => (
          <g key={n} className="fx-dag-node">
            <rect x={pos[n].x} y={pos[n].y} width={NODE_W} height={NODE_H} rx={6} className={`fx-dag-box ${statusOf(n)}`} />
            <text x={pos[n].x + NODE_W / 2} y={pos[n].y + NODE_H / 2} textAnchor="middle" dominantBaseline="central"
                  className="fx-dag-text">{n}</text>
            <title>{nodeTitle(n)}</title>
          </g>
        ))}
      </svg>
    </div>
  );
}

// One group card: a native collapsed <details>. Open, it lists matching members (parent families as a hierarchy),
// offers an explicit "Select matching", then the typed edge sections and the read-only linked context.
function GroupCard({ d, open, sourceMode, pickedSet, onPick, onSelectMatching, onToggle, nodeInfo, reasonOf, statusOf }) {
  const cycle = d.cycleIds.size > 0 || d.hasBlockCycle;
  return (
    <details className="fx-group" data-kind={d.g.kind} open={open}
             onToggle={(e) => { if (e.target !== e.currentTarget || e.target.open === open) return; onToggle(d.g.id, e.target.open); }}>
      <summary><GroupSummary d={d} /></summary>
      {open ? (
        <div className="fx-group-body">
          {cycle ? (
            <div className="fx-cycle-warn">
              {d.cycleIds.size ? `Cycle among: ${[...d.cycleIds].join(", ")}` : "Dependency links form a cycle"}. Shown as an
              edge list, not a DAG.
            </div>
          ) : null}
          <div className="fx-group-stats">
            <span className="fx-k">{(GROUP_KIND[d.kind] || { detail: "unrecognized relationship kind" }).detail}</span>
            <Tone tone={d.highestPriority[1]}>{d.highestPriority[0]}</Tone>
            {d.notReady ? <Tone tone="red">{d.notReady} not ready</Tone> : null}
            {d.linkedPrereq ? <Tone tone="gray">{d.linkedPrereq} linked prerequisite</Tone> : null}
            {d.depBlocked ? <Tone tone="amber">{d.depBlocked} dependency-blocked</Tone> : null}
            {d.notChecked ? <Tone tone="gray">{d.notChecked} not checked</Tone> : null}
            {d.outdated ? <Tone tone="gray">{d.outdated} outdated</Tone> : null}
            {d.assigneeCount > 1 ? <span className="fx-hint">{d.assigneeCount} assignees</span> : null}
            {d.repoCount > 1 ? <span className="fx-hint">{d.repoCount} repos</span> : null}
          </div>
          <div className="fx-group-sources-head">
            <span className="fx-k">{d.matchingMembers.length
              ? `${d.matchingMembers.length} matching / ${d.memberTickets.length} in group`
              : `No matching source · ${d.memberTickets.length} in group`}</span>
            {d.matchingMembers.length ? (
              <button className="fx-link-btn" onClick={() => onSelectMatching(d)}>
                Select matching ({d.matchingMembers.length})
              </button>
            ) : null}
          </div>
          {d.matchingMembers.length ? (
            <div className="fx-member-list">
              {d.kind === "parent"
                ? parentHierarchy(d).map(({ id, depth, parent }) => {
                    const t = nodeInfo(id);
                    return t ? <SourceRow key={id} s={t} depth={depth} parent={parent} cycle={d.cycleIds.has(id)}
                                          checked={pickedSet.has(id)} onToggle={() => onPick(id)} /> : null;
                  })
                : d.orderedMembers.map((t) => (
                    <SourceRow key={t.identifier} s={t} cycle={d.cycleIds.has(t.identifier)}
                               checked={pickedSet.has(t.identifier)} onToggle={() => onPick(t.identifier)} />
                  ))}
            </div>
          ) : null}
          {sourceMode === "dag" ? (
            d.blocks.length ? (
              cycle ? null : (
                <>
                  <DagGraph gid={d.g.id} blocks={d.blocks} nodeTitle={(id) => nodeInfo(id)?.title || id} statusOf={statusOf} />
                  <div className="fx-dag-phone">Dependency arrows are desktop-only; the readable edge list below shows the same links.</div>
                </>
              )
            ) : <div className="fx-hint">No recorded blocking links.</div>
          ) : null}
          <EdgeSections d={d} nodeInfo={nodeInfo} reasonOf={reasonOf} />
          <ReadOnlyContext d={d} nodeInfo={nodeInfo} reasonOf={reasonOf} />
        </div>
      ) : null}
    </details>
  );
}

// Relationship snapshot status: always shown while the source browser is open (even with no picks) so missing or
// incomplete relationship data is never mistaken for a known-empty graph.
function RelationshipStatus({ relationships, missingIds }) {
  if (!relationships) {
    return <div className="fx-err fx-rel-status">Relationship snapshot unavailable — links are unknown.</div>;
  }
  const incomplete = !relationships.complete;
  const nMissing = missingIds.size;
  return (
    <div className={`fx-rel-status${!incomplete && !nMissing ? " complete" : ""}`}>
      <div className="fx-hint">Links {relationships.observed_at ? `observed ${ago(relationships.observed_at)}` : "not yet observed"}</div>
      {incomplete ? <div className="fx-err">Relationships incomplete — some recorded links may be missing.</div> : null}
      {nMissing ? <div className="fx-hint">{nMissing} source{nMissing === 1 ? "" : "s"} without a relationship snapshot</div> : null}
    </div>
  );
}

// The relationship review, shown before grooming: each picked source's recorded links (typed, read-only), including
// endpoints that are filtered out or outside the owned source scope. A picked id with no relationships object, outside
// any group, or in the missing list is explicitly unknown — never "No recorded links".
function RelationshipReview({ picked, memberGroupId, groupById, missingIds, relationships, nodeInfo, reasonOf }) {
  const hasRelationships = !!relationships;
  return (
    <div className="fx-rel-review">
      <div className="fx-k">Relationship review — before grooming</div>
      {picked.map((id) => {
        const gid = memberGroupId[id];
        const g = gid != null ? groupById[gid] : null;
        const edges = g ? (g.edges || []).filter((e) => e.source === id || e.target === id)
                        .map((e) => ({ kind: e.kind, source: e.source, target: e.target })) : [];
        const n = nodeInfo(id);
        const unknown = !hasRelationships || g == null || missingIds.has(id);
        return (
          <div key={id} className="fx-rel-item">
            <div className="fx-rel-head">
              {n?.url ? <Ext href={n.url}>{id}</Ext> : <span className="fx-id">{id}</span>}
              <span className="fx-ttitle clamp">{n?.title || ""}</span>
              {unknown ? <Tone tone="amber">relations unknown</Tone> : null}
              {reasonOf(id) ? <Tone tone="gray">{reasonOf(id)}</Tone> : null}
            </div>
            {edges.length ? (
              <div className="fx-rel-edges">
                {edges.map((e, i) => <EdgeLine key={`${id}-${e.kind}-${e.source}-${e.target}-${i}`} e={e} nodeInfo={nodeInfo} reasonOf={reasonOf} />)}
              </div>
            ) : <div className="fx-hint">{unknown ? "Relations unknown (no snapshot, outside a group, or not captured)." : "No recorded links."}</div>}
          </div>
        );
      })}
    </div>
  );
}

// ---- Domain grooming: a read-only DeepSeek domain review + explicit human-approved dispositions -----------------
// A review analyses one owned canonical domain's open tickets against cached source snapshots and repository mirrors.
// It proposes a minimum system, consumers, correctness notes, a few complexity cuts, and a per-ticket disposition —
// but changes nothing. Approving here queues the EXACT reviewed ticket edits into the existing reconcile writeback
// run; the actual Linear mutations happen later in reconcile cron, never in this UI, and only for tickets you select.
const GROOM_STATUS = {
  pending: ["queued", "amber"], running: ["reviewing", "blue"],
  completed: ["completed", "green"], failed: ["failed", "red"],
};
const GROOM_ACTION = {
  keep: ["Keep", "gray", false], rewrite: ["Rewrite", "blue", true], merge: ["Merge", "amber", true],
  close: ["Cancel", "red", true], investigate: ["Investigate", "amber", false],
};
const groomAction = (a) => GROOM_ACTION[a] || [a || "unknown", "gray", false];
const groomMutates = (t) => groomAction(t.action)[2];

// Recursive grooming: a review is one human-facing pass (manual, the default) or an agent-led bounded loop (agentic).
// Its outcome is null while pending, else ready (substantiated), blocked (stopped on missing data — never substituted
// with cached facts), or limit_reached (its bounded pass budget was consumed before substantiation).
const GROOM_OUTCOME = {
  ready: ["ready", "green"],
  blocked: ["blocked", "amber"],
  limit_reached: ["limit reached", "amber"],
};
// A single pass's own outcome is a narrower enum: ready, blocked, or evidence (gathered read-only witness receipts).
const GROOM_ROUND_OUTCOME = {
  ready: ["ready", "green"],
  blocked: ["blocked", "amber"],
  evidence: ["evidence", "blue"],
};

// The outcome badge: hidden for null, and in compact rows for the default "ready" so stopped outcomes stand out.
const OutcomeTone = ({ outcome, showReady = false, map = GROOM_OUTCOME }) => {
  const oc = outcome ? map[outcome] || [outcome, "gray"] : null;
  if (!oc || (!showReady && outcome === "ready")) return null;
  return <Tone tone={oc[1]}>{oc[0]}</Tone>;
};

// Witness receipts are the backend's truthfully-mapped read-only witness output: {id,name,query,at,ok,result,error}.
// An ok receipt shows its result; a failed one shows its error. Nothing is synthesized here. Contract: round.receipts.
const roundReceipts = (round) => (round?.receipts || []);

function WitnessReceipts({ receipts }) {
  if (!receipts || !receipts.length) return null;
  return (
    <details className="fx-fold">
      <summary>Witness evidence ({receipts.length})</summary>
      <ul className="fx-src">
        {receipts.map((w, i) => {
          const ok = w && w.ok === true;
          const failed = w && (w.ok === false || !!w.error);
          return (
            <li key={i}>
              <div className="fx-row fx-row-status">
                <Tone tone={ok ? "green" : failed ? "red" : "amber"}>{ok ? "ok" : failed ? "error" : "unchecked"}</Tone>
                {w.name ? <span className="fx-hint">{w.name}</span> : null}
                {w.id != null ? <span className="fx-id">{w.id}</span> : null}
                {w.at ? <span className="fx-hint" title={exactTime(w.at)}>{localTime(w.at)}</span> : null}
              </div>
              {w.query ? <div className="fx-hint">query: {clip(w.query, 200)}</div> : null}
              {w.error ? <div className="fx-err">{clip(String(w.error), 240)}</div> : null}
              {ok && w.result != null
                ? <div className="fx-hint">result: {clip(typeof w.result === "string" ? w.result : JSON.stringify(w.result), 240)}</div>
                : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

// A read-only, folded view of a pass's retained candidate review (round.review): the validated proposal that pass
// recorded. It is never the human-facing result — no approval or selection lives here.
function CandidateTicket({ t }) {
  const [label, tone] = groomAction(t.action);
  return (
    <div className="fx-groom-ticket">
      <div className="fx-row fx-row-status">
        <Tone tone={tone}>{label}</Tone>
        {t.action === "merge" && t.target ? <Tone tone="amber">into {t.target}</Tone> : null}
        <span className="fx-id">{t.identifier}</span>
      </div>
      {t.reason ? <div className="fx-hint">{t.reason}</div> : null}
      {t.action === "rewrite" && t.title != null
        ? <div className="fx-hint">Title: {t.title || "(unchanged)"}{t.description ? ` · ${clip(t.description, 100)}` : ""}</div>
        : null}
      {t.evidence?.length ? (
        <details className="fx-fold"><summary>Evidence ({t.evidence.length})</summary>
          <ul className="fx-src">{t.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      ) : null}
    </div>
  );
}

function CandidateReview({ review }) {
  if (!review) return null;
  const tickets = review.tickets || [];
  const cuts = review.cuts || [];
  const cutById = Object.fromEntries(cuts.map((c) => [c.id, c]));
  const primaryCutOf = (t) => (t.cut_ids || []).find((id) => cutById[id]) || null;
  const ungrouped = tickets.filter((t) => primaryCutOf(t) == null);
  const groups = cuts.map((c) => ({ cut: c, tickets: tickets.filter((t) => primaryCutOf(t) === c.id) }));
  return (
    <details className="fx-fold">
      <summary>Recorded candidate proposal ({tickets.length} disposition{tickets.length === 1 ? "" : "s"}
        {cuts.length ? ` · ${cuts.length} cut${cuts.length === 1 ? "" : "s"}` : ""})</summary>
      <div className="fx-stack-v">
        {review.minimum_system ? <div className="fx-why">{review.minimum_system}</div> : null}
        {review.consumers?.length ? (
          <details className="fx-fold"><summary>Consumers ({review.consumers.length})</summary>
            <ul className="fx-src">{review.consumers.map((c, i) => <li key={i}>{c}</li>)}</ul>
          </details>
        ) : null}
        {review.correctness?.length ? (
          <details className="fx-fold"><summary>Correctness ({review.correctness.length})</summary>
            <ul className="fx-src">{review.correctness.map((c, i) => <li key={i}>{c}</li>)}</ul>
          </details>
        ) : null}
        {groups.map((g) => (
          <div key={g.cut.id} className="fx-stack-v">
            <div className="fx-hint"><strong>{g.cut.title || g.cut.id}</strong>{g.cut.reason ? ` — ${g.cut.reason}` : ""}</div>
            {g.cut.evidence?.length ? (
              <details className="fx-fold"><summary>Evidence ({g.cut.evidence.length})</summary>
                <ul className="fx-src">{g.cut.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </details>
            ) : null}
            {g.cut.risk ? <div className="fx-hint">Risk: {g.cut.risk}</div> : null}
            {g.cut.migration ? <div className="fx-hint">Migration: {g.cut.migration}</div> : null}
            {g.tickets.map((t) => <CandidateTicket key={t.identifier} t={t} />)}
          </div>
        ))}
        {ungrouped.map((t) => <CandidateTicket key={t.identifier} t={t} />)}
        {review.limitations?.length ? (
          <details className="fx-fold"><summary>Limitations ({review.limitations.length})</summary>
            <ul className="fx-src">{review.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
          </details>
        ) : null}
      </div>
    </details>
  );
}

// One recorded pass: its number, truthful outcome, the model's assessment text, its witness receipts, and (for a ready
// pass) its retained candidate proposal.
function RoundPass({ round }) {
  return (
    <section className="fx-sec fx-stack-v">
      <div className="fx-row fx-row-title">
        <span className="fx-k">Round {round?.number ?? "?"}</span>
        <OutcomeTone outcome={round?.outcome} showReady map={GROOM_ROUND_OUTCOME} />
        {round?.completed_at ? <span className="fx-hint">{localTime(round.completed_at)}</span> : null}
      </div>
      {round?.assessment ? <div className="fx-why">{round.assessment}</div> : null}
      <WitnessReceipts receipts={roundReceipts(round)} />
      <CandidateReview review={round?.review} />
    </section>
  );
}

// The status/meta summary shared by the review list row and the read-only lineage list.
function ReviewSummaryMeta({ r }) {
  const [label, tone] = GROOM_STATUS[r.status] || [r.status || "unknown", "gray"];
  return (
    <>
      <div className="fx-row fx-row-status">
        <Tone tone={tone}>{label}</Tone>
        {r.mode === "agentic" ? <Tone tone="blue">agent-led</Tone> : null}
        <OutcomeTone outcome={r.outcome} />
        {r.superseded_by != null ? <Tone tone="gray">superseded</Tone> : null}
        {r.approved_at ? <Tone tone="green">approved {ago(r.approved_at)}</Tone> : null}
        {r.proposal_brief_id ? <Tone tone="blue">brief #{r.proposal_brief_id}</Tone> : null}
      </div>
      <div className="fx-row-title fx-ttitle clamp">{r.domain_name || r.domain_id || `Review #${r.id}`}</div>
      <div className="fx-row-meta fx-hint">#{r.id} · requested {ago(r.requested_at)}
        {r.completed_at ? ` · completed ${ago(r.completed_at)}` : ""}{r.run_id ? ` · run ${r.run_id}` : ""}
        {r.round_count ? ` · ${r.round_count} round${r.round_count === 1 ? "" : "s"}` : ""}
        {r.parent_review_id ? ` · child of #${r.parent_review_id}` : ""}</div>
      {r.feedback ? <div className="fx-hint">“{clip(r.feedback, 140)}”</div> : null}
      {r.error ? <div className="fx-err">{clip(r.error, 160)}</div> : null}
      {r.blocker ? <div className="fx-hint">{r.blocker}</div> : null}
    </>
  );
}

// Manual (one human-facing revision, default) vs agent-led (bounded automatic critique/evidence/revision passes).
function ModeToggle({ mode, onChange, disabled, label }) {
  const manual = mode !== "agentic";
  return (
    <div className="fx-seg" role="radiogroup" aria-label={label || "Review mode"}>
      <button type="button" role="radio" aria-checked={manual} className={manual ? "on" : ""}
              disabled={disabled} onClick={() => onChange("manual")}>Manual</button>
      <button type="button" role="radio" aria-checked={!manual} className={manual ? "" : "on"}
              disabled={disabled} onClick={() => onChange("agentic")}>Agent-led</button>
    </div>
  );
}

function DomainReviewRow({ r, busy, onOpen, onRetry }) {
  return (
    <div className="fx-trow" role="button" tabIndex={0}
         onClick={onOpen}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { stop(e); onOpen(); } }}>
      <div className="fx-grow">
        <ReviewSummaryMeta r={r} />
      </div>
      <div className="fx-tc-ne">
        {r.status === "failed"
          ? <Button size="sm" disabled={!!busy} onClick={(e) => { stop(e); onRetry(); }}>Retry</Button>
          : <span className="fx-hint">›</span>}
      </div>
    </div>
  );
}

function DomainGroomSection({ domains, reviews, listErr, loading, busy, domainId, goal, mode, onDomain, onGoal, onMode, onStart, onOpen, onRetry }) {
  const sel = domainId || "";
  const sorted = [...reviews].sort((a, b) => (ts(b.requested_at) ?? 0) - (ts(a.requested_at) ?? 0));
  const active = reviews.some((r) => r.status === "pending" || r.status === "running");
  return (
    <section className="fx-sec fx-stack-v" aria-label="Groom domain">
      <div className="fx-k">Groom domain</div>
      <div className="fx-hint">A read-only DeepSeek review of one owned domain's open tickets against cached snapshots and
        repository mirrors. It proposes a minimum system and per-ticket dispositions — it changes no code and no ticket.
        Approving a change queues it into reconcile; the actual Linear edit happens later in reconcile cron.</div>
      <div className="fx-row">
        <select className="fx-select" value={sel} aria-label="Domain"
                onChange={(e) => onDomain(e.target.value)}>
          {!sel ? <option value="">Choose a domain…</option> : null}
          {domains.map((d) => (
            <option key={d.id} value={d.id}>{d.name} · {d.open_count ?? 0}/{d.ticket_count ?? 0} open</option>
          ))}
        </select>
        <Input value={goal || ""} placeholder="Optional focus/goal" maxLength={200} aria-label="Review goal"
               onChange={(e) => onGoal(e.target.value)} />
      </div>
      <div className="fx-row">
        <Button size="sm" disabled={!!busy || !sel} onClick={() => onStart(sel, goal)}>
          {busy === "domain-start" ? "Starting…" : "Start review"}
        </Button>
        <ModeToggle mode={mode} onChange={onMode} disabled={!!busy} label="Review mode" />
        {mode === "agentic" ? <span className="fx-hint">Agent-led: up to 3 automatic passes.</span> : null}
        {active ? <span className="fx-hint">A review is running; this list refreshes itself.</span> : null}
      </div>
      {listErr ? <div className="fx-err" role="alert">Domain reviews unavailable: {listErr}</div> : null}
      {loading
        ? <div className="fx-hint">Loading domain reviews…</div>
        : reviews.length
          ? <div className="fx-k">Past reviews ({reviews.length})</div>
          : <div className="fx-empty">No domain reviews yet.</div>}
      <div className="fx-list">
        {sorted.map((r) => <DomainReviewRow key={r.id} r={r} busy={busy} onOpen={() => onOpen(r.id)} onRetry={() => onRetry(r)} />)}
      </div>
    </section>
  );
}

function GroomTicket({ t, ctx, sel, disabled, onToggle }) {
  const [label, tone, mutate] = groomAction(t.action);
  const blocked = ctx?.mutable === false;
  const target = t.target || null;
  const isRewrite = t.action === "rewrite";
  const isMerge = t.action === "merge";
  const isClose = t.action === "close";
  const showDiff = isRewrite || isMerge || isClose;
  const oldTitle = ctx?.title || "";
  const oldDesc = ctx?.description || "";
  const resTitle = t.title ?? oldTitle;  // null means unchanged: the resulting ticket keeps the original captured field
  const resDesc = t.description ?? oldDesc;
  const checked = mutate && sel.has(t.identifier);
  return (
    <div className="fx-groom-ticket">
      <div className="fx-row fx-row-title">
        {mutate ? (
          <label className="fx-check-target">
            <input type="checkbox" className="fx-pick" aria-label={`Select ${t.identifier}`}
                   checked={checked} disabled={disabled || blocked} onChange={onToggle} />
          </label>
        ) : <span className="fx-check-target fx-groom-noop" aria-hidden="true">–</span>}
        <div className="fx-grow">
          <div className="fx-row fx-row-status">
            <Tone tone={tone}>{label}</Tone>
            {isMerge && target ? <Tone tone="amber">into {target}</Tone> : null}
            {blocked ? <Tone tone="red">not mutable</Tone> : null}
            {ctx?.state ? <Tone tone="gray">{ctx.state}</Tone> : null}
          </div>
          <div className="fx-row-meta">
            <span className="fx-id">{t.identifier}</span>
            {ctx?.title ? <span className="fx-hint">{clip(ctx.title, 60)}</span> : null}
            {ctx?.assignee ? <span className="fx-hint">{ctx.assignee}</span> : null}
            {ctx?.snapshot_updated_at ? <span className="fx-hint">snapshot {localTime(ctx.snapshot_updated_at)}</span> : null}
          </div>
          {t.reason ? <div className="fx-hint">{t.reason}</div> : null}
          {blocked && ctx?.blocker ? <div className="fx-err">{ctx.blocker}</div> : null}
          {t.evidence?.length ? (
            <details className="fx-fold"><summary>Evidence ({t.evidence.length})</summary>
              <ul className="fx-src">{t.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
            </details>
          ) : null}
          {showDiff ? (
            <details className="fx-fold">
              <summary>Before → after</summary>
              <div className="fx-diff">
                {oldTitle || oldDesc ? (
                  <>
                    {oldTitle ? <div className="fx-diff-line fx-diff-old">− Title: {oldTitle}</div> : null}
                    {oldDesc ? <div className="fx-diff-line fx-diff-old">− Description: {oldDesc}</div> : null}
                  </>
                ) : <div className="fx-diff-line fx-diff-old">− (no recorded title or description)</div>}
                {isRewrite ? (
                  <>
                    <div className="fx-diff-line fx-diff-new">+ Title: {resTitle || "(none)"}</div>
                    <div className="fx-diff-line fx-diff-new">+ Description: {resDesc || "(none)"}</div>
                  </>
                ) : null}
                {isMerge ? (
                  <>
                    <div className="fx-diff-line fx-diff-new">+ canceled (state → Canceled)</div>
                    <div className="fx-diff-line fx-diff-new">+ comment: points to retained target {target || "(retained target)"}</div>
                  </>
                ) : null}
                {isClose ? (
                  <div className="fx-diff-line fx-diff-new">+ canceled — this ticket's title and description are unchanged; Linear
                    marks it canceled</div>
                ) : null}
              </div>
            </details>
          ) : null}
          {t.cut_ids?.length ? <div className="fx-hint">cuts: {t.cut_ids.join(", ")}</div> : null}
        </div>
      </div>
    </div>
  );
}

function GroomCut({ cut, tickets, ctxById, sel, setSel, disabled }) {
  const selectable = tickets.filter((t) => groomMutates(t) && ctxById[t.identifier]?.mutable !== false);
  const allOn = selectable.length > 0 && selectable.every((t) => sel.has(t.identifier));
  const toggleCut = () => {
    const next = new Set(sel);
    selectable.forEach((t) => (allOn ? next.delete(t.identifier) : next.add(t.identifier)));
    setSel(next);
  };
  const toggle = (id) => {
    const next = new Set(sel);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSel(next);
  };
  return (
    <section className="fx-groom-cut">
      <div className="fx-groom-cut-head">
        <span className="fx-groom-cut-title">{cut.title || cut.id || "Scope decision"}</span>
        {selectable.length ? (
          <label className="fx-check-target fx-groom-cut-check">
            <input type="checkbox" className="fx-pick" checked={allOn} disabled={disabled} onChange={toggleCut}
                   aria-label={`Select all changes in ${cut.title || cut.id}`} />
            <span className="fx-hint">Select changes ({selectable.length})</span>
          </label>
        ) : null}
      </div>
      <div className="fx-groom-cut-body">
        {cut.reason ? <div className="fx-hint">{cut.reason}</div> : null}
        {cut.evidence?.length ? (
          <details className="fx-fold"><summary>Evidence ({cut.evidence.length})</summary>
            <ul className="fx-src">{cut.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
          </details>
        ) : null}
        {cut.risk ? <div className="fx-hint">Risk: {cut.risk}</div> : null}
        {cut.migration ? <div className="fx-hint">Migration: {cut.migration}</div> : null}
        {tickets.length ? (
          <div className="fx-member-list">
            {tickets.map((t) => (
              <GroomTicket key={t.identifier} t={t} ctx={ctxById[t.identifier]} sel={sel} disabled={disabled}
                           onToggle={() => toggle(t.identifier)} />
            ))}
          </div>
        ) : <div className="fx-hint">No ticket changes for this scope decision.</div>}
      </div>
    </section>
  );
}

function WritebackList({ rows }) {
  return (
    <section className="fx-sec fx-stack-v" aria-label="Reconcile writebacks">
      <div className="fx-k">{plural(rows.length, "Linear write")} from this review</div>
      <ul className="fx-writes">{rows.map((w, i) => {
        const held = w.decision === "flag";
        return (
          <li key={i} className={`w-${held ? "held" : w.status}`}>
            <span className="mark">{held ? "⏸" : w.status === "confirmed" ? "✓" : w.status === "failed" ? "✕" : "…"}</span>
            {w.identifier || "?"} · {w.op || "change"} · {held ? "held" : w.status || "queued"}
            {w.reason ? <span className="fx-hint"> · {w.reason}</span> : null}
          </li>
        );
      })}</ul>
    </section>
  );
}

function DomainReviewDetail({ r, busy, sel, setSel, armed, onBack, onApprove, onDisarm, onBrief, onOpenBrief,
                              mode, feedback, onFeedback, onMode, onRerun, onOpenReview }) {
  const [label, tone] = GROOM_STATUS[r.status] || [r.status || "unknown", "gray"];
  const active = r.status === "pending" || r.status === "running";
  const completed = r.status === "completed";
  const supersededBy = r.superseded_by ?? null;
  const superseded = supersededBy != null;
  const result = r.result || null;
  // v2 history is root -> latest and includes the current row; lineage shows only true earlier rounds so the current
  // review is never offered a misleading self-open.
  const history = (r.history || []).filter((p) => p.id !== r.id);
  const rounds = r.rounds || [];
  const tickets = result?.tickets || [];
  const cuts = result?.cuts || [];
  const ctxTickets = r.context?.tickets || [];
  const ctxById = Object.fromEntries(ctxTickets.map((t) => [t.identifier, t]));
  const cutById = Object.fromEntries(cuts.map((c) => [c.id, c]));
  const writebacks = r.writebacks || [];
  const approved = !!r.approved_at;
  const locked = approved || superseded || !!busy;  // frozen once approved/superseded, or while a mutation write is in flight
  const primaryCutOf = (t) => (t.cut_ids || []).find((id) => cutById[id]) || null;
  const groups = cuts.map((c) => ({ cut: c, tickets: tickets.filter((t) => primaryCutOf(t) === c.id) }));
  const ungrouped = tickets.filter((t) => primaryCutOf(t) == null);
  const mutableTickets = tickets.filter(groomMutates);
  const selTickets = tickets.filter((t) => sel.has(t.identifier) && groomMutates(t));
  const counts = { rewrite: 0, merge: 0, close: 0 };
  selTickets.forEach((t) => { counts[t.action] = (counts[t.action] || 0) + 1; });
  const countParts = [];
  if (counts.rewrite) countParts.push(`${counts.rewrite} rewrite${counts.rewrite === 1 ? "" : "s"}`);
  if (counts.merge) countParts.push(`${counts.merge} merge${counts.merge === 1 ? "" : "s"}`);
  if (counts.close) countParts.push(`${counts.close} cancel${counts.close === 1 ? "" : "s"}`);
  const toggle = (id) => {
    const next = new Set(sel);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSel(next);
  };
  return (
    <section className="fx-stack-v fx-groom" aria-label={`Domain review #${r.id}`}>
      <div className="fx-row between">
        <div className="fx-row fx-groom-head">
          <span className="fx-id">#{r.id}</span>
          <span className="fx-ttitle">{r.domain_name || r.domain_id || "Domain review"}</span>
          <Tone tone={tone}>{label}</Tone>
          {r.mode === "agentic" ? <Tone tone="blue">agent-led</Tone> : <Tone tone="gray">manual</Tone>}
          <OutcomeTone outcome={r.outcome} showReady />
          {approved ? <Tone tone="green">approved {ago(r.approved_at)}</Tone> : null}
        </div>
        <Button size="sm" ghost onClick={onBack}>← Back to sources</Button>
      </div>
      <div className="fx-hint">Requested {ago(r.requested_at)}{r.completed_at ? ` · completed ${ago(r.completed_at)}` : ""}{r.run_id ? ` · run ${r.run_id}` : ""}
        {r.round_count ? ` · ${r.round_count} round${r.round_count === 1 ? "" : "s"}` : ""}
        {r.parent_review_id ? ` · child of #${r.parent_review_id}` : ""}</div>
      {superseded ? (
        <>
          <div className="fx-err" role="status">Superseded — a newer review round (#{supersededBy}) exists. This round is
            read-only for new approvals and simplification briefs; its already queued writes remain visible below.</div>
          <div className="fx-row">
            <Button size="sm" ghost onClick={() => onOpenReview(supersededBy)}>Open newer review #{supersededBy} ›</Button>
          </div>
        </>
      ) : null}
      {r.feedback ? <div className="fx-why">Comment: {r.feedback}</div> : null}
      {r.outcome === "blocked" ? <div className="fx-hint">Stopped on missing data — unavailable evidence is shown below, never substituted with cached facts.</div> : null}
      {r.outcome === "limit_reached" ? <div className="fx-hint">Evidence budget (max 3 agent-led passes) consumed before the review was substantiated; request another round to continue.</div> : null}
      {active ? (
        <div className="fx-hint" role="status">{r.status === "pending" ? "Queued" : "Reviewing"} — a read-only analysis over cached
          snapshots and mirrors. It changes nothing; this refreshes itself.</div>
      ) : null}
      {r.status === "failed" ? <div className="fx-err" role="alert">Review failed{r.error ? `: ${r.error}` : "."}</div> : null}
      {r.blocker ? <div className="fx-hint">{r.blocker}</div> : null}

      {history.length ? (
        <section className="fx-sec fx-stack-v" aria-label="Review lineage">
          <div className="fx-k">Lineage ({history.length} earlier round{history.length === 1 ? "" : "s"})</div>
          <div className="fx-list">
            {history.map((p) => (
              <div key={p.id} className="fx-trow">
                <div className="fx-grow"><ReviewSummaryMeta r={p} /></div>
                <div className="fx-tc-ne">
                  <Button size="sm" ghost onClick={() => onOpenReview(p.id)} aria-label={`Open review #${p.id}`}>Open review ›</Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      {rounds.length ? (
        <section className="fx-sec fx-stack-v" aria-label="Review rounds">
          <div className="fx-k">Rounds ({rounds.length})</div>
          {rounds.map((rd, i) => <RoundPass key={i} round={rd} />)}
        </section>
      ) : null}

      {result ? (
        <>
          <div className="fx-k">Minimum system</div>
          <div className="fx-why">{result.minimum_system || "No summary recorded."}</div>
          {result.consumers?.length ? (
            <details className="fx-fold"><summary>Consumers ({result.consumers.length})</summary>
              <ul className="fx-src">{result.consumers.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </details>
          ) : null}
          {result.correctness?.length ? (
            <details className="fx-fold"><summary>Correctness ({result.correctness.length})</summary>
              <ul className="fx-src">{result.correctness.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </details>
          ) : null}
          {cuts.length ? (
            <>
              <div className="fx-k">Scope decisions ({cuts.length})</div>
              {groups.map((g) => (
                <GroomCut key={g.cut.id} cut={g.cut} tickets={g.tickets} ctxById={ctxById} sel={sel}
                          setSel={setSel} disabled={locked} />
              ))}
            </>
          ) : null}
          {ungrouped.length ? (
            <div className="fx-groom-ungrouped">
              <div className="fx-k">Dispositions without a specific cut ({ungrouped.length})</div>
              {ungrouped.map((t) => (
                <GroomTicket key={t.identifier} t={t} ctx={ctxById[t.identifier]} sel={sel}
                             disabled={locked} onToggle={() => toggle(t.identifier)} />
              ))}
            </div>
          ) : null}
          {!cuts.length && !tickets.length ? <div className="fx-empty">No scope decisions or dispositions recorded.</div> : null}
          {!mutableTickets.length && tickets.length ? (
            <div className="fx-hint">No ticket edits proposed — keep/investigate dispositions require no Linear change.</div>
          ) : null}
          {result.limitations?.length ? (
            <details className="fx-fold" open>
              <summary>Recorded-evidence limitations ({result.limitations.length})</summary>
              <ul className="fx-src">{result.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
            </details>
          ) : null}
        </>
      ) : active ? null
        : r.outcome === "blocked" && r.error ? <div className="fx-why">{r.error}</div>
        : <div className="fx-err">No result recorded.</div>}

      {writebacks.length ? <WritebackList rows={writebacks} /> : null}

      {result?.simplification && !r.proposal_brief_id && !superseded ? (
        <section className="fx-sec fx-stack-v">
          <div className="fx-k">Simplification brief</div>
          <div className="fx-hint">Draft an unapproved code-simplification brief from this review's retained sources. It is
            never staged or executed automatically; create it, then review it in the normal brief flow. Reconcile applies
            the approved ticket changes first; a stale review or pending writes refuse this draft.</div>
          <div className="fx-row">
            <Button size="sm" disabled={!!busy} onClick={onBrief}>
              {busy === "domain-brief" ? "Creating…" : "Create simplification brief"}
            </Button>
          </div>
        </section>
      ) : null}
      {r.proposal_brief_id ? (
        <div className="fx-row">
          <Button size="sm" ghost onClick={() => onOpenBrief(r.proposal_brief_id)}>
            Open simplification brief #{r.proposal_brief_id} ›
          </Button>
        </div>
      ) : null}

      {completed ? (
        <section className="fx-sec fx-stack-v" aria-label="Request another round">
          <div className="fx-k">Request another round</div>
          <div className="fx-hint">Leave a comment and ask for a revised review. Manual runs one human-facing revision;
            agent-led runs up to 3 bounded automatic critique/evidence/revision passes that stop when substantiated or
            blocked on missing data. The parent result and your comment feed the next review.</div>
          <ModeToggle mode={mode} onChange={onMode} disabled={!!busy} label="Next round mode" />
          <textarea className="fx-ta" rows={3} maxLength={4000} value={feedback} disabled={!!busy}
                    aria-label="Review comment" placeholder="Comment for the next review (optional)"
                    onChange={(e) => onFeedback(e.target.value)} />
          <div className="fx-row">
            <Button size="sm" disabled={!!busy} onClick={onRerun}>
              {busy === "domain-rerun" ? "Requesting…" : "Request next round"}
            </Button>
            {mode === "agentic"
              ? <span className="fx-hint">Agent-led: up to 3 automatic passes over read-only witnesses; may stop blocked or at the pass limit.</span>
              : <span className="fx-hint">Manual: one revised review.</span>}
          </div>
        </section>
      ) : null}

      {!approved && !superseded && result && mutableTickets.length ? (
        <section className="fx-sec fx-stack-v">
          {!armed ? (
            <div className="fx-row">
              <Button size="sm" disabled={!!busy || !sel.size} onClick={onApprove}>
                {busy === "domain-approve" ? "Queuing…" : `Approve ticket changes${sel.size ? ` (${sel.size})` : ""}`}
              </Button>
              {!sel.size ? <span className="fx-hint">Select at least one change above to approve.</span> : null}
            </div>
          ) : (
            <div className="fx-sw">
              <div className="fx-sw-q">Queue these {sel.size} ticket change(s) for reconcile?</div>
              <div className="fx-sw-detail">This queues the exact reviewed edits — {countParts.join(", ") || "selected changes"} —
                into the existing reconcile writeback run. Reconcile cron then edits Linear titles/descriptions and cancels
                tickets, subject to fresh ownership and eligibility checks. Nothing is edited here.</div>
              <div className="fx-row">
                <Button size="sm" disabled={!!busy} onClick={onApprove}>{busy === "domain-approve" ? "Queuing…" : `Confirm ticket changes (${sel.size})`}</Button>
                <Button size="sm" ghost disabled={!!busy} onClick={onDisarm}>Cancel</Button>
              </div>
            </div>
          )}
        </section>
      ) : approved ? (
        <div className="fx-why">Approved {ago(r.approved_at)} — changes queued for reconcile. Watch Reconcile for the applied
          results; held or failed writes surface there too.</div>
      ) : null}
    </section>
  );
}

export function StrategyTab({ data, view, onViewChange, onDone, onNavigate }) {
  const q = view?.q || "", picked = view?.picked || [], open = view?.open || null;
  const stateFilter = view?.stateFilter || "all", ctxFilter = view?.ctxFilter || "all",
        assigneeFilter = view?.assigneeFilter || "all";
  const sort = view?.sort || "priority";
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
  const pendingBriefs = briefs.filter(needsReview);
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
  }, [open, summary?.revision, summary?.state, summary?.dismissal?.at, mut]);
  const current = detail?.brief || null;
  const dismissal = current?.dismissal || summary?.dismissal;
  const dismissed = !!dismissal || summary?.readiness === "dismissed";

  // The editable form, reset when the open brief's version changes (a new revision is its own row).
  const [edit, setEdit] = useState(null);
  const [reason, setReason] = useState("");
  const [arm, setArm] = useState(null);  // the weighty action awaiting its second tap: approve|dispatch|amend|hold|dismiss
  useEffect(() => { setEdit(current ? toForm(current.body) : null); setReason(""); setArm(null); },
            [current?.id, current?.revision, dismissal?.at]);  // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = !!current && !!edit && canonical(edit) !== canonical(toForm(current.body || {}));
  const shownForm = dismissed && current ? toForm(current.body) : edit;
  const canCreateDispatch = current?.state === "approved" && summary?.readiness === "ready" && !dirty;
  const dispatchWhy = dirty ? "Save or discard edits first."
    : summary?.readiness === "verification-pending" ? "Exact-version verification is pending."
    : summary?.readiness === "needs-amendment" ? "Review source drift and amend or groom a replacement."
    : summary?.readiness === "blocked" ? "Resolve the grouped blockers or review replacement sources."
    : summary?.readiness === "held" ? "Resolve the hold reason and explicitly unhold."
    : summary?.readiness === "dispatched" ? "This brief already has a dispatch."
    : summary?.readiness === "superseded" ? "Use the current approved revision."
    : !summary ? "Readiness is still loading." : null;
  const investigation = summary?.investigation || null;
  const investigationEligible = !!summary && ["approved", "held"].includes(current?.state) &&
    !summary.dispatch && summary.readiness !== "superseded" && (summary.blockers || []).length > 0;

  // ---- Domain grooming: one read-only review per owned domain, human-approved dispositions ----------------------
  // The open review id and the start form's domain/goal live in the parent view so they survive leaving Strategy.
  const openReview = view?.domainReview || null;
  const groomDomain = view?.groomDomain || null;
  const groomGoal = view?.groomGoal || "";
  const groomMode = view?.groomMode === "agentic" ? "agentic" : "manual";
  const [groomList, setGroomList] = useState(null);   // {domains, reviews} from GET /strategy/domain-reviews
  const [groomListErr, setGroomListErr] = useState(null);
  const [groomDetail, setGroomDetail] = useState(null);
  const [groomDetailErr, setGroomDetailErr] = useState(null);
  const [groomMut, setGroomMut] = useState(0);        // bump to refetch list + detail after a write
  const [groomSel, setGroomSel] = useState(() => new Set());  // selected mutation identifiers
  const [groomArm, setGroomArm] = useState(false);    // the two-tap approve's second tap
  const [groomFeedback, setGroomFeedback] = useState("");  // the next-round comment, tied to the open review
  const openReviewRef = useRef(null);
  useEffect(() => { openReviewRef.current = openReview; }, [openReview]);
  const lastReview = useRef(null);

  // The domain-review list is a pure cached read, refetched on mount and on every overview refresh — the parent's
  // /stream refresh changes `data`, so a durable job's completion and later reconcile writebacks appear without any
  // polling loop here.
  useEffect(() => {
    let live = true;
    SDK.fetchJSON(`${API}/strategy/domain-reviews`)
      .then((x) => { if (live) { setGroomList(x); setGroomListErr(null); } },
            (e) => { if (live) setGroomListErr(errText(e)); });
    return () => { live = false; };
  }, [data, groomMut]);

  // The open review's full detail (context + result + writebacks), refetched on selection/actions and on every
  // overview refresh (`data`) so approved writebacks refresh after reconcile too. Switching reviews drops the old
  // detail and resets the selection/arm so a late reply is rejected.
  useEffect(() => {
    if (lastReview.current !== openReview) {
      lastReview.current = openReview;
      setGroomDetail(null); setGroomDetailErr(null);
      setGroomSel(new Set()); setGroomArm(false);
      setGroomFeedback("");  // a comment never carries across to a different review
    }
    if (openReview == null) return;
    let live = true;
    SDK.fetchJSON(`${API}/strategy/domain-reviews/${openReview}`)
      .then((x) => { if (live) { setGroomDetail(x); setGroomDetailErr(null); } },
            (e) => { if (live) setGroomDetailErr(errText(e)); });
    return () => { live = false; };
  }, [openReview, data, groomMut]);

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

  // Editing any field (title, outcome, resources, …) disarms an armed approval so the human always re-confirms the
  // values actually shown; nothing is auto-saved or auto-approved.
  const editField = (key, v) => { setEdit((e) => ({ ...e, [key]: v })); setArm(null); };

  const groom = async () => {
    const submitted = [...picked];
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call("/strategy/groom", { identifiers: submitted }, "groom");
    if (!r) return;
    // Remove only the identifiers this request submitted; picks made since are left alone. Spread `s` so the rest of
    // the view (open brief, filters, sort, source mode, expansions, pagination) is preserved.
    onViewChange((s) => ({ ...s, picked: (s.picked || []).filter((i) => !submitted.includes(i)),
                           replacementReview: null }));
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Groomed a draft brief #${r.id} from ${submitted.length} source${submitted.length === 1 ? "" : "s"}`);
  };

  const save = async () => {
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "edited draft" }, "save");
    if (!r) return;
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Saved draft #${r.id}`);
  };

  const approveBrief = async () => {
    if (arm !== "approve") { setArm("approve"); return; }
    setArm(null);
    const atOpen = open, atNav = NAV_TOKEN;
    let target = current;
    if (dirty) {  // approve the exact edited version: persist it, then approve it
      target = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() || "approval" }, "approve");
      if (!target) return;
    }
    const approved = await call(`/strategy/${target.id}/approve`, {}, "approve");
    if (!approved) return;
    if (applyGuarded(approved, atNav)) settleId(approved, atOpen);
    onDone(approved, null, `Approved brief #${approved.id} for planning (not execution)`);
  };

  const amend = async () => {  // approved/held -> new draft revision; the reason is required
    if (arm !== "amend") { setArm("amend"); return; }
    if (!reason.trim()) return;
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/revise`, { body: formToBody(edit), reason: reason.trim() }, "amend");
    if (!r) return;
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Amendment #${r.id} drafted; review and approve`);
  };

  const hold = async () => {
    if (arm !== "hold") { setArm("hold"); return; }
    if (!reason.trim()) return;
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/hold`, { reason: reason.trim() }, "hold");
    if (!r) return;
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Held #${open}`);
  };

  const dismiss = async () => {
    if (dismissed || current?.state !== "draft" || summary?.readiness === "superseded") return;
    if (arm !== "dismiss") { setReason(""); setArm("dismiss"); return; }
    if (!reason.trim()) return;
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/dismiss`, { reason: reason.trim() }, "dismiss");
    if (!r) return;
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Dismissed draft #${open}; history preserved`);
  };

  const unhold = async () => {
    const atOpen = open, atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/unhold`, {}, "unhold");
    if (!r) return;
    if (applyGuarded(r, atNav)) settleId(r, atOpen);
    onDone(r, null, `Unheld #${open}`);
  };

  const alive = useRef(true);  // a late reply never steals navigation: dispatch creation moves only while Strategy is open
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const createDispatch = async () => {
    if (arm !== "dispatch") { setArm("dispatch"); return; }
    setArm(null);
    const atNav = NAV_TOKEN;
    const r = await call(`/strategy/${open}/stage`, {}, "dispatch");
    if (!r) return;
    onDone(r, null, `Created draft dispatch ${r.run_id || ""} for review`);
    // Redirect only if the operator is still on this brief (nav token) and the tab is still mounted (lifecycle).
    if (r.run_id && alive.current && NAV_TOKEN === atNav) onNavigate({ stage: "draft", run: r.run_id });
  };

  const investigate = async () => {
    const briefId = current.id;
    const r = await call(`/strategy/${briefId}/investigate`, {}, "investigate");
    if (!r) return;
    // POST returns the durable job immediately. Patch only its parent summary; SSE refresh supplies later worker states.
    setAll((prev) => prev ? {
      ...prev,
      briefs: (prev.briefs || []).map((b) => b.id === briefId ? { ...b, investigation: r } : b),
    } : prev);
  };

  const reviewReplacement = () => {
    const replacement = summary?.replacement || { eligible: [], excluded: [] };
    onViewChange((v) => ({ ...v, briefsOpen: false, picked: [...replacement.eligible],
      replacementReview: { briefId: current.id, excluded: replacement.excluded,
                           eligible: replacement.eligible } }));
    onNavigate({ stage: "strategy", brief: null });
  };

  const refresh = async () => {
    const r = await call("/strategy/refresh", {}, "refresh");
    if (r) onDone(r, null, "Sources refreshed");
  };

  // ---- domain grooming actions ---------------------------------------------------------------------------------
  // A reply is applied only if the operator is still on the same review (navigation generation) and the same review
  // id; switching reviews resets the detail + selection, so a late reply can never mutate the wrong review.
  const groomStillHere = (atReview, atNav) => NAV_TOKEN === atNav && openReviewRef.current === atReview;

  // Any selection change re-arms the confirmation, so a confirm never applies values the operator didn't just see.
  const changeGroomSel = (next) => { setGroomSel(next); setGroomArm(false); };

  const startReview = async (domainId, goal) => {
    const atNav = NAV_TOKEN, atReview = openReview;
    const r = await call("/strategy/domain-reviews", { domain_id: domainId, goal: (goal || "").trim(), mode: groomMode }, "domain-start");
    if (!r) return;
    setGroomMut((m) => m + 1);  // refresh the list in the background regardless of where the operator is now
    // Move into the new review only if the operator is still where they started it (mounted + same navigation + same
    // review context); a background result must never hijack a newly selected view.
    if (alive.current && NAV_TOKEN === atNav && openReviewRef.current === atReview) {
      update({ domainReview: r.id, open: null, briefsOpen: false });
    }
    onDone(r, null, `Started domain review #${r.id}`);
  };

  const retryReview = async (rv) => {
    const atNav = NAV_TOKEN, atReview = openReview;
    const r = await call("/strategy/domain-reviews", { domain_id: rv.domain_id, goal: "" }, "domain-start");
    if (!r) return;
    setGroomMut((m) => m + 1);
    if (alive.current && NAV_TOKEN === atNav && openReviewRef.current === atReview) {
      update({ domainReview: r.id, open: null, briefsOpen: false });
    }
    onDone(r, null, `Restarted domain review #${r.id}`);
  };

  // A child round: the parent review id links lineage, its original goal is preserved, and the comment + mode drive the
  // next prompt. A failed reply keeps the comment and mode for another attempt; a late reply never hijacks navigation.
  const rerunReview = async () => {
    const atNav = NAV_TOKEN, atReview = openReview;
    const r = await call("/strategy/domain-reviews", {
      domain_id: groomDetail?.domain_id,
      goal: (groomDetail?.context?.goal || "").trim(),
      mode: groomMode,
      parent_review_id: openReview,
      feedback: groomFeedback.trim().slice(0, 4000),
    }, "domain-rerun");
    if (!r) return;
    setGroomMut((m) => m + 1);
    if (alive.current && NAV_TOKEN === atNav && openReviewRef.current === atReview) {
      update({ domainReview: r.id, open: null, briefsOpen: false });
    }
    onDone(r, null, `Started ${groomMode === "agentic" ? "agent-led" : "manual"} review round #${r.id}`);
  };

  const approveReview = async () => {
    if (!groomArm) { setGroomArm(true); return; }
    setGroomArm(false);
    const atReview = openReview, atNav = NAV_TOKEN;
    const ids = [...groomSel];
    const r = await call(`/strategy/domain-reviews/${openReview}/approve`, { identifiers: ids }, "domain-approve");
    if (!r) return;
    if (!groomStillHere(atReview, atNav)) return;
    setGroomDetail(r);
    setGroomSel(new Set());
    setGroomMut((m) => m + 1);
    onDone(r, null, `Queued ${ids.length} ticket change${ids.length === 1 ? "" : "s"} for reconcile`);
  };

  const createGroomBrief = async () => {
    const atReview = openReview, atNav = NAV_TOKEN;
    const r = await call(`/strategy/domain-reviews/${openReview}/brief`, {}, "domain-brief");
    if (!r) return;
    if (!groomStillHere(atReview, atNav)) return;
    setGroomMut((m) => m + 1);
    onDone(r, null, `Created simplification brief draft #${r.brief?.id ?? ""}`);
  };

  const openGroomReview = (id) => update({ domainReview: id, open: null, briefsOpen: false });
  const backFromGroom = () => update({ domainReview: null });
  const openGroomBrief = (id) => { update({ domainReview: null }); onNavigate({ stage: "strategy", brief: id }); };
  const setGroomDomain = (id) => update({ groomDomain: id });
  const setGroomGoal = (goal) => update({ groomGoal: goal });
  const setGroomMode = (mode) => update({ groomMode: mode === "agentic" ? "agentic" : "manual" });

  // ---- Sources: grouped-first browsing over the backend's recorded relationship groups -------------------------
  // `groups` (GET /strategy) are the backend's stable, typed link clusters — parent families, dependency chains,
  // one-hop related candidates, and organizational project/context buckets. Each selectable source belongs to exactly
  // one group; a group's edges/context are recorded links (read-only), never model-inferred here.
  const groups = all?.groups || [];
  const relationships = all?.relationships || null;  // {complete, observed_at, missing:[identifier]}
  const sourceMode = view?.sourceMode === "flat" ? "flat" : view?.sourceMode === "dag" ? "dag" : "groups";
  const expandedGroups = view?.expandedGroups || [];
  const briefsOpen = !!view?.briefsOpen;
  const replacementReview = view?.replacementReview || null;
  const limit = view?.limit || (sourceMode === "flat" ? PAGE_FLAT : PAGE_GROUPS);

  const needle = q.trim().toLowerCase();
  const states = useMemo(() => [...new Set(tickets.map((t) => t.state).filter(Boolean))].sort(), [tickets]);
  const contexts = useMemo(() => [...new Set(tickets.map((t) => t.context).filter(Boolean))].sort(), [tickets]);
  // Distinct non-empty assignee emails from the full source list (never the filtered one), sorted for the dropdown.
  const assignees = useMemo(() => [...new Set(tickets.map((t) => t.assignee).filter(Boolean))].sort(), [tickets]);
  // "Assigned to me" = a non-null/non-empty assignee equal to the ticket's lead; "unassigned" = null or empty.
  const assigneeMatch = (t) => assigneeFilter === "all" ||
    (assigneeFilter === "me" ? t.assignee != null && t.lead != null && t.assignee === t.lead :
     assigneeFilter === "unassigned" ? !t.assignee : t.assignee === assigneeFilter);
  const inFilters = (t) => (stateFilter === "all" || t.state === stateFilter) &&
    (ctxFilter === "all" || t.context === ctxFilter) && assigneeMatch(t);
  const matches = (t) => inFilters(t) && (!needle || `${t.identifier} ${t.title || ""}`.toLowerCase().includes(needle));
  const list = tickets.filter(matches);  // matching = state/context/assignee filters + search
  // Filter first, sort second, paginate third; sorting copies `list` so `tickets` is never mutated.
  const sorted = sortSources(list, sort);
  const matching = list.length;
  const total = tickets.length;
  const filteredActive = !!needle || stateFilter !== "all" || ctxFilter !== "all" || assigneeFilter !== "all";
  const pick = (id) => onViewChange((v) => {
    const cur = v.picked || [];
    return { ...v, picked: cur.includes(id) ? cur.filter((i) => i !== id) : [...cur, id] };
  });

  const ticketById = useMemo(() => Object.fromEntries(tickets.map((t) => [t.identifier, t])), [tickets]);
  const groupById = useMemo(() => Object.fromEntries(groups.map((g) => [g.id, g])), [groups]);
  const rankById = useMemo(() => { const m = {}; sorted.forEach((t, i) => { m[t.identifier] = i; }); return m; }, [sorted]);
  const contextById = useMemo(() => {
    const m = {};
    for (const g of groups) for (const n of g.context || []) if (n && n.identifier && !(n.identifier in m)) m[n.identifier] = n;
    return m;
  }, [groups]);
  const memberGroupId = useMemo(() => {
    const m = {};
    for (const g of groups) for (const id of g.members || []) if (!(id in m)) m[id] = g.id;
    return m;
  }, [groups]);
  const missingIds = useMemo(() => new Set(relationships?.missing || []), [relationships]);
  const pickedSet = useMemo(() => new Set(picked), [picked]);
  // A display record: cached context metadata merged under the ticket. Ticket fields (including authoritative nulls)
  // win over context; context-only fields (state_type, project) survive when the ticket lacks them entirely.
  const nodeInfo = (id) => {
    const t = ticketById[id], c = contextById[id];
    if (!t && !c) return null;
    return { ...(c || {}), ...(t || {}) };
  };
  const reasonOf = (id) => {
    const t = ticketById[id];
    if (!t) return "outside source scope";
    return matches(t) ? null : "outside filters";
  };
  const statusOf = (id) => {
    const t = ticketById[id];
    if (!t) return "ext";
    return matches(t) ? "in" : "off";
  };

  // Group order follows the existing sorted source order: a group ranks by its first (best) matching member; groups
  // with no matching member follow last, in stable backend order (deterministic id tie-break).
  const orderedGroups = [...groups].sort((a, b) => {
    const key = (g) => { let r = Infinity; for (const id of g.members || []) { const rk = rankById[id]; if (rk != null && rk < r) r = rk; } return r; };
    const ra = key(a), rb = key(b);
    if (ra === Infinity && rb === Infinity) return cmp(a.id || "", b.id || "");
    if (ra === Infinity) return 1;
    if (rb === Infinity) return -1;
    return ra - rb || cmp(a.id || "", b.id || "");
  });

  // Per-group figures over MATCHING members only (context is never counted as selectable work).
  const groupData = orderedGroups.map((g) => {
    const kind = groupKind(g);
    const memberTickets = (g.members || []).map((id) => ticketById[id]).filter(Boolean);
    const matchingMembers = memberTickets.filter(matches);
    const edges = g.edges || [];  // exact edge kinds preserved; only literal "blocks" is a dependency link
    const blocks = edges.filter((e) => e.kind === "blocks");
    const parentEdges = edges.filter((e) => e.kind === "parent");
    const related = edges.filter((e) => e.kind === "related");
    const duplicate = edges.filter((e) => e.kind === "duplicate");
    const otherByKind = {};
    for (const e of edges) if (!["blocks", "parent", "related", "duplicate"].includes(e.kind)) (otherByKind[e.kind] ||= []).push(e);
    // Dependency chains keep the actual prerequisite order (topological by blocks edges, excluded members as transit
    // nodes); every other group uses the active sort. Ready peers in a chain keep the active sort as their tie-break.
    const orderedMembers = kind === "dependency"
      ? dependencyOrder(matchingMembers, memberTickets, blocks, sort)
      : sortSources(matchingMembers, sort);
    const mm = matchingMembers;
    // Readiness: not-checked/outdated come from verdict/stale directly (validity, independent of reasonState); not-ready
    // is the remaining readiness reason from ticket.reason (completed is not ready, never dependency-blocked).
    const notChecked = mm.filter((t) => !t.verdict).length;
    const outdated = mm.filter((t) => !!t.verdict && !!t.stale).length;
    const notReady = mm.filter((t) => reasonState(t) === "notready").length;
    const highestPriority = priorityOf(mm.reduce((best, t) => Math.min(best, pRank(t.priority)), 5));
    const dueDates = mm.map((t) => (isDue(t.due_date) ? t.due_date : null)).filter(Boolean).sort();
    const earliestDue = dueDates[0] || null;
    const assigneeCount = new Set(mm.map((t) => t.assignee).filter(Boolean)).size;
    const repoCount = new Set(mm.map((t) => t.repo).filter(Boolean)).size;
    // Linked prerequisites: any incoming blocks edge (any status). Dependency-blocked: an incoming blocks edge whose
    // prerequisite is open (state_type backlog/unstarted/started). Never derived from verdict/stale.
    const linkedPrereq = mm.filter((t) => blocks.some((e) => e.target === t.identifier)).length;
    const depBlocked = mm.filter((t) => blocks.some((e) => e.target === t.identifier && isUnresolvedPrereq(nodeInfo(e.source)))).length;
    const excludedMembers = memberTickets.filter((t) => !matches(t));
    const contextNodes = (g.context || []).filter((n) => n && n.identifier && !matchingMembers.some((t) => t.identifier === n.identifier));
    const cycleIds = new Set(g.cycles || []);
    const hasBlockCycle = hasCycle(blocks);
    return { g, kind, memberTickets, matchingMembers, orderedMembers, edges, blocks, parentEdges, related, duplicate,
             otherByKind, notChecked, outdated, notReady, linkedPrereq, depBlocked, highestPriority, earliestDue,
             assigneeCount, repoCount, excludedMembers, contextNodes, cycleIds, hasBlockCycle };
  });

  // Picked sources hidden by the current filters/search (kept intact, never silently dropped).
  const hiddenPicked = picked.filter((id) => !(id in rankById)).length;
  // Grouped modes only paginate groups that actually match; excluded linked members stay read-only inside them.
  const visibleGroups = groupData.filter((d) => d.matchingMembers.length > 0);
  // Pagination: `limit` counts matching rows in flat mode, matching groups in grouped modes. It lives in the parent
  // view (retained across remounts); only an explicit filter/mode/sort change resets it.
  const shown = sorted.slice(0, limit);
  const shownGroups = visibleGroups.slice(0, limit);
  const shownCount = sourceMode === "flat" ? shown.length : shownGroups.length;
  const pageTotal = sourceMode === "flat" ? matching : visibleGroups.length;
  const moreCount = pageTotal - shownCount;

  const pageDefault = (mode) => (mode === "flat" ? PAGE_FLAT : PAGE_GROUPS);
  const changeFilter = (patch) => update({ ...patch, limit: pageDefault(patch.sourceMode ?? sourceMode) });
  const activeFilters = [
    needle ? `Search “${q.trim()}”` : null,
    stateFilter !== "all" ? `State: ${stateFilter}` : null,
    ctxFilter !== "all" ? `Context: ${ctxFilter}` : null,
    assigneeFilter !== "all"
      ? `Assignee: ${assigneeFilter === "me" ? "me" : assigneeFilter === "unassigned" ? "unassigned" : assigneeFilter}`
      : null,
  ].filter(Boolean);
  const clearFilters = () => changeFilter({
    q: "", stateFilter: "all", ctxFilter: "all", assigneeFilter: "all",
  });

  const setExpanded = (id, open) => onViewChange((v) => {
    const cur = v.expandedGroups || [];
    return { ...v, expandedGroups: open ? [...new Set([...cur, id])] : cur.filter((x) => x !== id) };
  });
  const selectGroupMatching = (d) => onViewChange((v) => {
    const cur = v.picked || [];
    const add = d.matchingMembers.map((t) => t.identifier).filter((id) => !cur.includes(id));
    return add.length ? { ...v, picked: [...cur, ...add] } : v;
  });

  const openBrief = (id) => onNavigate({ stage: "strategy", brief: id });
  const showSources = () => {
    update({ briefsOpen: false, domainReview: null });
    if (open != null) onNavigate({ stage: "strategy", brief: null });
  };
  const showBriefs = () => {
    update({ briefsOpen: true, domainReview: null });
    if (open != null) onNavigate({ stage: "strategy", brief: null });
  };
  const openDispatch = (d) => onNavigate({ stage: d.phase || DISPATCH_STAGE[d.state] || "draft", run: d.run_id });

  return (
    <div className="fx-stack-v fx-strategy">
      <header className="fx-strategy-heading">
        <div>
          <h2>Strategy</h2>
          <div className="fx-hint">Choose work worth doing.</div>
        </div>
        {briefsOpen && open == null
          ? <Button size="sm" ghost onClick={showSources}>Back to sources</Button>
          : <Button size="sm" ghost onClick={showBriefs}>View briefs <span className="fx-count">{briefs.length}</span>{pendingBriefs.length ? ` · ${pendingBriefs.length} to review` : ""}</Button>}
      </header>

      {loadErr ? <div className="fx-err" role="alert">{all ? `Refreshing strategy failed: ${loadErr}. Showing the last loaded.` : `Strategy did not load: ${loadErr}`}</div> : null}
      {busy ? <div className="fx-hint" role="status">
        {busy === "groom" ? "Grooming with DeepSeek (this takes a while)…"
          : busy === "investigate" ? "Starting blocker investigation…"
          : busy === "domain-start" ? "Starting domain review…"
          : busy === "domain-rerun" ? "Requesting next review round…"
          : busy === "domain-approve" ? "Queueing ticket changes…"
          : busy === "domain-brief" ? "Creating simplification brief…" : "Working…"}
      </div> : null}
      {err ? <div className="fx-err" role="alert">{err}</div> : null}

      {openReview != null ? (
        !groomDetail && !groomDetailErr ? <div className="fx-hint">Loading domain review #{openReview}…</div>
        : groomDetailErr ? (
          <div className="fx-err" role="alert">Domain review #{openReview} did not load: {groomDetailErr}
            <div className="fx-row"><Button size="sm" ghost onClick={backFromGroom}>← Back to sources</Button></div>
          </div>
        ) : (
          <DomainReviewDetail r={groomDetail} busy={busy} sel={groomSel} setSel={changeGroomSel}
                              armed={groomArm} onBack={backFromGroom} onApprove={approveReview}
                              onDisarm={() => setGroomArm(false)} onBrief={createGroomBrief} onOpenBrief={openGroomBrief}
                              onOpenReview={openGroomReview} mode={groomMode} feedback={groomFeedback}
                              onFeedback={setGroomFeedback} onMode={setGroomMode} onRerun={rerunReview} />
        )
      ) : open != null ? (
        <>
          <div className="fx-row">
            <Button size="sm" ghost onClick={showSources}>← Back to sources</Button>
          </div>
          {!current && !detailErr ? <div className="fx-hint">Loading brief #{open}…</div>
            : detailErr ? <div className="fx-err" role="alert">Brief #{open} did not load: {detailErr}</div>
            : current && edit ? (
            <section className="fx-sec fx-stack-v fx-brief-section" aria-label={`Brief #${current.id}`}>
              <div className="fx-row between">
                <div className="fx-row">
                  <span className="fx-id">#{current.id}</span><span className="fx-hint">revision {current.revision}</span>
                  <Tone tone={dismissed ? "gray" : STATE_TONE[current.state] || "gray"}>{dismissed ? "dismissed" : stateLabel(current)}</Tone>
                  {summary?.readiness === "superseded" ? <Tone tone="gray">superseded</Tone> : null}
                  {!dismissed && summary?.source_changed?.length ? <Tone tone="red">needs amendment</Tone> : null}
                </div>
                <div className="fx-hint">{current.created_by} · {ago(current.created_at)}
                  {current.approved_by ? ` · approved by ${current.approved_by} ${ago(current.approved_at)}` : ""}</div>
              </div>
              {current.created_by === "agent:brief-proposer" ? (
                <div className="fx-why">Auto-proposed by DeepSeek from captured sources. Generation is not verification or approval.
                  {!dismissed && current.state === "draft" ? " Review, edit and approve, or dismiss with a reason." : ""}</div>
              ) : null}
              {dismissal ? (
                <div className="fx-why">Dismissed: {dismissal.reason}
                  <div className="fx-hint">{dismissal.actor} · {ago(dismissal.at)} · Read-only history</div>
                </div>
              ) : null}
              {current.amendment_reason ? <div className="fx-why">Amended: {current.amendment_reason}</div> : null}
              {current.hold_reason ? <div className="fx-why">Hold: {current.hold_reason}</div> : null}
              {summary?.readiness === "superseded" ? (
                <div className="fx-err">Superseded — a newer revision exists; a draft with a child cannot be approved.</div>
              ) : null}
              {!dismissed ? <ReadinessPanel summary={summary} /> : null}
              {!dismissed && (investigation || investigationEligible) ? (
                <InvestigationPanel investigation={investigation} eligible={investigationEligible} dirty={dirty}
                                    busy={busy} onInvestigate={investigate} onOpenProposal={openBrief} />
              ) : null}
              {current.state === "approved" ? (
                <div className="fx-why">Approval freezes scope for planning only. It does not authorize or start execution.</div>
              ) : null}
              {!dismissed && summary?.replacement?.excluded?.length ? (
                <div className="fx-sec fx-stack-v">
                  <div className="fx-k">Recover this scope</div>
                  <div className="fx-hint">The approved brief stays intact. This explicit action replaces any current source picks,
                    excludes the ineligible members listed above, and opens the normal relationship review and grooming flow.</div>
                  <div className="fx-row">
                    <Button size="sm" ghost disabled={!!busy} onClick={reviewReplacement}>
                      {summary.replacement.eligible.length ? "Review replacement sources" : "Browse other work"}
                    </Button>
                    {current.state === "approved" ? <span className="fx-hint">Consider Hold while the blocked scope is unresolved.</span> : null}
                  </div>
                  {!summary.replacement.eligible.length ? (
                    <div className="fx-err">None of this brief's sources remain eligible. Browse other work; empty grooming is not offered.</div>
                  ) : null}
                </div>
              ) : null}

              <div className="fx-brief-fields">
                {FIELDS.map(([key, label, kind]) => (
                  <Fragment key={key}>
                    <Field label={label} kind={kind} value={shownForm[key]} disabled={!!busy}
                           readOnly={dismissed} onChange={(v) => editField(key, v)} />
                    {key === "resources" && (shownForm.resources || "").split("\n").map((x) => x.trim()).includes("global:*") ? (
                      <div className="fx-note"><code>global:*</code> reserves all execution resources, so this work runs alone.
                        Use narrower claims only after reviewing them.</div>
                    ) : null}
                  </Fragment>
                ))}
              </div>
              {!dismissed && dirty ? (current.state === "draft"
                ? <div className="fx-hint">Unsaved edits.</div>
                : <div className="fx-err">Unsaved edits are not the approved intent — amend (with a reason) before creating a draft dispatch.</div>
              ) : null}

              <div className="fx-k">Captured sources (server-captured, not model-edited)</div>
              <Provenance sources={current.sources} />
              {current.relationship_warnings?.length ? (
                <div className="fx-err">Relationships: {current.relationship_warnings.join("; ")}</div>
              ) : null}

              <details className="fx-sec fx-fold">
                <summary>Compiled preview (self-contained intent)</summary>
                <Preview md={detail?.render} err={detailErr} busy={current != null && detail?.render == null && !detailErr} />
              </details>

              <div className="fx-stack-v">
                {!dismissed && current.state === "draft" ? (
                  <>
                    <div className="fx-row">
                      <Button size="sm" disabled={!!busy || !dirty} onClick={save}>{busy === "save" ? "Saving…" : "Save draft"}</Button>
                      {summary?.readiness !== "superseded"
                        ? <Button size="sm" disabled={!!busy} onClick={approveBrief}>{busy === "approve" ? "Approving…" : arm === "approve" ? "Confirm approval" : "Approve brief"}</Button>
                        : null}
                      {summary?.readiness !== "superseded" && arm !== "dismiss" ? (
                        <Button size="sm" ghost disabled={!!busy} onClick={dismiss}>Dismiss draft</Button>
                      ) : null}
                    </div>
                    {arm === "approve" ? (
                      <div className="fx-sw">
                        <div className="fx-sw-q">Approve this brief?</div>
                        <div className="fx-sw-detail">This freezes the exact version above as approved scope for verification and
                          planning. It does not create a dispatch, start execution, override missing evidence, answer questions,
                          or change Linear.</div>
                      </div>
                    ) : null}
                    {arm === "dismiss" ? (
                      <div className="fx-sw">
                        <div className="fx-sw-q">Dismiss this draft?</div>
                        <div className="fx-sw-detail">Removes it from human review and keeps read-only history. It does not approve
                          work or change Linear.{dirty ? " Unsaved edits will not be saved." : ""}</div>
                        <label className="fx-field">
                          <span className="fx-k">Dismissal reason (required)</span>
                          <Input autoFocus required value={reason} maxLength={2000} disabled={!!busy}
                                 onChange={(e) => setReason(e.target.value)} />
                        </label>
                        <div className="fx-row">
                          <Button size="sm" disabled={!!busy || !reason.trim()} onClick={dismiss}>
                            {busy === "dismiss" ? "Dismissing…" : "Confirm dismissal"}
                          </Button>
                          <Button size="sm" ghost disabled={!!busy} onClick={() => { setArm(null); setReason(""); }}>Cancel</Button>
                        </div>
                      </div>
                    ) : null}
                  </>
                ) : null}

                {!dismissed && current.state !== "draft" ? (
                  <>
                    <div className="fx-row">
                      <Button size="sm" disabled={!!busy} onClick={amend}>{busy === "amend" ? "Amending…" : arm === "amend" ? "Confirm amendment" : "Amend"}</Button>
                      {current.state === "held"
                        ? <Button size="sm" disabled={!!busy} onClick={unhold}>{busy === "unhold" ? "…" : "Unhold"}</Button>
                        : <Button size="sm" ghost disabled={!!busy} onClick={hold}>{busy === "hold" ? "…" : arm === "hold" ? "Confirm hold" : "Hold"}</Button>}
                      {current.state === "approved"
                        ? <Button size="sm" disabled={!!busy || !canCreateDispatch} title={dispatchWhy || undefined}
                                  onClick={createDispatch}>{busy === "dispatch" ? "Creating…" : arm === "dispatch" ? "Confirm draft dispatch" : "Create draft dispatch"}</Button>
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
                    {current.state === "approved" && !canCreateDispatch && dispatchWhy ? (
                      <div className="fx-hint">Draft dispatch unavailable: {dispatchWhy}</div>
                    ) : null}
                    {arm === "dispatch" ? (
                      <div className="fx-sw">
                        <div className="fx-sw-q">Create a draft dispatch from this approved brief?</div>
                        <div className="fx-sw-detail">This pins the approved brief and opens the draft review flow. It does not
                          stage or execute work; execution still requires a separate dispatch review and approval.</div>
                      </div>
                    ) : null}
                  </>
                ) : null}
              </div>
            </section>
          ) : null}
        </>
      ) : briefsOpen ? (
        <section className="fx-brief-section" aria-label="Work briefs">
          <div className="fx-k">{plural(briefs.length, "brief")} · {pendingBriefs.length} to review</div>
          <div className="fx-list">
            {briefs.length ? [...pendingBriefs, ...briefs.filter((b) => !needsReview(b))].map((b) => (
              <BriefRow key={b.id} b={b} selected={false}
                        onSelect={() => openBrief(b.id)} onDispatch={openDispatch} />
            )) : <div className="fx-empty">No briefs yet. Groom a source, or create one from the CLI.</div>}
          </div>
        </section>
      ) : (
        <section className="fx-stack-v" aria-label="Sources">
          <DomainGroomSection
            domains={groomList?.domains || []} reviews={groomList?.reviews || []} listErr={groomListErr}
            loading={groomList == null} busy={busy} domainId={groomDomain} goal={groomGoal} mode={groomMode}
            onDomain={setGroomDomain} onGoal={setGroomGoal} onMode={setGroomMode} onStart={startReview}
            onOpen={openGroomReview} onRetry={retryReview} />
          <div className="fx-strategy-toolbar">
            <Input className="fx-search" type="search" placeholder="Search source id or title" value={q}
                   onChange={(e) => changeFilter({ q: e.target.value })} />
            <details className="fx-filter-panel">
              <summary>Filters{activeFilters.length ? ` (${activeFilters.length})` : ""}</summary>
              <div className="fx-row fx-filters">
                <select className="fx-select" value={stateFilter} aria-label="State filter"
                        onChange={(e) => changeFilter({ stateFilter: e.target.value })}>
                  <option value="all">All states</option>
                  {states.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                <select className="fx-select" value={ctxFilter} aria-label="Context filter"
                        onChange={(e) => changeFilter({ ctxFilter: e.target.value })}>
                  <option value="all">All contexts</option>
                  {contexts.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <select className="fx-select" value={assigneeFilter} aria-label="Assignee filter"
                        onChange={(e) => changeFilter({ assigneeFilter: e.target.value })}>
                  <option value="all">All assignees</option>
                  <option value="me">Assigned to me</option>
                  <option value="unassigned">Unassigned</option>
                  {assignees.map((a) => <option key={a} value={a}>{a}</option>)}
                </select>
              </div>
            </details>
              <select className="fx-select" value={sort} aria-label="Source sort"
                      onChange={(e) => changeFilter({ sort: e.target.value })}>
                <option value="priority">Priority</option>
                <option value="due">Due soon</option>
                <option value="oldest">Oldest created</option>
                <option value="newest">Newest created</option>
                <option value="updated">Recently updated</option>
              </select>
            <select className="fx-select fx-mode" aria-label="Source view" value={sourceMode}
                    onChange={(e) => changeFilter({ sourceMode: e.target.value })}>
              <option value="groups">Groups</option>
              <option value="flat">Flat list</option>
              <option value="dag">Dependency DAG</option>
            </select>
            <Button size="sm" ghost disabled={!!busy} onClick={refresh}>{busy === "refresh" ? "Refreshing…" : "Refresh"}</Button>
          </div>

          {activeFilters.length ? (
            <div className="fx-active-filters">
              <span>{activeFilters.join(" · ")}</span>
              <button className="fx-link-btn" aria-label="Clear source filters" onClick={clearFilters}>Clear</button>
            </div>
          ) : null}

          <RelationshipStatus relationships={relationships} missingIds={missingIds} />
          {replacementReview ? (
            <div className="fx-sec fx-stack-v" role="status">
              <div className="fx-k">Replacement review for brief #{replacementReview.briefId}</div>
              {replacementReview.eligible.length ? (
                <div className="fx-hint">{plural(replacementReview.eligible.length, "remaining eligible source")} preselected.
                  Review the selection and relationships before grooming. The approved brief is unchanged.</div>
              ) : (
                <div className="fx-err">No existing source remains eligible. Browse and select other work; grooming stays
                  unavailable until you make a non-empty selection.</div>
              )}
              {replacementReview.excluded.length ? (
                <details className="fx-sec fx-fold" open>
                  <summary>Excluded from replacement ({replacementReview.excluded.length})</summary>
                  <FactRows rows={replacementReview.excluded} />
                </details>
              ) : null}
              <div className="fx-row">
                <Button size="sm" ghost onClick={() => update({ replacementReview: null })}>End replacement review</Button>
              </div>
            </div>
          ) : null}
          {picked.length ? (
            <RelationshipReview picked={picked} memberGroupId={memberGroupId} groupById={groupById}
                                missingIds={missingIds} relationships={relationships} nodeInfo={nodeInfo} reasonOf={reasonOf} />
          ) : null}

          <div className="fx-hint">
            {sourceMode === "flat"
              ? `${shownCount} of ${matching} sources${filteredActive ? ` · ${total} total` : ""}`
              : `${shownCount} of ${visibleGroups.length} groups · ${filteredActive ? `${matching}/${total}` : total} sources`}
          </div>
          {sourceMode === "flat" ? (
            <div className="fx-list">
              {!all && !loadErr ? <div className="fx-hint">Loading…</div>
                : shown.length ? shown.map((t) => (
                  <SourceRow key={t.identifier} s={t} checked={pickedSet.has(t.identifier)} onToggle={() => pick(t.identifier)} />
                )) : <div className="fx-empty">{filteredActive ? "No source matches." : "No sources yet; refresh sources first."}</div>}
            </div>
          ) : (
            <div className="fx-groups">
              {!all && !loadErr ? <div className="fx-hint">Loading…</div>
                : shownGroups.length ? shownGroups.map((d) => (
                  <GroupCard key={d.g.id} d={d} open={expandedGroups.includes(d.g.id)} sourceMode={sourceMode}
                             pickedSet={pickedSet} onPick={pick} onSelectMatching={selectGroupMatching}
                             onToggle={setExpanded} nodeInfo={nodeInfo} reasonOf={reasonOf} statusOf={statusOf} />
                )) : <div className="fx-empty">{filteredActive ? "No group has a matching source." : "No groups yet; refresh sources first."}</div>}
            </div>
          )}
          {moreCount > 0 ? (
            <div className="fx-row">
              <Button size="sm" ghost onClick={() => update({ limit: limit + pageDefault(sourceMode) })}>Show {moreCount} more</Button>
            </div>
          ) : null}

          {picked.length ? (
            <div className="fx-selection-bar" role="region" aria-label="Selected sources">
              <span><strong>{plural(picked.length, "source")} selected</strong>{hiddenPicked ? ` · ${hiddenPicked} hidden` : ""}</span>
              <Button size="sm" ghost onClick={() => update({ picked: [] })}>Clear</Button>
              <Button size="sm" disabled={!!busy} onClick={groom}>
                {busy === "groom" ? "Grooming…" : `Groom ${picked.length}`}
              </Button>
            </div>
          ) : null}
        </section>
      )}

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
    </div>
  );
}

"""Linear GraphQL: read-only ingest. Mutations live only in reconcile."""
import json
import urllib.request

from . import db
from .config import Config, secret

API = "https://api.linear.app/graphql"

ISSUE_FIELDS = """
  id identifier title description url priority createdAt updatedAt archivedAt dueDate
  state { id name type }
  assignee { id email }
  team { id key }
  project { id name }
  projectMilestone { id name }
  labels { nodes { name } }
  parent { identifier }
  attachments { nodes { url } }
"""

ISSUES_QUERY = """
query($filter: IssueFilter, $after: String) {
  issues(filter: $filter, first: 100, after: $after, orderBy: updatedAt, includeArchived: true) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % ISSUE_FIELDS


def gql(cfg: Config, query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": secret(cfg, "LINEAR_API_KEY")},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if body.get("errors"):
        raise RuntimeError(f"linear: {body['errors'][0].get('message')}")
    return body["data"]


def scope_filter(cfg: Config) -> dict:
    """Tickets we care about: configured teams (my team) or assigned to me."""
    return {"or": [{"team": {"key": {"in": cfg.linear["teams"]}}}, {"assignee": {"isMe": {"eq": True}}}]}


def in_scope(cfg: Config, issue: dict) -> bool:
    team = cfg.linear.get("team", {}).get(issue["team"]["key"], {})
    state = issue["state"]
    return issue.get("archivedAt") is None and (
        state["type"] in cfg.linear.get("active_state_types", ["unstarted", "started"])
        or state["name"] == team.get("todo_state"))


def fetch(cfg: Config, flt: dict):
    after = None
    while True:
        page = gql(cfg, ISSUES_QUERY, {"filter": flt, "after": after})["issues"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


PROJECTS_QUERY = """
query($after: String) {
  projects(first: 100, after: $after, includeArchived: true) {
    nodes { id slugId name lead { email } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def sync_projects(cfg: Config, conn) -> int:
    """Upsert Linear's current projects (~150 rows, 2 pages) without destroying referenced identities.

    domain_review.domain_id references linear_project(id), so the old blanket DELETE fails with a
    foreign-key error once any review exists. Fetched projects are upserted in place: new, renamed
    and reassigned rows update by id. Projects absent from the fetch are handled truthfully:
    unreferenced rows are deleted, while a removed project a review still points at keeps its row
    (the identity stays resolvable) but loses ownership — lead_email becomes NULL, which every
    ownership path (prune.owned, domain_groom.request/list_, strategy._ticket_list) already treats
    as "not ours". The whole fetch happens before the single write, so a failed fetch changes nothing.
    """
    rows, after = [], None
    while True:
        page = gql(cfg, PROJECTS_QUERY, {"after": after})["projects"]
        rows += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    at = db.now()
    with db.tx(conn):
        conn.executemany(
            "INSERT INTO linear_project(id, slug_id, name, lead_email, fetched_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET slug_id=excluded.slug_id, name=excluded.name, "
            "lead_email=excluded.lead_email, fetched_at=excluded.fetched_at",
            [(p["id"], p["slugId"], p["name"], (p["lead"] or {}).get("email"), at) for p in rows])
        fetched = {p["id"] for p in rows}
        referenced = {r[0] for r in conn.execute("SELECT DISTINCT domain_id FROM domain_review")}
        for row in conn.execute("SELECT id FROM linear_project"):
            if row["id"] in fetched:
                continue
            if row["id"] in referenced:
                # A review still cites this project; keep the identity but drop the stale owner so it
                # is no longer listed, requestable, or counted as owned.
                conn.execute("UPDATE linear_project SET lead_email=NULL, fetched_at=? WHERE id=?", (at, row["id"]))
            else:
                conn.execute("DELETE FROM linear_project WHERE id=?", (row["id"],))
    return len(rows)


CURSOR = "linear:issues"
SKEW = 120  # seconds of overlap for clock skew; upsert makes the overlap free


def ingest(cfg: Config, conn, full: bool = False) -> dict:
    from datetime import datetime, timedelta

    row = conn.execute("SELECT updated_at_gt FROM sync_cursor WHERE name=?", (CURSOR,)).fetchone()
    flt = scope_filter(cfg)
    if row is None or full:
        # First run: only currently-active tickets; afterwards every change in scope, any state,
        # so tickets closed elsewhere still land and invalidate verdicts.
        flt = {"and": [flt, {"state": {"type": {"in": ["unstarted", "started", "backlog"]}}}]}
        cursor = None
    else:
        cursor = row["updated_at_gt"]
        since = datetime.fromisoformat(cursor.replace("Z", "+00:00")) - timedelta(seconds=SKEW)
        flt = {"and": [flt, {"updatedAt": {"gt": since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}}]}

    fetched = inserted = 0
    max_updated = cursor
    fetched_at = db.now()
    issues = list(fetch(cfg, flt))  # paginated HTTP outside the write lock
    with db.tx(conn):
        for issue in issues:
            fetched += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO linear_snapshot VALUES (?,?,?,?,?,?,?)",
                (issue["id"], issue["identifier"], issue["updatedAt"], fetched_at, issue["state"]["type"],
                 int(in_scope(cfg, issue)), json.dumps(issue, sort_keys=True)))
            inserted += cur.rowcount
            # The due sidecar is version-keyed and idempotent: populate it even when the snapshot itself was
            # ignored (unchanged), and never touch the append-only raw_json. A NULL due_date records an actual
            # due removal on this version.
            conn.execute(
                "INSERT OR IGNORE INTO linear_due(issue_id, snapshot_updated_at, due_date) VALUES (?,?,?)",
                (issue["id"], issue["updatedAt"], issue.get("dueDate")))
            if max_updated is None or issue["updatedAt"] > max_updated:
                max_updated = issue["updatedAt"]
        conn.execute(
            "INSERT INTO sync_cursor VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "updated_at_gt=excluded.updated_at_gt, last_run_at=excluded.last_run_at, last_count=excluded.last_count",
            (CURSOR, max_updated or fetched_at, fetched_at, inserted))
    res = {"fetched": fetched, "inserted": inserted, "cursor": max_updated}
    # Relationship snapshots refresh AFTER the source snapshot tx committed: a link mutation
    # may not bump issue updatedAt, so refresh runs every ingest. A failure propagates and
    # leaves the already-committed source snapshot in place; the next ingest retries regardless
    # of updatedAt. Imported locally to keep linear/prune import edges one-way.
    from . import relationships
    res["relationships"] = relationships.refresh(cfg, conn)
    return res

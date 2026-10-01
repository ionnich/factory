"""Read-only GitHub review inbox, fetched through the authenticated ``gh`` CLI.

The inbox contains only open, non-draft pull requests whose current review requests name the viewer directly or a
team the viewer belongs to. Factory dispatch links come only from exact PR URLs already recorded on a dispatch card.
"""
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime

from . import dispatch

MAX_TEAMS = 20
PAGE_SIZE = 10
MAX_ITEMS = 250
TOTAL_TIMEOUT = 50

_PULL_FIELDS = """
    nodes {
      ... on PullRequest {
        url title number state isDraft createdAt updatedAt
        repository { nameWithOwner }
        author { login }
        reviewRequests(first: 100) {
          nodes {
            requestedReviewer {
              ... on User { login }
              ... on Team { slug name organization { login } }
            }
          }
        }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
    pageInfo { hasNextPage endCursor }
"""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _problem(stderr: str, fallback: str) -> str:
    text = " ".join(stderr.strip().split())
    return text[-400:] if text else fallback


def _run_gh(args: list[str], timeout: float):
    gh = shutil.which("gh")
    if not gh:
        raise FileNotFoundError("gh is not installed or is not on PATH")
    return subprocess.run([gh, *args], capture_output=True, text=True, timeout=max(1, timeout))


def _teams(runner, deadline: float) -> tuple[list[dict], list[str]]:
    """Return active viewer teams. ``gh api --paginate --jq`` emits one JSON object per line across all pages."""
    try:
        result = runner(["api", "--paginate", "/user/teams?per_page=100", "--jq",
                         ".[] | {slug: .slug, name: .name, organization: .organization.login}"],
                        min(15, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"Team review requests could not be read: {exc}"]
    if result.returncode:
        return [], ["Team review requests could not be read: " + _problem(result.stderr, "GitHub API failed")]
    teams = []
    try:
        for line in result.stdout.splitlines():
            if line.strip():
                team = json.loads(line)
                if team.get("organization") and team.get("slug"):
                    teams.append(team)
    except (json.JSONDecodeError, TypeError):
        return [], ["Team review requests could not be read: gh returned invalid JSON"]
    warnings = []
    if len(teams) > MAX_TEAMS:
        warnings.append(f"Only the first {MAX_TEAMS} GitHub teams were checked; the inbox may be incomplete.")
        teams = teams[:MAX_TEAMS]
    return teams, warnings


def _query(teams: list[dict], aliases: list[str] | None = None, cursors: dict[str, str] | None = None,
           page_size: int = PAGE_SIZE) -> tuple[str, dict[str, dict | None]]:
    sources = {"direct": None} | {f"team{i}": team for i, team in enumerate(teams)}
    qualifiers = {"direct": "review-requested:@me"} | {
        f"team{i}": f"team-review-requested:{team['organization']}/{team['slug']}"
        for i, team in enumerate(teams)}
    aliases = aliases or list(sources)
    parts = ["viewer { login }"] if cursors is None else []
    for alias in aliases:
        search = f"is:pr is:open draft:false {qualifiers[alias]} sort:updated-desc"
        after = f", after: {json.dumps(cursors[alias])}" if cursors else ""
        parts.append(f"{alias}: search(query: {json.dumps(search)}, type: ISSUE, first: {page_size}{after}) "
                     f"{{ {_PULL_FIELDS} }}")
    return "query { " + " ".join(parts) + " }", sources


def _checks(pr: dict) -> str:
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") if commits else None) or {}
    return {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed", "PENDING": "pending",
            "EXPECTED": "pending"}.get(rollup.get("state"), "unknown")


def _requested(pr: dict, source: dict | None, teams: dict[tuple[str, str], dict],
               viewer: str) -> str | None:
    requested = [n.get("requestedReviewer") or {} for n in (pr.get("reviewRequests") or {}).get("nodes") or []]
    if source:
        org, slug = source["organization"].lower(), source["slug"].lower()
        if any((r.get("organization") or {}).get("login", "").lower() == org and
               r.get("slug", "").lower() == slug for r in requested):
            return f"Team {source.get('name') or source['slug']} ({source['organization']})"
        return None  # stale search result: the exact team request is no longer present
    matching = []
    for reviewer in requested:
        if viewer and reviewer.get("login", "").lower() == viewer:
            return "Requested from you"
        org = (reviewer.get("organization") or {}).get("login", "").lower()
        team = teams.get((org, reviewer.get("slug", "").lower()))
        if team:
            matching.append(f"Team {team.get('name') or team['slug']} ({team['organization']})")
    return matching[0] if matching else None  # stale search result: no applicable request remains


def _dispatch_for(conn, url: str) -> tuple[dict | None, bool]:
    # Both fields are written by ``factory card done``. The event fallback keeps exact associations from older rows
    # whose dispatch_ticket.pr_url was not populated; no title, branch, ticket, or repository guessing is allowed.
    rows = conn.execute(
        f"SELECT DISTINCT d.run_id, d.state, {dispatch.PHASE} phase, d.created_at "
        "FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
        "LEFT JOIN card_event e ON e.run_id=t.run_id AND e.issue_id=t.issue_id "
        "WHERE t.pr_url=? OR json_extract(e.metadata_json, '$.pr')=? "
        "ORDER BY d.created_at DESC LIMIT 2", (url, url)).fetchall()
    if len(rows) != 1:
        return None, len(rows) > 1
    return {k: rows[0][k] for k in ("run_id", "state", "phase")}, False


def _items(data: dict, sources: dict[str, dict | None], teams: list[dict], conn) -> tuple[list[dict], list[str], bool]:
    viewer = ((data.get("viewer") or {}).get("login") or "").lower()
    memberships = {(t["organization"].lower(), t["slug"].lower()): t for t in teams}
    found, warnings, truncated = {}, [], False
    if not viewer:
        warnings.append("GitHub did not identify the authenticated viewer; direct review requests were omitted.")
    for alias, source in sources.items():
        result = data.get(alias)
        if result is None:
            warnings.append(f"GitHub did not return the {'direct' if source is None else source['organization'] + '/' + source['slug']} review search.")
            continue
        if (result.get("pageInfo") or {}).get("hasNextPage"):
            truncated = True
        for pr in result.get("nodes") or []:
            if not pr or pr.get("state") != "OPEN" or pr.get("isDraft") or not pr.get("url"):
                continue
            if viewer and (pr.get("author") or {}).get("login", "").lower() == viewer:
                continue
            context = _requested(pr, source, memberships, viewer)
            if context is None:
                continue
            item = found.get(pr["url"])
            if item:
                if context not in item["request_context"]:
                    item["request_context"] += f"; {context}"
                continue
            association, ambiguous = _dispatch_for(conn, pr["url"])
            if ambiguous:
                warnings.append(f"{pr['url']} is recorded on more than one dispatch; no dispatch link is shown.")
            found[pr["url"]] = {
                "url": pr["url"], "title": pr.get("title") or "Untitled pull request", "number": pr.get("number"),
                "repository": (pr.get("repository") or {}).get("nameWithOwner"),
                "author": (pr.get("author") or {}).get("login"), "created_at": pr.get("createdAt"),
                "updated_at": pr.get("updatedAt"), "checks": _checks(pr), "request_context": context,
                "dispatch": association,
            }
    return sorted(found.values(), key=lambda p: p.get("updated_at") or "", reverse=True), warnings, truncated


def list_reviews(conn, runner=_run_gh) -> dict:
    """Fetch the review inbox. Failures are data, not an invented empty inbox; callers can keep the last good list."""
    fetched_at = _now()
    deadline = time.monotonic() + TOTAL_TIMEOUT
    teams, warnings = _teams(runner, deadline)
    partial = bool(warnings)
    query, sources = _query(teams)
    try:
        result = runner(["api", "graphql", "-f", f"query={query}"], deadline - time.monotonic())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"items": [], "count": None, "fetched_at": fetched_at, "warnings": warnings,
                "partial": True, "truncated": False, "error": f"GitHub review requests could not be read: {exc}"}
    if result.returncode:
        return {"items": [], "count": None, "fetched_at": fetched_at, "warnings": warnings,
                "partial": True, "truncated": False,
                "error": "GitHub review requests could not be read: " + _problem(result.stderr, "GitHub API failed")}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"items": [], "count": None, "fetched_at": fetched_at, "warnings": warnings,
                "partial": True, "truncated": False, "error": "GitHub review requests returned invalid JSON"}
    api_errors = [e.get("message", "unknown GraphQL error") for e in payload.get("errors") or []]
    if api_errors:
        warnings.append("GitHub returned partial data: " + "; ".join(api_errors[:3]))
        partial = True
    data = payload.get("data")
    if not data:
        return {"items": [], "count": None, "fetched_at": fetched_at, "warnings": warnings,
                "partial": True, "truncated": False, "error": "GitHub review requests returned no data"}
    pending = {alias: result["pageInfo"]["endCursor"] for alias in sources
               if (result := data.get(alias)) and (result.get("pageInfo") or {}).get("hasNextPage")
               and (result.get("pageInfo") or {}).get("endCursor")}
    total = sum(len((data.get(alias) or {}).get("nodes") or []) for alias in sources)
    while pending and total < MAX_ITEMS and deadline - time.monotonic() > 2:
        remaining = MAX_ITEMS - total
        selected = list(pending)[:remaining]
        page_size = max(1, min(PAGE_SIZE, remaining // len(selected)))
        page_query, _ = _query(teams, selected, pending, page_size)
        try:
            page_result = runner(["api", "graphql", "-f", f"query={page_query}"], deadline - time.monotonic())
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"GitHub pagination stopped early: {exc}")
            partial = True
            break
        if page_result.returncode:
            warnings.append("GitHub pagination stopped early: " +
                            _problem(page_result.stderr, "GitHub API failed"))
            partial = True
            break
        try:
            page_payload = json.loads(page_result.stdout)
        except json.JSONDecodeError:
            warnings.append("GitHub pagination stopped early: gh returned invalid JSON")
            partial = True
            break
        page_errors = [e.get("message", "unknown GraphQL error") for e in page_payload.get("errors") or []]
        if page_errors:
            warnings.append("GitHub pagination returned partial data: " + "; ".join(page_errors[:3]))
            partial = True
        page_data = page_payload.get("data") or {}
        before = total
        cursors_before = dict(pending)
        for alias in selected:
            page = page_data.get(alias)
            if page is None:
                warnings.append(f"GitHub pagination omitted the {alias} review search.")
                partial = True
                pending.pop(alias, None)
                continue
            nodes = page.get("nodes") or []
            data[alias].setdefault("nodes", []).extend(nodes)
            total += len(nodes)
            data[alias]["pageInfo"] = page.get("pageInfo") or {}
            info = page.get("pageInfo") or {}
            if info.get("hasNextPage") and info.get("endCursor"):
                pending[alias] = info["endCursor"]
            else:
                pending.pop(alias, None)
        if total == before and pending == cursors_before:
            warnings.append("GitHub pagination made no progress; later review requests were omitted.")
            partial = True
            break
    items, item_warnings, truncated = _items(data, sources, teams, conn)
    warnings.extend(item_warnings)
    partial = partial or bool(item_warnings)
    if truncated:
        warnings.append(f"Some review searches had more results; showing at most the newest {MAX_ITEMS} matches.")
    return {"items": items, "count": None if partial else len(items), "fetched_at": fetched_at,
            "warnings": warnings, "partial": partial, "truncated": truncated, "error": None}

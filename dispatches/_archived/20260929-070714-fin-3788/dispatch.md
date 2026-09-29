# Dispatch 20260929-070714-fin-3788

Staged 2026-09-29T07:07:14.866162Z by factory:propose. This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-api @ trunk `443d6e42d76ecbd78f828e6cc57a94b7d7313c97`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## FIN-3788: Chore: allowlist console origins on commercial /v1/search CORS

- Linear: https://linear.app/joefinks/issue/FIN-3788/chore-allowlist-console-origins-on-commercial-v1search-cors (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-api (context search-api), trunk `443d6e42d76ecbd78f828e6cc57a94b7d7313c97`
- Verdict: valid — The CORS allowlist is not implemented anywhere in the repo: no CorsLayer/allow_origins and no OPTIONS preflight handler exist, and /v1/search (plus /v1/account*) all sit behind auth_middleware, so an unauthenticated OPTIONS preflight still 401s. The ask is still needed and its referenced routes exist at trunk.
- Evidence:
  - `{"note": "build_router registers /v1/search, /v1/search/fields, /v1/account, /v1/account/keys, /v1/account/usage, each wrapped in auth::auth_middleware; no CORS layer or OPTIONS route present", "path": "src/lib.rs", "sha": "443d6e42d76ecbd78f828e6cc57a94b7d7313c97", "type": "file"}`

### Ticket body (claim, not truth)

Domain: [External API Surface](https://linear.app/joefinks/project/external-api-surface-b94799e1ad92)

Context: The console browser cannot call `POST /v1/search` because a keyless OPTIONS preflight returns 401.

Change: Allow unauthenticated CORS preflight for allowlisted console Origins on commercial `/v1/*`.

Assumptions:

* Allowlist is `http://127.0.0.1:3000` and `https://finks.ai` (plus staging if the console is served there) · customer / design-partner Origins are not console · they stay off the list and call server-side.
* Do not touch `api.finks.ai` · that host is not the commercial edge · if a custom domain is already live, apply the same preflight rule there.

Acceptance:

* `OPTIONS` on `/v1/search`, `/v1/search/fields`, `/v1/account`, `/v1/account/keys`, and `/v1/account/usage` from an allowlisted Origin returns 2xx or 204 without a bearer, with `Access-Control-Allow-Origin`, `Access-Control-Allow-Headers: authorization,content-type`, and `Access-Control-Allow-Methods: POST,GET,OPTIONS`.
* A browser `POST /v1/search` with `Authorization: Bearer fk_test_…` from that origin is not CORS-blocked; a missing key on POST is still `401`.

Honors: `XAPI-INV-4`

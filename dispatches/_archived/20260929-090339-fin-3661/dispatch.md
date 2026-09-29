# Dispatch 20260929-090339-fin-3661

Approved 2026-09-29T09:54:06.530138Z by user:dashboard. This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-insights-service @ trunk `e9f15da714c803519d13e9c7f2aa793a44d4492a`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and override the plan and the ticket body; if one cannot be followed, `factory card block` with why.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. Name the step id (e.g. FIN-1/2) in comments.
- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep working on other tickets; the answer arrives in this session.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## Theme: Insights report fallback and candidate-payload cleanup

Two independent hardening changes in finks-insights-service: FIN-3661 gives a failed Report one regeneration before falling back to the card narrative, and FIN-2553 pre-formats money and percentages and drops metadata from the candidate payload the model describes. Both share the Insights domain and the grounding-gate context, so one pass lands both.

## Answered questions (binding)

- On FIN-3661/2: How should the card-narrative fallback be represented when both generation attempts fail the gate? **Narrative-only ready row**: Consumer writes a ready brief with headline set to the title, narrative set to the card narrative, and no visuals; the handler serves it verbatim. Smallest change, a documented exception to the at-least-one-visual invariant; relies on the frontend rendering a ready brief with zero visuals. (user:dashboard (approved with the recommendation))
- On FIN-2553/2: Does 1-decimal percentage formatting extend to the entry-row percentages (eps_beat_pct, change_5d_pct), or only the portfolio_overview percentages named in acceptance? **All percentage fields**: Format change_percent, weight_percent, eps_beat_pct, and change_5d_pct to exactly 1 decimal. Matches the assumption that percentages apply to every insight type. (user:dashboard (approved with the recommendation))

## FIN-3661: Improvement: a failed Insight Report regenerates once, then falls back to the card narrative

- Linear: https://linear.app/joefinks/issue/FIN-3661/improvement-a-failed-insight-report-regenerates-once-then-falls-back (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-insights-service (context insights), trunk `e9f15da714c803519d13e9c7f2aa793a44d4492a`
- Verdict: valid — The pre-generated brief (the Report) still has no regenerate-once-then-fallback: src/brief/consumer.rs calls generate() exactly once with no retry loop and writes a terminal failed row on any gate/grounding error, and src/brief/handler.rs serves that as status=failed with no body, so the 'Brief unavailable' state persists. The card narrative already on published_insights is never consulted as the fallback the ticket asks for.
- Evidence:
  - `{"note": "consumer generates exactly once (deps.generator.generate(&ctx).await) with no retry loop; writes terminal failed on generator error per step-5 doc comment", "path": "src/brief/consumer.rs", "sha": "427067932c68255525e265a7881d66af81812400", "type": "file"}`
  - `{"note": "map_status maps BriefStatus::Failed to status=failed with brief=None; no card-narrative fallback path", "path": "src/brief/handler.rs", "sha": "427067932c68255525e265a7881d66af81812400", "type": "file"}`

### Plan

- **FIN-3661/1** Retry grounding rejections once in src/brief/consumer.rs:handle_event
  Wrap deps.generator.generate(&ctx).await (L255) so an Err(Error::Generation(msg)) whose msg starts with BRIEF_GROUNDING_FAILED_PREFIX is retried exactly once. Reuse classify_failure_stage to detect grounding; non-grounding errors and Ok-with-zero-visuals keep the current single-shot path.
- **FIN-3661/2** Write the card narrative as the fallback brief on a second grounding failure (after FIN-3661/1)
  Replace the terminal failed write (L306-317) for a second grounding failure with a ready brief whose BriefBody is headline set to published.title, narrative set to published.narrative, visuals empty, sources empty (per the fallback question), reusing the same generated_at. Non-grounding failures still write failed as today.
- **FIN-3661/3** Confirm src/brief/handler.rs serves the fallback body as ready (after FIN-3661/2)
  map_status already serves a Ready brief verbatim, so a narrative-only ready row needs no handler change (per the fallback question). Verify the envelope is status ready with the narrative body and no Brief unavailable state; only change handler.rs if the handler-side option is chosen.
- **FIN-3661/4** Consumer unit tests: retry-once, fallback, non-grounding unchanged (after FIN-3661/2)
  Add consumer tests: first attempt grounding error plus second success writes ready; first plus second grounding error writes the narrative fallback with no failed row; non-grounding error still writes failed; zero-visuals still writes failed. Extend FakeBriefGenerator to fail-then-succeed.
- **FIN-3661/5** Handler test: fallback brief serves as narrative (after FIN-3661/3)
  Add a handler test that a ready brief with empty visuals serves status ready with the card narrative, confirming the frontend never sees the failed or Brief unavailable state.
- **FIN-3661/6** Open PR against main; CI green (after FIN-3661/4, FIN-3661/5)
  Open a PR against main for finks-insights-service; confirm cargo test and clippy pass and CI is green before merge.

### Ticket body (claim, not truth)

Domain: [Insights](<https://linear.app/joefinks/project/insights-7e4e661e5e0a>)

Context: A Report that fails the grounding gate leaves the user on a dead card reading "Brief unavailable", with nothing to open.

Change: On a grounding rejection, regenerate the Report once; if the second attempt also fails, serve the card's own narrative in place of the error state rather than persisting a terminal `failed` read.

Assumptions:

* One retry, not a backoff chain · a second sample usually clears a per-run phrasing miss and the generation cost is one call · if the retry rarely clears, drop it and fall back on the first failure.
* The fallback reuses the card `narrative` already on `published_insights` · the card is grounded by construction, so it needs no second gate pass · if the expanded view needs more than one line, the fallback becomes a deterministic render of the candidate payload.

Acceptance:

* A Report whose first generation fails the gate and whose second succeeds serves as `ready` (`INS-INV-2`).
* A Report failing both attempts opens showing the card's narrative, with no "Brief unavailable" error state.

## FIN-2553: Improvement: pre-format numeric fields and drop metadata from insights candidate payloads

- Linear: https://linear.app/joefinks/issue/FIN-2553/improvement-pre-format-numeric-fields-and-drop-metadata-from-insights (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-insights-service (context insights), trunk `e9f15da714c803519d13e9c7f2aa793a44d4492a`
- Verdict: valid — Still unimplemented: the candidate payload continues to emit raw floats and the metadata the ticket asks to drop. compose.rs changes since the last verdict only added a detected_at field (unrelated).
- Evidence:
  - `{"note": "portfolio_components (L1023) still emits label \"connected accounts\" (L1036) and raw f64 total_value_today/change_dollar/change_percent (L1033-1035); weight_percent raw (L1050/1069). No whole-dollar/thousands or 1-decimal formatting anywhere", "path": "src/llm/compose.rs", "sha": "e9f15da714c803519d13e9c7f2aa793a44d4492a", "type": "file"}`
  - `{"note": "build_entry_row (L791) still emits raw market_cap (L821/851/878) with no $0 drop and no formatting", "path": "src/llm/compose.rs", "sha": "e9f15da714c803519d13e9c7f2aa793a44d4492a", "type": "file"}`

### Plan

- **FIN-2553/1** Format money and percentages in src/llm/compose.rs:portfolio_components
  In portfolio_components (L1023): format total_value_today and change_dollar to the nearest whole dollar with thousands separators; change_percent and weight_percent (holdings L1050 and sector L1069) to exactly 1 decimal with a trailing zero; drop the label field (L1036).
- **FIN-2553/2** Format and drop-zero market_cap in src/llm/compose.rs:build_entry_row
  In build_entry_row (L791): format market_cap to whole dollars with thousands separators and omit it when zero; keep the raw u64 for the magnitude ranking field (L832/864). Format eps_beat_pct and change_5d_pct to 1 decimal per the percentage question.
- **FIN-2553/3** Match Jinja templates to the formatted payload (after FIN-2553/1, FIN-2553/2)
  Update stock_movers.j2, upcoming_earnings.j2, and earnings_reported.j2 so the model does not see raw floats or a zero market cap: render the formatted values and omit market_cap when absent, consistent with compose.rs (the other site the assumption flags).
- **FIN-2553/4** Compose and grounding tests for the formatted payload (after FIN-2553/3)
  Add compose.rs unit tests pinning the formatted money and percentage strings, label absent, and zero market_cap absent; add grounding tests confirming comma-separated money and trailing-zero percentages still pass the numeric gate (grounding.rs already accepts commas).
- **FIN-2553/5** Open PR against main; CI green (after FIN-2553/4)
  Open a PR against main for finks-insights-service; confirm cargo test, clippy, and CI are green before merge.

### Ticket body (claim, not truth)

Domain: [Insights](<https://linear.app/joefinks/project/insights-7e4e661e5e0a>)

Context: Card copy prints unrounded figures and account labels the prompt tells the model to hide.

Change: Format money and percentages once when the candidate payload is assembled, and drop non-financial metadata from the fields the model describes.

Assumptions:

* Format once at payload assembly · the Jinja template was the other site · if the model still receives raw floats from the template, format there too.
* Whole dollars and 1-decimal percentages apply to every insight type · only `portfolio_overview` had a written convention · if a type already publishes a different precision, keep that precision.
* A `$0` `market_cap` is removed · every sampled payload carried zero · if the card needs a real market cap, populate the field instead of dropping it.

Acceptance:

* Money in the candidate payload, including `portfolio_overview` `change_dollar` and `total_value_today`, is the nearest whole dollar with thousands separators, and percentages including `change_percent` and `weight_percent` are exactly 1 decimal with a trailing zero kept (`INS-INV-3`).
* `label`, account-type strings, and a `$0` `market_cap` are absent from the fields the model is asked to describe.

Honors: `INS-INV-2`

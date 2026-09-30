# Dispatch 20260930-045817-fin-4146

Approved 2026-09-30T10:06:17.659250Z by factory:auto (you were silent after it reached you). This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-dagster @ trunk `3a744be31295bba50919d770f475491f295a9ec9`

## Runs in

- `fx-financial-data` (factory-fleet), handed this dispatch directly by the factory

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and override the plan and the ticket body; if one cannot be followed, `factory card block` with why.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. Name the step id (e.g. FIN-1/2) in comments.
- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep working on other tickets; the answer arrives in this session.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## Theme: Repair XLE spurious 2x split-adjusted close steps

XLE canonical close_price carries clean-factor steps (x1.99 / x0.51 / x1.99) because a spurious raw doubling for 2025-11-24 through 12-02 is not guarded before the 0.5 split factor applies; one guarded fix plus a fenced recovery backfill lands the repair.

## Answered questions (binding)

- On FIN-4146/1: How should the spurious-raw-step guard behave? **Fail loud, operator repairs**: the backfill raises with the symbol and dates; no silent rewrite of canonical price history (user:dashboard)

## FIN-4146: Bug: XLE split-adjusted close has spurious 2x steps — price-performance chart renders fake spikes

- Linear: https://linear.app/joefinks/issue/FIN-4146/bug-xle-split-adjusted-close-has-spurious-2x-steps-price-performance (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-dagster (context finks-data), trunk `3a744be31295bba50919d770f475491f295a9ec9`
- Verdict: valid — XLE's canonical close_price still steps x1.99/x0.51/x1.99 across 2025-11-24/12-03/12-05: raw_close_price spuriously doubles to ~89 on 2025-11-24 (44.71 -> 89.17 -> 45.92) while the 0.5 split factor halves those pre-12-05 rows, netting the fake spikes. The transform applies the split factor but has no guard for a spurious raw step, so the repair+backfill ask is still needed.
- Evidence:
  - `{"note": "_apply_split_adjustments sets raw_close_price from the upstream close and multiplies pre-split rows by denominator/numerator (0.5 for 2025-12-05:2/1); it has no detection or repair for a spurious raw step, so a bad raw doubling propagates into close_price", "path": "finks_dagster/defs/gold/dimensions/master_eod_closing_prices.py", "sha": "3a744be31295bba50919d770f475491f295a9ec9", "type": "file"}`
  - `{"note": "XLE in ck_dev.master_eod_closing_prices: close_price 22.355 -> 44.585 on 2025-11-24 (x1.99), 45.04 -> 22.96 on 2025-12-03 (x0.51), 23.055 -> 45.92 on 2025-12-05 (x1.99); raw_close_price 44.71 -> 89.17 -> 45.92 doubles spuriously for 2025-11-24..12-02", "query": "SELECT symbol, trade_date, raw_close_price, close_price, split_adjustment_factor, split_evidence FROM ck_dev.master_eod_closing_prices WHERE symbol = 'XLE' AND trade_date >= '2025-11-20' AND trade_date <= '2025-12-10' ORDER BY trade_date", "result_sha256": "40b1920f8556600ae88f576f202926b949e1c0d148ed507f2a692be69cf4e756", "type": "sql", "witness": "clickhouse-serving", "witness_log_id": 86}`

### Plan

- **FIN-4146/1** Add spurious-raw-step detector in master_eod_closing_prices.py
  Pure helper next to _raw_history_for_symbols that flags a clean-factor day-over-day step in raw_close_price not aligned to a declared split_date.
- **FIN-4146/2** Repair the spurious step in rebuild projection build_rebuild_select (after FIN-4146/1)
  Revert XLE spurious 2025-11-24 through 12-02 raw doubling in master_eod_closing_prices_ck_load.py before the 0.5 factor applies.
- **FIN-4146/3** Add unit tests for the detector and the XLE repair (after FIN-4146/2)
  Extend test_master_eod_closing_prices.py and test_master_eod_closing_prices_ck_load.py with a fixture reproducing the XLE raw double.
- **FIN-4146/4** Backfill XLE via the FIN-3450 recovery rail (after FIN-4146/3)
  Run publish_atomic_candidate with a captured live fingerprint and source watermark to land the corrected XLE series.
- **FIN-4146/5** Verify acceptance: clean-factor scan, 1Y return, continuous chart (after FIN-4146/4)
  XLE close_price has no clean-factor step across 11-24, 12-03, and 12-05; 1Y return near +36.3% matching FMP; rebuild master_price_performance.

### Ticket body (claim, not truth)

Domain: [FMP Ingestion Pipeline](https://linear.app/joefinks/project/fmp-ingestion-pipeline-1a99f8528448)

Context: XLE's split-adjusted close in `ck_dev.master_eod_closing_prices` is discontinuous around its 2025-12-05 split, so the Inline Charts price-performance chart renders fake \~+100% vertical spikes ("how has the energy sector performed vs the market").

Change: Repair XLE's canonical price series in finks-dagster and backfill. `close_price` steps ×1.99 on 2025-11-24, ×0.51 on 2025-12-03, ×1.99 on 2025-12-05: `raw_close_price` doubles spuriously for 2025-11-24 → 12-02 (44.71 → 89.17 → 45.92) while `split_adjustment_factor=0.5` (`split_evidence='2025-12-05:2/1'`) halves every pre-12-05 row. Net: the 1Y rebased series ends +175% instead of \~+37%.

Acceptance:

* XLE `close_price` has no clean-factor step across 2025-11-24, 2025-12-03, or 2025-12-05, and its 1Y return matches the FMP figure (+36.3%; close 45.15 on 2025-09-29 → 62.10 on 2026-09-28).
* The [FIN-3450](https://linear.app/joefinks/issue/FIN-3450/bug-master-eod-closing-prices-holds-unadjusted-closes-stock-splits) rolling five-year clean-factor scan passes for XLE, and the price-performance chart renders one continuous line with no spike.

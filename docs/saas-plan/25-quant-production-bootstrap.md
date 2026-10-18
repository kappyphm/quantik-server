# Quant production bootstrap — 2026-10-18

Screening publication now exists, but this alone does not prove Quant readiness.
Production inspection found zero models, zero market snapshots and zero reports.
The scheduled TRAIN for the 2026-10-16 session was still PENDING for its configured
future run time; earlier snapshot tasks had expired without inputs.

## Actual training result

Executed `train_manifest` in the production train container, using completed
manifest `c4945936-cc4c-431a-b7f5-34c34607746e` and the same configuration as the
scheduled worker: `replace(QuantConfig(), PRICE_INPUT_UNIT="VND")`.
The initial diagnostic invocation omitted this override and failed before
training; it created no model. The corrected invocation exited 0.

- Persisted model: `788984fc-4400-4414-9121-3f4cffbb8497`, status CANDIDATE.
- Training rows: 509,148; full mature refit rows: 532,948.
- Validation: 15,752 rows across 40 sessions, 2026-08-05 through 2026-10-02.
- Train feature cutoff: 2026-07-07; train label cutoff: 2026-07-21.
- Rank IC: 0.1290238706 across 40 rank days.
- Absolute RMSE: 6.3707395819 versus zero-return baseline 6.3763329401.
- Alpha RMSE: 6.1854084640 versus baseline 6.2133180147.
- Directional accuracy: 0.5246952768. Improvements in RMSE are small; acceptance
  is evidence of this configured holdout test, not guaranteed predictive value.
- Existing temporal-beta-1 validation accepted with no rejection reasons.
- Observed train-container memory reached about 764 MiB within its 1.5 GiB
  ceiling. This is an observed sample, not an instrumented peak or load test.

Log on server: `/opt/quantik/releases/20261018-screening-availability/quant-bootstrap.log`.
No customer requests, quotas, subscriptions or payments were changed.

## Remaining work

The model evidence has `live_data_validated=false`: stored inputs retain
LIVE_UNVERIFIED provenance and PROVIDER_ADJUSTMENT_UNVERIFIED. No metadata was
rewritten to bypass publication. The model has not been promoted; no ready Quant
snapshot or completed customer report has yet been demonstrated.

The provider documents adjusted equity history and possible source-specific
differences around corporate actions. These claims should inform an explicit,
content-bound production data acceptance path rather than an unimplemented
requirement for a VERIFIED flag:

- https://www.vnstocks.com/docs/vnstock/thi-truong-market
- https://www.vnstocks.com/docs/vnstock/chuyen-doi/tu-vnstock-data-3x

Next steps remain source/adjustment acceptance with honest evidence, model
publication, snapshot construction, a real customer analysis request through
the UI/API, and verification of the complete persisted report and daily quota.
New-account Google signup and automatic Free grant also require end-to-end
production evidence; existing profile/Free implementation alone is insufficient.

## Adjustment audit implementation

Added `verify_adjusted_prices` and provider `adjustment_evidence`. The audit uses
explicit VCI adjusted OHLC fields (VND), converts aware trading timestamps to
Vietnam session dates, requires every saved session, rejects duplicate dates,
wrong instruments/sources and nonfinite prices, and tolerates only 0.000001 VND
floating-point noise. It returns checksums and scope without modifying data.
Evidence explicitly says this is provider-series consistency, not independent
corporate-action reconstruction. No production readiness gate has been bypassed.

The live endpoint returned only 1,000 rows for a limit of 20,000. Therefore the
provider method uses disjoint 500-calendar-day windows through the shared rate
limiter; missing sessions still fail verification. Eight focused tests passed.

A production FPT audit fetched four windows (345/338/341/274 rows), covering all
1,298 stored sessions from 2021-08-03 through 2026-10-16. All four OHLC fields
matched explicit adjusted prices; maximum difference was 7.275957614183426e-12
VND. Evidence is in server release `adjustment-FPT.log`. This one-symbol result
does not certify the entire training manifest or authorize model publication.

The full-manifest audit was started on production with the committed audit
implementation loaded in the diagnostic process, without replacing running
application services. Output: `adjustment-manifest.jsonl` in the same release.
At the first 24 processed stocks, 5 matched and 19 differed. This is not a final
acceptance result. AAH was investigated separately: over 443 common sessions in
2025-2026, close differed on 416 sessions (up to 296 VND), while isolated open
differences reached 400 VND. These are not floating-point noise. Endpoint price
semantics/source differences must be resolved before interpreting the batch as
an exclusion list; no stored candles or eligibility flags were changed.

## Acceptance policy correction

Stopped the diagnostic batch deliberately after identifying incompatible close
semantics. AAH's recent OHLCV close equals raw trading close; the nominal
adjusted-close field instead approximates average trading price. An exact
cross-endpoint match must not become a production publication gate.

Quant now accepts the pinned Vnstock 5.0.3 OHLCV contract with content-bound
source/unit/OHLC/session evidence already checked by ingestion. This matches the
prototype's use of provider-adjusted OHLCV. Fixtures, unsupported versions,
uncertified input, altered content and provisional data remain rejected.
No provenance or adjustment flags are rewritten. Reports retain the warning
that corporate-action adjustment has not been independently audited. Model
evidence records PROVIDER_OHLCV_CONTRACT scope and the acceptance policy version.
The legacy `live_data_validated` boolean denotes accepted source evidence;
it does not assert an independent reconstruction of corporate actions.

Snapshot publication also previously had an agent-added 80% whole-universe
coverage default. Removed that default to match the same availability policy as
screening: process available histories, record exclusions and actual coverage,
and retain the minimum five usable stocks required for cross-sectional ranking.
Explicit callers can still request a coverage bound. The snapshot test now
publishes at 6/8 coverage and verifies missing-stock exclusion; four usable
stocks still fail without replacing the published snapshot.

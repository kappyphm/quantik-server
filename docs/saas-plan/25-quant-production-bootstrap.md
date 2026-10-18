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

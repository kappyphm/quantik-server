# Quant request integration

## Admission and read contracts

Authenticated customers use GET analyses/options?target_session=YYYY-MM-DD to
obtain completed exchange sessions, eligible symbol codes and readiness/reason.
The calendar must cover today; sessions become selectable after16:00 Vietnam
time. POST analyses validates the same requirements before daily admission.
It requires an active subscription, validated completed data and a usable
published model. A missing snapshot additionally requires an enabled snapshot
service in OPERATIONS. Rejected input/readiness never consumes daily admissions.
Existing idempotent requests remain readable/replayable when readiness changes.

GET analyses lists the customer's jobs newest first with a stable timestamp/ID
cursor. Job responses include created_at, deadline and attempt. Report responses
include target_session and data_observed_at. New data_as_of values represent the
daily data date at00:00UTC; target_session is the authoritative exchange date,
data_observed_at is the collection timestamp and evaluated_at is execution time.
Historical reports may lack the additional metadata. No analysis cancellation
or quota-restoring action is exposed.

## Dependencies and model selection

Shared dependencies check validated manifest/model readiness before computing.
They use120-second leases and20-second heartbeats, fence snapshot effects and
respect OPERATIONS snapshot pauses. Expired leases can be recovered before the
customer job's24-hour deadline. A dependency without live waiters closes. Changes
to source data or models mark running dependencies dirty for another pass.
READY requires a ready snapshot and, under strict policy, a model.

Snapshot model selection searches published model history, matching the pinned
configuration, cutoff no later than the requested session and age under28days
at execution. JSON-normalized config hashes avoid tuple/list comparison drift.
CLI/scheduled training and snapshots retain the same data-verification gates.

## Data acceptance still open

### Research charts for the prototype result UI

New reports include `research` (chart schema_version1, symbol, VND/pct units).
It contains lineage (observations/start/end), horizon_sessions, simulation_count,
cone (session1 is the first simulated future session; p05/p25/p50/p75/p95),
distribution (bin lowerPct/upperPct, midpoint returnPct, count, probabilityPct),
dated HMM regime labels with their matching closing prices, historical drawdown,
normalized factor_details.z with explicit [-1,1] domains, and simulation risk
metrics. Monte Carlo VaR/CVaR are identified separately from historical metrics.

`simulation_summary` aggregates the exact raw paths before snapshot serialization
removes `_mc_paths`. Histogram bin count is bounded at60. Stored chart aggregates
survive snapshot serialization and report response validation; report assembly
does not simulate again. The quant engine itself is unchanged. The recipe is now
`prototype-v8-research`, so new work rebuilds snapshots with these aggregates
instead of reusing snapshots that lack them. No old-report migration is required.

This restores browser-rendered research charts without reintroducing the removed
quant_visuals/Matplotlib image-export pipeline, backtest, Kelly or meta-label.
Reference UI: kappyphm/quantik-react commit40166c6.

### Market acceptance

The current Vnstock adapter certifies screening contracts only. It does not
independently verify adjustment or turn existing histories into LIVE_VALIDATED.
Provider documentation says equity OHLCV is adjusted, but this alone does not
constitute the application's independent Quant acceptance evidence.
Reference: https://www.vnstocks.com/docs/vnstock/thi-truong-market

No fixture/degraded setting is enabled for normal operation. Synthetic model,
data and fitting fixtures in temporary test databases verify control flow only.
Live acceptance still requires a complete verified market manifest, a model
passing training validation and a real customer report. Source/UI integration
must not be described as that live acceptance.

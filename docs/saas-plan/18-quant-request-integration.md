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

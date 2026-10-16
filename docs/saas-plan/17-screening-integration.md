# 17 — Screening integration and live data contract

Implemented16/10/2026. Shared screenings are read-only subscription benefits: both latest/results require an enabled authenticated user and an active Subscription whose immutable plan has screening=true. Expired, queued-only or absent subscriptions return403 SCREENING_SUBSCRIPTION_REQUIRED. Reading does not create jobs, buckets, reservations or daily admissions.

## Provider contract validation

The pinned Vnstock5.0.1 adapter now uses VCI for equity and VNINDEX histories. Live local probes authenticated the configured Golden account, fetched a1519-member universe and read VNINDEX/FPT historical bars. KBS index returned VNSTOCK_SOURCE_UNAVAILABLE; VCI returned both. Equity source prices remain thousandVND normalized toVND; index remains points. These probes are not a full-market run or proof of corporate-action adjustment.

`screening_validation.py` checks normalized provider/version/source/units, ordered finite OHLCV, candle bounds, integer nonnegative share volume and target session. The evidence binds the normalized content checksum and scope SCREENING_ONLY to policy vnstock-5.0.1-screening-contract-1. The normalization/readiness checks still reject unavailable target sessions, provider caps, wrong modes and invalid AM/PM observation times.

The adapter attaches this screening-only evidence after a successful provider response and normalization. It **does not** relabel LIVE_UNVERIFIED as LIVE_VALIDATED, set adjustment=VERIFIED or unlock Quant/training gates. Screening accepts the scoped evidence and publishes source_mode=LIVE_SCREENING_VALIDATED, validation_scope=SCREENING_ONLY; adjustment warnings remain visible in individual results. Existing fully validated data remains supported. Fixtures remain blocked unless explicitly enabled in a development/test service.

Changed content invalidates the certificate. Reusable ingestion caches need valid screening evidence or fully validated provenance; older uncertified entries are fetched again. Structural/source contract checks are not independent exchange reconciliation or scientific signal validation.

## Pipeline and publication

Scheduler → INGEST → immutable universe/history/manifest → SCREEN → atomic results/run/published pointers. No LightGBM training or market snapshot is required for screening. The schedule,95% coverage threshold,21 tri-state criteria, rankings, lease fencing and deadlines are unchanged.

Per-symbol data-not-ready errors now continue to the next symbol. A mandatory VNINDEX failure or shared provider/budget outage stops the pass. Resume fetches missing histories only. A single suspended symbol cannot starve later symbols; a partial pass below95% cannot publish.

Latest still returns503 SCREENING_NOT_READY before first publication, with latest_attempt and Retry-After60. Published results include refresh guidance and schedule metadata. Errors do not replace the last published READY run. Results are immutable and pagination remains pinned to run_id.

Admin operations/health now includes published screening date/slot/time/coverage, latest attempt, operation switches and environment provider gate. Existing process health reports scheduler and schedule-worker:INGEST/SCREEN heartbeats. FE surfaces these on scheduled tasks.

## Running locally and deploying

```powershell
& ./deploy/local/start-screening.ps1 -EnableProvider
```

The helper uses the shared venv, starts hidden scheduler/INGEST/SCREEN processes, avoids starting duplicate matching commands and writes logs under runtime/screening-logs. It is a local development helper, not Windows service supervision; run again after machine restart. Process IDs can be found by command line. Provider ingestion additionally needs active OPERATIONS.enabled and ingestion_enabled; screening needs screening_enabled. Pause by deactivating the corresponding operation gates; secrets remain in the existing ignored configuration.

Compose ingest/screen are now default services alongside scheduler. Snapshot/training remain optional profiles. Applying this source change requires a new release; no VPS deployment was performed in this task. Verify credentials/device slot and resource limits independently on the deployment host. Existing trial deployment evidence is historical.

Local configuration was explicitly activated with audit: enabled=true, ingestion_enabled=true, screening_enabled=true; QUANTIK_SCHEDULED_PROVIDER_ENABLED=true in ignored .env. Scheduler, INGEST and SCREEN were started and fresh RUNNING heartbeats checked. Missed AM tasks retain failures, not fabricated publications. At implementation time the next usable window was PM16:00–17:30 Vietnam time on16/10/2026. No full-market live publication has yet been verified; current local UI may correctly show no publication until a successful eligible slot.

## Validation performed

-6screening/scheduling tests passed, including real run orchestration/API over a temporary DB, subscription denial/expiry, quota unchanged, screening-only certificates, tamper/unit rejection and fixture boundary.
-3observability tests were exercised:2passed initially; the affected admin health test failed on a missing settings reference, was fixed and passed on targeted rerun.
-After the per-symbol ingestion change, its focused budget/resume test passed, verifying processing continues after an unavailable symbol and does not refetch completed symbols.
-Live Vnstock login/universe/VNINDEX/FPT probes passed with VCI; no source/key/token printed. Full1519-symbol ingestion, AM/PM live publication and production-host restart remain operational acceptance checks.
-FE production + proxy + FastAPI + SQLite browser check passed at1440px/390px against isolated synthetic fixtures. It verifies actual auth cookies, two result routes, run IDs, criteria, no overflow/JS errors and created_today=0/remaining20. This is not a live-market acceptance claim.

No schema migration is required. No payment/user records were changed for screening QA in the operational database. Temporary browser-test accounts/orders/payments exist only inside a disposable test database.

## Completed-session backfill

Admin can request a completed daily session without changing automatic slot deadlines:

- `GET /api/v1/admin/screening/backfill-options`: last 60 completed sessions from the active exchange calendar, operation gates and latest backfill task.
- `POST /api/v1/admin/screening/backfills`: optional `target_session` (defaults to latest completed), mandatory reason and `Idempotency-Key`. Returns an asynchronous task with HTTP202.
- `GET /api/v1/admin/scheduled-tasks/{id}`: persisted status, attempts, next retry, deadline and reason.

Only sessions at or after16:00 Vietnam time are eligible. This path uses COMPLETED_DAILY data; it cannot reconstruct missing morning observations. The active calendar must cover today. Both ingestion/screening operation switches and the environment provider gate must be enabled. A single pending/running backfill is permitted; repeated requests for the same session share a task. The audit log records actor, reason, request identity and previous failure/attempt count. Replaying the same key never restarts a failed task; an explicit new request can restart it with a fresh bounded deadline.

The existing SCREEN worker consumes BACKFILL tasks, first resuming ingestion then screening. No additional worker service is required. SCREEN/BACKFILL acquire INGEST then SCREEN lanes, excluding overlapping ingestion or screening. Heartbeats renew both lanes; leases and active operator gates fence all data/publication effects. Backfills yield at guarded boundaries when an automatic ingest/screen task is due, then resume on retry; saved histories are retained. Provider budgets/cooldowns remain shared. Retry and total processing time use the pinned OPERATIONS policy. Missing coverage, provider failure or expiry do not publish partial results. Existing READY runs are reused without refetching or recomputing.

Screening permits an extended deadline only for a guarded BACKFILL task matching the completed session. The actual execution/publication time is retained and summary.execution_mode=BACKFILL identifies a late run. ALL/PM pointers advance only to an equal or newer session, preserving newer publications. Automatic deadlines and AM observation validation remain unchanged. This uses existing task/audit tables; no migration is required.

Verification: focused scheduling/screening checks and an isolated HTTP→worker→real screening computation test cover admin authorization, incomplete-session denial, idempotency conflict, shared lanes, heartbeat, monotonic publication, provider failure, operator pause, expiry and explicit retry. Provider I/O in that test is synthetic, confined to its temporary database. Live provider status must be reported separately.

# 12 — Versioned operations and durable exchange scheduling

Implemented under delegated operational decisions. Source/model validity gates remain mandatory;
scheduling an operation does not prove live data, model publication, broker durability or VPS acceptance.

## Configuration and calendar

Immutable OPERATIONS and TRADING_CALENDAR versions have canonical SHA-256 IDs. Admin creates validated
versions and activates/rolls back using expected_version plus reason. Active pointer changes lock/CAS;
stale requests conflict rather than overwrite. Audit records actor/reason/before/after. Unknown operation
fields, invalid intervals, duplicate/out-of-range sessions and malformed source references are rejected.
Fixture calendars cannot activate/run in production. Hashes are rechecked when read/used.

`quantik seed-operations` requires a seeded enabled admin, initializes missing active pointers only, and
preserves existing operator configuration. Default policy enables screening/snapshots/weekly training;
provider ingestion is disabled. Retry defaults 300 seconds, max36 attempts, execution timeout8h, 1900-day
history request. Numerical/scientific settings remain in their existing QuantConfig versions; these controls
do not silently change criteria, model validation, prices or job charging.

Bundled calendar covers 2026 only: 248 sessions, standard weekdays minus published exchange closures.
Source review includes the Jan2 update and the Jan10/Aug22 non-trading make-up Saturdays. References:
[HOSE 2026](https://www.hsx.vn/vi/tin-tuc/hose-thong-bao-lich-nghi-giao-dich-nam-2026/2437411),
[HNX annual calendar](https://portal.hnx.vn/vi-vn/chi-tiet-lich-nghi-gd-60021971.html),
[HNX New Year update](https://www.hnx.vn/vi-vn/chi-tiet-lich-nghi-gd-60022084.html).
The scheduler does not extrapolate an unverified next year. CALENDAR_OUT_OF_RANGE requires a new reviewed
calendar version. Admin can import coverage up to three years with explicit ordered session dates.

## Durable scheduling

All schedule times use Asia/Ho_Chi_Minh; DB instants are Unix UTC seconds, target sessions remain dates.

| Operation | Trigger | Deadline |
|---|---|---|
| AM ingestion, if enabled | Session11:30 | Session12:55 |
| AM screening | Session11:30 | Session12:55 |
| PM screening | Session15:00 | Session17:30; completed observations still require16:00 |
| PM ingestion, if enabled | Session16:00 | Next calendar day05:00 |
| Completed snapshot | Session16:10 | Next calendar day06:00 |
| Weekly training | Sunday18:00, cutoff last actual session of that ISO week | Monday06:00 |

Tasks are unique by kind/session/slot, pin operation/calendar versions, and persist scheduled/deadline/
retry/attempt state. Initial catch-up covers seven calendar days; a persisted cursor thereafter recovers
every missed date in bounded31-day transactions. Config/calendar changes re-plan the recent seven days
without duplicating existing tasks. No weekend/holiday screening. A partial end-of-calendar week is not
extrapolated into an uncovered Sunday; import the next calendar in advance, preferably with overlap.

Global DB lanes permit one normal executing INGEST/SCREEN/SNAPSHOT/TRAIN each across replicas. Worker
claims use leases120s, heartbeat20s, finite attempts, retry-after and deadline checks. Expired leases requeue;
old tokens cannot finalize or publish after replacement. Completed tasks retain their result on redelivery.
Claim and heartbeat lock lane→task; finish releases the lane in a separate transaction to avoid inversion.
Lease fencing is not a promise to kill an already-running numerical/native function.

Effects recheck task ownership and current operator gates in their commit transaction: screening/snapshot
publication, candidate persistence/promotion, ingestion saves and manifest publication. Active calendar
corrections removing a session prevent its old task from publishing. Pause prevents new claims and stops
future guarded effects, without modifying historical task versions/results. Missed screening slots create
explicit failed run metadata and preserve published latest. Calendar-aware freshness compares the latest
publication with the last expected slot deadline, avoiding age-only weekend/holiday stale flags. Without
calendar coverage, the API discloses calendar status and retains the age fallback.

## Provider and scientific gates

Automatic provider calls require BOTH operation ingestion_enabled=true and environment
QUANTIK_SCHEDULED_PROVIDER_ENABLED=true. The environment flag is persisted false while the user waits
for Vnstock device-slot resolution. No login was retried during this implementation. Manual ingest CLI is
an explicit operator action and is not invoked by this pause configuration.

Scheduled ingestion resumes already-normalized immutable symbol histories with the same/longer requested
window and enough bars; AM cache must have lunch-window observations. Checksum and live-source provenance
are checked for reuse. A budget failure resumes missing symbols instead of repeatedly refetching the first
symbols; provider retry-after is retained. Manual ingestion defaults to refresh, allowing correction scans.
Manifest assembly projects only headers/metadata rather than decoding all historical bars into RAM.

Training/snapshot workers wait before importing heavy engines unless a completed manifest passes live
universe/provenance/adjustment gates (fixture exception only explicit development/test setting). Snapshot
also waits for a published non-future model younger than28days. Training retains mature-label/temporal
validation; auto-promote only attempts accepted candidates through the existing live evidence gate.
Rejected candidates retain the previous published model. Component caches/immutable writes support retry.

## Commands and API

Activate the shared venv first. Actual supported commands:

```powershell
quantik migrate
quantik seed-operations
quantik scheduler --once
quantik scheduler
quantik schedule-worker --kind SCREEN
quantik schedule-worker --kind SNAPSHOT
quantik schedule-worker --kind TRAIN
# Only after provider pause is lifted and both gates are enabled:
quantik schedule-worker --kind INGEST
```

Processes have explicit graceful shutdown; deployment supervision is still pending. Worker `--once` is
supported for bounded operation. Idle control/operation workers do not load NumPy/Pandas/LightGBM.

Admin JWT endpoints: POST /configs/{OPERATIONS|TRADING_CALENDAR}/versions with payload;
GET /configs/{kind}; POST /configs/{kind}/{id}/activate with expected_version (null initially) + reason;
GET /scheduled-tasks with bounded cursor/status/kind filtering. Prefix /api/v1/admin applies to all.
OPERATIONS GET also reports the environment provider switch for operator diagnosis.

Focused checks passed calendar closures/holiday freshness, unique slots/restart recovery, lane exclusion,
lease replacement/stale fencing, CAS rollback, corrected calendar, Sunday cutoff, provider-pause/readiness
waiting and resumable ingestion using a stub provider. Admin config API validation/auth/activation and
existing screening publication checks passed. Local migration/seed/scheduler/provider-disabled worker
commands ran successfully. PostgreSQL parallel races, real scheduled market/model run and VPS restart
acceptance remain unverified. No Docker resources were created.

# 13 — Local PostgreSQL/RabbitMQ and restore checks

The user asked to continue local deployment while preparing the server. These checks use real infrastructure
with explicit synthetic fixtures; they do not prove live Vnstock/Google/SePay, production hardening or VPS load.

## Isolated runner

`backend/tools/run_infrastructure_checks.py` uses existing image tags/IDs supplied explicitly. It never pulls
or builds an image. Each invocation creates two uniquely labelled temporary containers, random credentials,
loopback-only random published ports, 1CPU/512MiB limits each and tmpfs data (Postgres256MiB, Rabbit128MiB).
No named/anonymous volume is created. Test and snapshot schemas are isolated from the local SQLite app.
The existing `quantik-*` containers belong to another repository and were left untouched.
Existing image IDs used: Postgres144950fc417143e9cb6b561a4d141a9f582700f442d0713c722a3c39a3d5b5ff;
RabbitMQd60811676b7caa2286c241ce54c3db4eaf6ca947685a032d5224f41fba05a33f (both sha256).

Run from the repository root with the shared venv activated:

```powershell
python backend/tools/run_infrastructure_checks.py --postgres-image EXISTING_POSTGRES_IMAGE --broker-image EXISTING_RABBITMQ_IMAGE
```

Replace the two image arguments with inspected existing tags or SHA-256 IDs. Missing images fail rather
than download. The runner checks readiness, migrates isolated schemas, runs focused real-infrastructure
tests, dumps/restores into a separate DB and compares every application table's row count/content hash
plus Alembic revisions. Temporary containers/credentials are removed in finally after success/failure.
Ownership labels are verified before deletion; unrelated resources are never pruned. If Docker cleanup
cannot be confirmed, the command reports owned names requiring inspection rather than claiming cleanup.
Only a non-secret summary/fingerprints persist in runtime/infrastructure-check/latest-result.json.

The health probe and stop_app/start_app execute as RabbitMQ's OS user. A root probe during initialization
was observed to create a root-only Erlang cookie, preventing startup; the runner now avoids that race and
reports terminated-container logs immediately rather than waiting on a missing process.

## Covered behavior

- Concurrent admissions enforce active limit, one idempotent job/reservation and exact bucket balances.
- Completion versus cancellation produces one terminal settlement/report effect.
- Concurrent duplicate SePay IDs and independent transfers to one order create one entitlement; excess
  transfers remain in reconciliation. Parallel refunds from multiple admins credit once with one ledger entry.
- Competing configuration CAS activations choose one winner; simultaneous scheduler ticks keep unique
  tasks; two runnable tasks still claim only one global lane. Heartbeat/finish run against PostgreSQL locks.
- Actual RabbitMQ publisher confirms, durable delivery, duplicate processing through AnalysisWorker,
  acknowledgements, dead-letter routing and persistent message survival across Rabbit application restart.
- Snapshot/report API→outbox→real broker→actual AnalysisWorker→report/quota, with injected error after
  successful publish before DB commit. Redelivery keeps one report/consume. The baseline is deliberately a
  fixture; this validates persistence/transport/integrity, not full numerical or live model correctness.
- Custom pg_dump/pg_restore into an empty separate database, with exact restored table-content fingerprints
  in both schemas. This is a local restore check, not off-VPS backup or an established RPO/RTO.

## JSONB integrity regression

The first real PostgreSQL snapshot check failed on rereading READY cache: JSONB preserves JSON number
values but can change their representation/types (signed zero, exponent notation to integer).
The previous hash used Python's textual JSON number representation. The regression is reproduced with a
real JSONB round trip; canonical hashing treats mathematically identical JSON numbers equally while keeping
booleans distinct, rejecting nonfinite/ambiguous objects and detecting one-ULP metric changes.

New snapshot recipe market-snapshot-beta-3 uses a streamed decimal-token hash for dependency/root/member/
reference integrity, with no rounding of payload values and no numerical-engine changes. Legacy beta2
snapshots still use their original hash when read; failed legacy integrity is not bypassed. A new builder
uses a new immutable dependency key. History/model/config identity hashes are unchanged.
Upgrade builders/workers together after draining old builds; mixed beta2/beta3 binaries are not a supported
rolling deployment. Existing persisted customer reports remain intact.

References: [PostgreSQL JSON numbers](https://www.postgresql.org/docs/17/datatype-json.html),
[pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[RabbitMQ CLI authentication](https://www.rabbitmq.com/docs/cli).

Final local rerun passed: five real-infrastructure tests, including the JSONB regression and full fixture
snapshot/report flow, plus pg_dump/pg_restore exact table-content comparison. The focused canonical-hash
unit check also passed. Restore compared80 application table fingerprints across two schemas (206 rows).
The final run took61.96seconds including startup/testing/restore. Cleanup found zero check containers and
the same three pre-existing volumes/four images; no build/pull/prune was performed. Full evidence scope
remains local fixtures as stated above.

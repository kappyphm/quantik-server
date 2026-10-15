# 11 — Admin customer controls and quota corrections

Implemented operational subset of FR-10 under delegated implementation decisions. General versioned
runtime configuration/rollback and financial bank refunds remain separate work.

## Explicit mutations

POST /api/v1/admin/users/{id}/state accepts enabled + a 10–500 character reason. Only customer accounts
are eligible: this cannot disable an administrator or edit roles/passwords. Account state is locked and
audited with actor/reason/before/after. Disabling revokes all refresh sessions; existing JWTs fail immediately
through the existing account/session checks. Re-enabling requires fresh login; old sessions remain revoked.

Suspension commits without locking jobs, avoiding a lock-order inversion against worker completion.
The existing reconciler now sweeps up to 100 nonterminal jobs of disabled accounts each iteration, fences
execution and releases reservations with ACCOUNT_DISABLED. This includes work already computing;
security stop is a release, distinct from voluntary user cancellation. Worker begin/complete already reject
disabled accounts; stale tokens cannot complete after reconciliation. A completion committed before the
account suspension keeps its original settlement. No process kill is claimed; executor records actual stop.

POST /api/v1/admin/jobs/{id}/quota-refund requires Idempotency-Key, reason and evidence. Only terminal
jobs with CONSUMED reservation can receive one unit. A compensating REFUND ledger entry and QuotaRefund
record are appended; original job/reservation/settlement history remains unchanged. Consumed decreases
once in the original bucket, never below zero. Expired bucket refunds update history, not a new period.
Repeated same key/body returns the same refund; changed body/job conflicts; another key/admin cannot
refund the same reservation twice. Account of the admin serializes keys; job/reservation locks and DB unique
constraints serialize the one-refund rule. All effects and audit commit/rollback together.

This action restores analysis quota only; it does not initiate a money transfer or mark a bank refund done.
Customer job responses expose quota_refund_id alongside original quota_effect; usage reports net consumed.

## Bounded operational views

Admin JWT is required for GET /admin/users, /jobs, /payments, /subscriptions, /audit, /models, /data-health.
Lists use bounded cursor pages; jobs/subscriptions can filter account, jobs/payments status, audit action.
No password hashes, tokens, model artifacts, raw bank descriptions or full market histories are returned.
Data health shows latest manifest, screening state and dependency counts; presence of a manifest does not
certify live provenance/price adjustments. Model publication still uses the existing validated CLI gate.
Plan availability audit now records before/after in addition to actor/version.

## Evidence and remaining gates

Focused temporary migrated-SQLite API check passed: role rejection, refund idempotency/body conflict,
single-refund ledger, customer visibility, expired-period isolation, rollback before commit, account/session
revocation, queued/running security stop/release, stale-token fencing, re-enable requiring login and all
operational read routes/audit fields. Existing focused job lifecycle test also passed after the sweep change.
Real PostgreSQL parallel races and multi-process acceptance remain unverified.

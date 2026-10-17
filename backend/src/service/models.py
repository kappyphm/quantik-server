from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from datetime import date
from sqlalchemy.orm import Mapped, mapped_column

from service.db import Base


def new_id() -> str:
    return str(uuid4())


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("role IN ('user', 'admin')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str | None] = mapped_column(String(255), unique=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    phone_number: Mapped[str | None] = mapped_column(String(16))
    profile_required: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    __table_args__ = (UniqueConstraint("token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    previous_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[int] = mapped_column(Integer)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class UsedRefreshToken(Base):
    __tablename__ = "used_refresh_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("refresh_sessions.id"), index=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    action: Mapped[str] = mapped_column(String(100), index=True)
    occurred_at: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class OAuthFlow(Base):
    __tablename__ = "oauth_flows"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(16), default="STARTED")
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    exchange_hash: Mapped[str | None] = mapped_column(String(64), unique=True)


class QuotaBucket(Base):
    __tablename__ = "quota_buckets"
    __table_args__ = (CheckConstraint("quota_total >= 0 AND consumed >= 0 AND reserved >= 0"),
        CheckConstraint("consumed + reserved <= quota_total"),
        CheckConstraint("active_jobs >= 0 AND active_limit > 0"),
        UniqueConstraint("subscription_id", "valid_from"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    valid_from: Mapped[int] = mapped_column(Integer)
    valid_until: Mapped[int] = mapped_column(Integer)
    quota_total: Mapped[int] = mapped_column(Integer)
    consumed: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    active_limit: Mapped[int] = mapped_column(Integer)
    entitlement_version: Mapped[str] = mapped_column(String(100))
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey("subscriptions.id"))
    daily_created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key"),
        CheckConstraint("status IN ('QUEUED','WAITING_DATA','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    bucket_id: Mapped[str] = mapped_column(ForeignKey("quota_buckets.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    target_session: Mapped[date] = mapped_column(Date)
    inputs: Mapped[dict] = mapped_column(JSON)
    pinned_versions: Mapped[dict | None] = mapped_column(JSON)
    policy: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    created_at: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[int] = mapped_column(Integer)
    ready_at: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int | None] = mapped_column(Integer)
    compute_started_at: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(100))
    evaluated_at: Mapped[int | None] = mapped_column(Integer)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt_number"),)
    token: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[int] = mapped_column(Integer)
    stopped_at: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))


class Inbox(Base):
    __tablename__ = "inbox"
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox.id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    completed_at: Mapped[int] = mapped_column(Integer)


class DependencyRequest(Base):
    __tablename__ = "dependency_requests"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    next_attempt_at: Mapped[int] = mapped_column(Integer)
    lease_until: Mapped[int] = mapped_column(Integer, default=0)
    execution_token: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str | None] = mapped_column(String(100))
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)


class DependencyWaiter(Base):
    __tablename__ = "dependency_waiters"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    dependency_key: Mapped[str] = mapped_column(ForeignKey("dependency_requests.key"), index=True)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (CheckConstraint("state IN ('RESERVED','CONSUMED','RELEASED')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    bucket_id: Mapped[str] = mapped_column(ForeignKey("quota_buckets.id"))
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (UniqueConstraint("reservation_id", "effect"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    effect: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(100))


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    event: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[int | None] = mapped_column(Integer)


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class HistoryRevision(Base):
    __tablename__ = "history_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    target_session: Mapped[date] = mapped_column(Date)
    input_mode: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)


class UniverseVersion(Base):
    __tablename__ = "universe_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)


class DataManifest(Base):
    __tablename__ = "data_manifests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    universe_id: Mapped[str] = mapped_column(ForeignKey("universe_versions.id"))
    target_session: Mapped[date] = mapped_column(Date, index=True)
    input_mode: Mapped[str] = mapped_column(String(24))
    histories: Mapped[dict] = mapped_column(JSON)
    coverage: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)
    revision_order: Mapped[int] = mapped_column(Integer, server_default="0")


class MarketRevisionClock(Base):
    __tablename__ = "market_revision_clock"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)


class ProviderGate(Base):
    __tablename__ = "provider_gates"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    cooldown_until: Mapped[int] = mapped_column(Integer, default=0)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(ForeignKey("provider_gates.name"), index=True)
    occurred_at: Mapped[int] = mapped_column(Integer, index=True)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("data_manifests.id"))
    cutoff_session: Mapped[date] = mapped_column(Date)
    created_at: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON)
    artifact: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)


class PublishedModel(Base):
    __tablename__ = "published_models"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))


class ComponentResult(Base):
    __tablename__ = "component_results"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(JSON)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dependency_key: Mapped[str] = mapped_column(String(64), unique=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("data_manifests.id"))
    model_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))
    target_session: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    payload_checksum: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(100))


class PublishedSnapshot(Base):
    __tablename__ = "published_snapshots"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"))


class ScreeningRun(Base):
    __tablename__ = "screening_runs"
    __table_args__ = (UniqueConstraint("target_session", "slot"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_session: Mapped[date] = mapped_column(Date)
    slot: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(20))
    manifest_id: Mapped[str | None] = mapped_column(ForeignKey("data_manifests.id"))
    policy: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[int | None] = mapped_column(Integer)
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(100))
    summary: Mapped[dict | None] = mapped_column(JSON)


class ScreeningResult(Base):
    __tablename__ = "screening_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("screening_runs.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    overall_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    strong_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    accumulation_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    distribution_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class PublishedScreening(Base):
    __tablename__ = "published_screenings"
    name: Mapped[str] = mapped_column(String(8), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("screening_runs.id"))


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (CheckConstraint("price_vnd >= 0 AND duration_days > 0 AND analysis_quota > 0 AND active_limit > 0", name="ck_plan_terms"),
        UniqueConstraint("created_by", "creation_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    price_vnd: Mapped[int] = mapped_column(Integer)
    duration_days: Mapped[int] = mapped_column(Integer)
    analysis_quota: Mapped[int] = mapped_column(Integer)
    active_limit: Mapped[int] = mapped_column(Integer)
    features: Mapped[dict] = mapped_column(JSON)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    creation_key: Mapped[str | None] = mapped_column(String(128))
    creation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    replaces_plan_id: Mapped[str | None] = mapped_column(ForeignKey("plan_versions.id"))


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key"),
        CheckConstraint("amount_vnd > 0"),
        CheckConstraint("status IN ('PENDING','PAID','EXPIRED','CANCELLED')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    reference: Mapped[str] = mapped_column(String(32), unique=True)
    amount_vnd: Mapped[int] = mapped_column(Integer)
    bank_code: Mapped[str] = mapped_column(String(32))
    bank_account: Mapped[str] = mapped_column(String(64))
    account_name: Mapped[str] = mapped_column(String(128))
    transfer_content: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    paid_at: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


class PurchaseIntent(Base):
    __tablename__ = "purchase_intents"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"))


class BankPayment(Base):
    __tablename__ = "bank_payments"
    __table_args__ = (UniqueConstraint("bank_transaction_key"),
        CheckConstraint("amount_vnd > 0"))
    # SePay's stable transaction ID is global across replay/retry.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bank_transaction_key: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    bank_code: Mapped[str] = mapped_column(String(32))
    bank_account: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(4))
    amount_vnd: Mapped[int] = mapped_column(Integer)
    transaction_at: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[int] = mapped_column(Integer)
    references: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(100))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("(order_id IS NOT NULL AND payment_id IS NOT NULL AND grant_key IS NULL) OR (order_id IS NULL AND payment_id IS NULL AND grant_key IS NOT NULL)", name="ck_subscription_origin"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.id"), unique=True)
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("bank_payments.id"), unique=True)
    grant_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    bucket_id: Mapped[str] = mapped_column(ForeignKey("quota_buckets.id"), unique=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"))
    valid_from: Mapped[int] = mapped_column(Integer)
    valid_until: Mapped[int] = mapped_column(Integer)


class QuotaRefund(Base):
    __tablename__ = "quota_refunds"
    __table_args__ = (UniqueConstraint("actor_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(500))
    evidence: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[int] = mapped_column(Integer)


class RuntimeConfigVersion(Base):
    __tablename__ = "runtime_config_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))


class ActiveRuntimeConfig(Base):
    __tablename__ = "active_runtime_configs"
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("runtime_config_versions.id"))


class SchedulerCursor(Base):
    __tablename__ = "scheduler_cursors"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    planned_through: Mapped[date] = mapped_column(Date)
    config_id: Mapped[str] = mapped_column(ForeignKey("runtime_config_versions.id"))
    calendar_id: Mapped[str] = mapped_column(ForeignKey("runtime_config_versions.id"))


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (UniqueConstraint("kind", "target_session", "slot"),
        CheckConstraint("status IN ('PENDING','RUNNING','WAITING','SUCCEEDED','FAILED','SKIPPED')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(24))
    target_session: Mapped[date] = mapped_column(Date)
    slot: Mapped[str] = mapped_column(String(4))
    config_id: Mapped[str] = mapped_column(ForeignKey("runtime_config_versions.id"))
    calendar_id: Mapped[str] = mapped_column(ForeignKey("runtime_config_versions.id"))
    scheduled_at: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[int | None] = mapped_column(Integer)
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[dict | None] = mapped_column(JSON)


class ScheduledLane(Base):
    __tablename__ = "scheduled_lanes"
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    execution_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[int] = mapped_column(Integer, default=0)


class ProcessHeartbeat(Base):
    __tablename__ = "process_heartbeats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[int] = mapped_column(Integer)
    last_seen: Mapped[int] = mapped_column(Integer, index=True)
    progress_at: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    error_type: Mapped[str | None] = mapped_column(String(100))


class IntegrationConfig(Base):
    __tablename__ = "integration_configs"
    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    encrypted_payload: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[int | None] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))

"""Vnstock v5 adapter; external I/O stays outside numerical engines."""
import contextlib
import io
import time
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version

from sqlalchemy import func, select, update

from service.market_data import DataError, DailyHistory, normalize_history, normalize_universe
from service.models import ProviderCall, ProviderGate

WINDOWS = {"per_minute": 60, "per_hour": 3600, "per_day": 86400, "per_month": 31 * 86400}


def reserve_provider_call(db, limits: dict[str, int], now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    if set(limits) != set(WINDOWS) or any(not isinstance(n, int) or n <= 0 for n in limits.values()):
        raise DataError("PROVIDER_LIMITS_UNVERIFIED")
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(ProviderGate).values(name="vnstock", cooldown_until=0)
        .on_conflict_do_nothing(index_elements=["name"]))
    gate = db.scalar(select(ProviderGate).where(ProviderGate.name == "vnstock").with_for_update())
    if gate.cooldown_until > now:
        raise DataError("PROVIDER_COOLDOWN", True, gate.cooldown_until - now)
    for key, seconds in WINDOWS.items():
        calls = select(ProviderCall).where(ProviderCall.provider == "vnstock", ProviderCall.occurred_at > now - seconds)
        count = db.scalar(select(func.count()).select_from(calls.subquery()))
        if count >= limits[key]:
            oldest = db.scalar(select(func.min(ProviderCall.occurred_at)).where(ProviderCall.provider == "vnstock",
                ProviderCall.occurred_at > now - seconds))
            raise DataError("PROVIDER_BUDGET_EXHAUSTED", True, max(1, oldest + seconds - now))
    db.add(ProviderCall(provider="vnstock", occurred_at=now))


class VnstockProvider:
    def __init__(self, api_key: str, sessions):
        if version("vnstock") != "5.0.3":
            raise DataError("PROVIDER_VERSION_MISMATCH")
        self.sessions = sessions
        import vnstock
        from vnstock.core.exceptions import VnstockError
        self.library, self.error_type = vnstock, VnstockError
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                account = vnstock.login(api_key)
                usage = vnstock.usage()
        except VnstockError as exc:
            raise self.safe_error(exc) from None
        if not account.get("authenticated"):
            raise DataError("PROVIDER_NOT_AUTHENTICATED")
        self.tier = account.get("tier")
        self.limits = usage.get("limits", {})
        self.market, self.reference = vnstock.Market(), vnstock.Reference()

    @staticmethod
    def safe_error(exc) -> DataError:
        code = getattr(exc, "code", "VNSTOCK_ERROR")
        if not isinstance(code, str) or not code.startswith("VNSTOCK_") or len(code) > 100:
            code = "VNSTOCK_ERROR"
        retryable = type(exc).__name__ in {"NetworkError", "RateLimitedError", "ProviderError", "CircuitOpenError"}
        return DataError(code, retryable, 60 if retryable else None)

    def call(self, operation, *, operation_name: str = "unknown", source: str | None = None, symbol: str | None = None):
        with self.sessions() as db, db.begin():
            reserve_provider_call(db, self.limits)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return operation()
        except self.error_type as exc:
            error = self.safe_error(exc)
            from service.telemetry import emit
            status = getattr(exc, "status", None)
            emit("provider.request_failed", operation=operation_name, source=source, symbol=symbol,
                code=error.code, http_status=status if type(status) is int else None, retryable=error.retryable)
            if error.retryable:
                with self.sessions() as db, db.begin():
                    db.execute(update(ProviderGate).where(ProviderGate.name == "vnstock")
                        .values(cooldown_until=int(time.time()) + (error.retry_after or 60)))
            raise error from None

    def universe(self) -> dict:
        result = normalize_universe(self.call(lambda: self.reference.equity.list_by_exchange(source="vci"),
            operation_name="universe", source="vci"))
        result["provenance"] = "LIVE_UNVERIFIED"
        from service.screening_validation import certify_universe
        return certify_universe(result)

    def history(self, symbol: str, start: date, target_session: date, *, kind: str = "EQUITY",
                input_mode: str = "COMPLETED_DAILY", minimum_bars: int = 120) -> DailyHistory:
        if kind not in {"EQUITY", "INDEX"}:
            raise DataError("INVALID_ASSET_KIND")
        if start > target_session:
            raise DataError("INVALID_DATE_WINDOW")
        # A daily window contains at most one bar per calendar day. Request its
        # full capacity without asking a source for an unrelated 20,000 bars.
        row_limit = min(20000, (target_session-start).days+1)
        source = "vci"
        asset = self.market.equity(symbol) if kind == "EQUITY" else self.market.index(symbol)
        frame = self.call(lambda: asset.ohlcv(start=start.isoformat(), end=target_session.isoformat(),
            interval="1D", limit=row_limit, source=source, floating=None),
            operation_name="daily_history", source=source, symbol=symbol)
        args = dict(symbol=symbol, kind=kind, source=source, target_session=target_session,
            observed_at=datetime.now(timezone.utc), input_mode=input_mode, minimum_bars=minimum_bars,
            provenance="LIVE_UNVERIFIED")
        repairs = []
        try:
            history = normalize_history(frame, **args)
        except DataError as exc:
            if exc.code != "INVALID_OHLC" or input_mode != "COMPLETED_DAILY":
                raise
            secondary_source = "kbs" if kind == "INDEX" else "vnd"
            secondary = self.call(lambda: asset.ohlcv(start=start.isoformat(), end=target_session.isoformat(),
                interval="1D", limit=row_limit, source=secondary_source, floating=None),
                operation_name="history_correction", source=secondary_source, symbol=symbol)
            from service.provider_repair import replace_invalid_ohlc
            repaired, repairs = replace_invalid_ohlc(frame, secondary, secondary_source)
            history = normalize_history(repaired, **args)
        if repairs:
            history.payload["metadata"]["source_corrections"] = repairs
            history.payload["metadata"]["warnings"].append("CANDLES_REPLACED_FROM_CORROBORATED_SOURCE")
        history.payload["metadata"]["requested_start"] = start.isoformat()
        from service.screening_validation import certify_history
        return certify_history(history)

    def adjustment_evidence(self, history: DailyHistory) -> dict:
        """Audit every saved equity candle against explicit VCI adjusted OHLC.

        VCI trade_history can cap a response at 1,000 rows even when a larger
        limit is requested. Disjoint 500-calendar-day windows cannot reach that
        cap for daily records. Each request uses the shared provider budget.
        """
        import pandas as pd
        from service.adjustment_validation import verify_adjusted_prices

        meta = history.payload["metadata"]
        if (meta.get("source") != "vci" or meta.get("kind") != "EQUITY"
                or history.input_mode != "COMPLETED_DAILY" or not history.payload["bars"]):
            raise DataError("ADJUSTMENT_REFERENCE_CONTRACT")
        start = date.fromisoformat(history.payload["bars"][0]["session"])
        frames = []
        while start <= history.target_session:
            stop = min(history.target_session, start + timedelta(days=499))
            frame = self.call(lambda: self.market.equity(history.symbol).trade_history(
                start=start.isoformat(), end=stop.isoformat(), limit=1000, source="vci"),
                operation_name="adjustment_validation", source="vci", symbol=history.symbol)
            if (frame.attrs.get("source") != "vci" or frame.attrs.get("symbol") != history.symbol
                    or any(value for key, value in frame.attrs.items()
                        if key.startswith("tier_cap_") or key == "request_cap_rows")):
                raise DataError("ADJUSTMENT_REFERENCE_CONTRACT")
            frames.append(frame)
            start = stop + timedelta(days=1)
        reference = pd.concat(frames, ignore_index=True)
        reference.attrs = {"source": "vci", "symbol": history.symbol}
        return verify_adjusted_prices(history, reference)

"""Provider-neutral daily history and immutable manifest contracts."""
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from service.models import DataManifest, HistoryRevision, UniverseVersion, new_id

VN_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
InputMode = Literal["COMPLETED_DAILY", "PROVISIONAL_DAILY"]


class DataError(Exception):
    def __init__(self, code: str, retryable: bool = False, retry_after: int | None = None):
        self.code, self.retryable, self.retry_after = code, retryable, retry_after
        super().__init__(code)


def checksum(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class DailyHistory:
    symbol: str
    target_session: date
    input_mode: InputMode
    payload: dict

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.payload["bars"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("session")))
        df.attrs.update(self.payload["metadata"])
        return df


def normalize_history(frame: pd.DataFrame, *, symbol: str, kind: Literal["EQUITY", "INDEX"],
                      source: str, target_session: date, observed_at: datetime,
                      input_mode: InputMode = "COMPLETED_DAILY", minimum_bars: int = 120) -> DailyHistory:
    if observed_at.tzinfo is None:
        raise DataError("OBSERVED_AT_REQUIRES_TIMEZONE")
    local = observed_at.astimezone(VN_TIMEZONE)
    if target_session > local.date() or minimum_bars <= 0:
        raise DataError("INVALID_HISTORY_REQUEST")
    if kind not in {"EQUITY", "INDEX"} or input_mode not in {"COMPLETED_DAILY", "PROVISIONAL_DAILY"}:
        raise DataError("INVALID_HISTORY_MODE")
    if frame is None or frame.empty:
        raise DataError("NO_DATA")
    if not {"time", "open", "high", "low", "close", "volume"}.issubset(frame.columns):
        raise DataError("PROVIDER_SCHEMA")
    if frame.attrs.get("source", source).lower() != source.lower():
        raise DataError("PROVIDER_SOURCE_CHANGED")
    capped = {key: str(value) for key, value in frame.attrs.items()
        if (key.startswith("tier_cap_") or key == "request_cap_rows") and value is not None and value is not False}
    if capped:
        raise DataError("PROVIDER_HISTORY_CAPPED")
    df = frame[["time", "open", "high", "low", "close", "volume"]].copy()
    try:
        stamps = pd.to_datetime(df.pop("time"), errors="raise")
        if stamps.dt.tz is not None:
            stamps = stamps.dt.tz_convert(VN_TIMEZONE)
        df.index = pd.DatetimeIndex(stamps.dt.date)
        df = df.apply(pd.to_numeric, errors="raise")
    except (ValueError, TypeError, AttributeError):
        raise DataError("INVALID_BAR_TYPES") from None
    if df.index.hasnans or df.index.duplicated().any():
        raise DataError("DUPLICATE_OR_INVALID_SESSIONS")
    if (df.index.date > target_session).any():
        raise DataError("FUTURE_BARS")
    df = df.sort_index()
    if input_mode == "COMPLETED_DAILY" and target_session == local.date() and local.hour < 16:
        df = df[df.index.date < target_session]
    if input_mode == "PROVISIONAL_DAILY" and target_session != local.date():
        raise DataError("PROVISIONAL_SESSION_MISMATCH")
    if df.empty or df.index[-1].date() != target_session:
        raise DataError("TARGET_SESSION_NOT_READY", retryable=True)
    if len(df) < minimum_bars:
        raise DataError("INSUFFICIENT_HISTORY")
    values = df.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise DataError("NONFINITE_BARS")
    if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df.volume < 0).any():
        raise DataError("INVALID_PRICE_OR_VOLUME")
    if not np.equal(df.volume, np.floor(df.volume)).all():
        raise DataError("FRACTIONAL_SHARE_VOLUME")
    if ((df.high < df[["open", "close", "low"]].max(axis=1)) |
        (df.low > df[["open", "close", "high"]].min(axis=1))).any():
        raise DataError("INVALID_OHLC")
    # V5 contract: equity OHLC in thousand VND; index OHLC in points.
    # This multiplier comes from the provider contract, never price magnitude.
    if kind == "EQUITY":
        df[["open", "high", "low", "close"]] *= 1000.0
    bars = [{"session": session.date().isoformat(), **{key: float(value) for key, value in row.items()}}
        for session, row in df.iterrows()]
    metadata = {"provider": "vnstock", "provider_version": "5.0.1", "source": source,
        "kind": kind, "price_unit": "VND" if kind == "EQUITY" else "POINTS", "volume_unit": "SHARES",
        "source_price_unit": "THOUSAND_VND" if kind == "EQUITY" else "POINTS",
        "adjustment": "PROVIDER_ADJUSTMENT_UNVERIFIED", "target_session": target_session.isoformat(),
        "input_mode": input_mode, "observed_at": observed_at.astimezone(ZoneInfo("UTC")).isoformat(),
        "warnings": ["CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED"]}
    return DailyHistory(symbol, target_session, input_mode, {"bars": bars, "metadata": metadata})


def normalize_universe(frame: pd.DataFrame) -> dict:
    if frame is None or frame.empty or not {"symbol", "exchange"}.issubset(frame.columns):
        raise DataError("UNIVERSE_SCHEMA")
    members = {}
    for row in frame.to_dict("records"):
        symbol = str(row["symbol"]).strip().upper()
        exchange = str(row["exchange"]).strip().upper()
        exchange = {"HSX": "HOSE", "UPCoM".upper(): "UPCOM"}.get(exchange, exchange)
        if exchange not in {"HOSE", "HNX", "UPCOM"}:
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,16}", symbol) or symbol in members:
            raise DataError("INVALID_OR_DUPLICATE_INSTRUMENT")
        sector = row.get("icb_code_lv2")
        members[symbol] = {"exchange": exchange, "sector": None if pd.isna(sector) else str(sector)}
    if not members:
        raise DataError("EMPTY_UNIVERSE")
    return {"version": "universe-1", "members": dict(sorted(members.items()))}


def persist_immutable(db: Session, model, identity: dict, **fields):
    hashed = checksum(identity)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(model).values(id=new_id(), checksum=hashed, **fields)
        .on_conflict_do_nothing(index_elements=["checksum"]))
    return db.scalar(select(model).where(model.checksum == hashed))


def save_history(db: Session, history: DailyHistory) -> HistoryRevision:
    identity = {"symbol": history.symbol, **history.payload}
    return persist_immutable(db, HistoryRevision, identity, symbol=history.symbol,
        target_session=history.target_session, input_mode=history.input_mode, payload=identity)


def save_universe(db: Session, payload: dict) -> UniverseVersion:
    return persist_immutable(db, UniverseVersion, payload, payload=payload, created_at=int(time.time()))


def publish_manifest(db: Session, universe: UniverseVersion, histories: dict[str, HistoryRevision], *,
                     target_session: date, input_mode: InputMode, minimum_coverage_ratio: float = 0.95) -> DataManifest:
    if not 0 < minimum_coverage_ratio <= 1:
        raise DataError("INVALID_COVERAGE_POLICY")
    members = universe.payload["members"]
    index = histories.get("VNINDEX")
    if not index or index.payload["metadata"]["kind"] != "INDEX":
        raise DataError("INDEX_NOT_READY", retryable=True)
    for symbol, history in histories.items():
        if history.symbol != symbol or history.target_session != target_session or history.input_mode != input_mode:
            raise DataError("MIXED_MANIFEST_VERSIONS")
        if symbol != "VNINDEX" and (symbol not in members or history.payload["metadata"]["kind"] != "EQUITY"):
            raise DataError("MANIFEST_UNIVERSE_CONFLICT")
    available = set(histories) & set(members)
    ratio = len(available) / len(members)
    if ratio < minimum_coverage_ratio:
        raise DataError("MANIFEST_COVERAGE_NOT_READY", retryable=True)
    refs = {symbol: history.id for symbol, history in sorted(histories.items())}
    coverage = {"expected": len(members), "available": len(available), "ratio": ratio,
        "excluded": sorted(set(members) - available), "minimum_ratio": minimum_coverage_ratio}
    identity = {"universe": universe.id, "target_session": target_session.isoformat(),
        "input_mode": input_mode, "histories": refs, "coverage": coverage}
    return persist_immutable(db, DataManifest, identity, universe_id=universe.id, target_session=target_session,
        input_mode=input_mode, histories=refs, coverage=coverage, created_at=int(time.time()))

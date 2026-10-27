"""Screening-only provider contract checks; never certify corporate-action adjustment.

Evidence binds normalized content to the pinned adapter contract. It does not change
LIVE_UNVERIFIED provenance or authorize model training / personalized Quant reports.
"""
from copy import deepcopy
from datetime import date, datetime
import math
import re

from service.market_data import DailyHistory, DataError, checksum

POLICY = "vnstock-5.0.3-screening-contract-1"


def content_hash(payload: dict) -> str:
    value = deepcopy(payload)
    value.pop("symbol", None)  # save_history includes the identity in its stored payload.
    value.pop("screening_validation", None)
    value.get("metadata", {}).pop("screening_validation", None)
    return checksum(value)


def certify_universe(payload: dict) -> dict:
    if payload.get("provenance") != "LIVE_UNVERIFIED" or not payload.get("members"):
        raise DataError("SCREENING_UNIVERSE_CONTRACT")
    for symbol, member in payload["members"].items():
        if not re.fullmatch(r"[A-Z0-9]{2,16}", symbol) or member.get("exchange") not in {"HOSE", "HNX", "UPCOM"}:
            raise DataError("SCREENING_UNIVERSE_CONTRACT")
    result = deepcopy(payload)
    result["screening_validation"] = {"policy": POLICY, "content_checksum": content_hash(result),
        "provider": "vnstock", "version": "5.0.3", "source": "vci", "scope": "SCREENING_ONLY"}
    return result


def certify_history(history: DailyHistory) -> DailyHistory:
    payload = deepcopy(history.payload)
    meta, bars = payload["metadata"], payload["bars"]
    equity = meta.get("kind") == "EQUITY"
    expected = {"provider": "vnstock", "provider_version": "5.0.3", "provenance": "LIVE_UNVERIFIED",
        "price_unit": "VND" if equity else "POINTS",
        "source_price_unit": "THOUSAND_VND" if equity else "POINTS", "volume_unit": "SHARES",
        "target_session": history.target_session.isoformat(), "input_mode": history.input_mode}
    if meta.get("kind") not in {"EQUITY", "INDEX"} or meta.get("source") not in ({"vci"} if equity else {"vci", "kbs"}) or any(meta.get(k) != v for k, v in expected.items()) or not bars:
        raise DataError("SCREENING_HISTORY_CONTRACT")
    observed = datetime.fromisoformat(meta["observed_at"])
    if observed.tzinfo is None:
        raise DataError("SCREENING_HISTORY_CONTRACT")
    previous = None
    for bar in bars:
        session = date.fromisoformat(bar["session"])
        values = [bar[k] for k in ("open", "high", "low", "close", "volume")]
        if (previous and session <= previous) or session > history.target_session or not all(math.isfinite(v) for v in values):
            raise DataError("SCREENING_HISTORY_CONTRACT")
        o, h, low, close, volume = values
        if min(o, h, low, close) <= 0 or volume < 0 or volume != int(volume) or h < max(o, low, close) or low > min(o, h, close):
            raise DataError("SCREENING_HISTORY_CONTRACT")
        previous = session
    if previous != history.target_session:
        raise DataError("SCREENING_HISTORY_CONTRACT")
    meta["screening_validation"] = {"policy": POLICY, "scope": "SCREENING_ONLY",
        "content_checksum": content_hash(payload), "checks": ["SOURCE_VERSION_UNITS", "ORDERED_FINITE_OHLCV", "TARGET_SESSION"]}
    return DailyHistory(history.symbol, history.target_session, history.input_mode, payload)


def eligible(payload: dict, *, universe: bool = False) -> bool:
    container = payload if universe else payload.get("metadata", {})
    if container.get("provenance") == "LIVE_VALIDATED":
        return True
    evidence = container.get("screening_validation", {})
    return bool(container.get("provenance") == "LIVE_UNVERIFIED" and evidence.get("policy") == POLICY
        and evidence.get("scope") == "SCREENING_ONLY" and evidence.get("content_checksum") == content_hash(payload))

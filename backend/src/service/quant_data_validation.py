"""Accept the pinned provider OHLCV contract without claiming an independent audit.

Vnstock documents daily equity OHLCV as corporate-action adjusted. Prototype
analysis consumes that series directly. The trading statistics endpoint has
different close semantics on some instruments and is not an acceptance oracle.
"""
from service.screening_validation import eligible

POLICY = "vnstock-5.0.3-quant-provider-contract-1"
SCOPE = "PROVIDER_OHLCV_CONTRACT"


def universe_accepted(payload: dict) -> bool:
    return eligible(payload, universe=True)


def history_accepted(payload: dict) -> bool:
    meta = payload.get("metadata", {})
    if meta.get("provenance") == "LIVE_VALIDATED" and meta.get("adjustment") == "VERIFIED":
        return True  # Existing explicitly validated datasets retain their contract.
    return bool(meta.get("input_mode") == "COMPLETED_DAILY"
        and meta.get("adjustment") == "PROVIDER_ADJUSTMENT_UNVERIFIED"
        and meta.get("provider") == "vnstock" and meta.get("provider_version") == "5.0.3"
        and eligible(payload))

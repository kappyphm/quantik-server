"""Accept the pinned provider OHLCV contract without claiming an independent audit.

Vnstock documents daily equity OHLCV as corporate-action adjusted. Prototype
analysis consumes that series directly. The trading statistics endpoint has
different close semantics on some instruments and is not an acceptance oracle.
"""
import re

from service.screening_validation import POLICY as INGESTION_POLICY, eligible

POLICY = "vnstock-5.0.3-quant-provider-contract-1"
SCOPE = "PROVIDER_OHLCV_CONTRACT"


def universe_accepted(payload: dict) -> bool:
    return eligible(payload, universe=True)


def history_header_accepted(meta: dict) -> bool:
    """Cheap admission preflight; numerical consumers verify full content too."""
    if meta.get("provenance") == "LIVE_VALIDATED" and meta.get("adjustment") == "VERIFIED":
        return True  # Existing explicitly validated datasets retain their contract.
    proof = meta.get("screening_validation", {})
    return bool(meta.get("input_mode") == "COMPLETED_DAILY"
        and meta.get("adjustment") == "PROVIDER_ADJUSTMENT_UNVERIFIED"
        and meta.get("provider") == "vnstock" and meta.get("provider_version") == "5.0.3"
        and meta.get("provenance") == "LIVE_UNVERIFIED"
        and proof.get("policy") == INGESTION_POLICY and proof.get("scope") == "SCREENING_ONLY"
        and isinstance(proof.get("content_checksum"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["content_checksum"]))


def history_accepted(payload: dict) -> bool:
    return history_header_accepted(payload.get("metadata", {})) and eligible(payload)

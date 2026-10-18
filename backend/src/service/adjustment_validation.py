"""Compare normalized equity OHLC with VCI's explicit adjusted-price fields.

This verifies consistency with the provider's adjustment series, not an
independent reconstruction of corporate actions. It never modifies candles.
"""
from service.market_data import DataError, DailyHistory, checksum

POLICY = "vci-adjusted-trades-1"
FIELDS = {"open": "open_price_adjusted", "high": "highest_price_adjusted",
          "low": "lowest_price_adjusted", "close": "close_price_adjusted"}


def verify_adjusted_prices(history: DailyHistory, reference) -> dict:
    import numpy as np
    import pandas as pd

    meta = history.payload["metadata"]
    if (meta.get("provider") != "vnstock" or meta.get("provider_version") != "5.0.3"
            or meta.get("source") != "vci" or meta.get("kind") != "EQUITY"
            or meta.get("price_unit") != "VND" or history.input_mode != "COMPLETED_DAILY"
            or reference.attrs.get("source") != "vci"
            or reference.attrs.get("symbol") != history.symbol
            or not {"trading_date", *FIELDS.values()}.issubset(reference.columns)):
        raise DataError("ADJUSTMENT_REFERENCE_CONTRACT")
    try:
        stamps = pd.to_datetime(reference["trading_date"], errors="raise")
        if stamps.dt.tz is None:
            raise DataError("ADJUSTMENT_REFERENCE_TIMEZONE")
        dates = stamps.dt.tz_convert("Asia/Ho_Chi_Minh").dt.date.astype(str)
        right = reference[list(FIELDS.values())].apply(pd.to_numeric, errors="raise").copy()
        right.index = dates
        left = pd.DataFrame(history.payload["bars"]).set_index("session")
    except (ValueError, TypeError, AttributeError, KeyError):
        raise DataError("ADJUSTMENT_REFERENCE_SCHEMA") from None
    if not right.index.is_unique or not left.index.is_unique:
        raise DataError("ADJUSTMENT_DUPLICATE_SESSION")
    if left.empty or not left.index.isin(right.index).all():
        raise DataError("ADJUSTMENT_REFERENCE_INCOMPLETE")
    right = right.loc[left.index]
    actual = left[list(FIELDS)].to_numpy(dtype=float)
    adjusted = right[list(FIELDS.values())].to_numpy(dtype=float)
    if (not np.isfinite(actual).all() or not np.isfinite(adjusted).all()
            or (actual <= 0).any() or (adjusted <= 0).any()):
        raise DataError("ADJUSTMENT_INVALID_PRICE")
    # Only floating-point conversion noise is tolerated, in normalized VND.
    delta = np.abs(actual-adjusted)
    if (delta > 0.000001).any():
        raise DataError("ADJUSTMENT_PRICE_MISMATCH")
    records = [{"session": day, **{key: float(value) for key, value in row.items()}}
               for day, row in right.iterrows()]
    return {"policy": POLICY, "scope": "PROVIDER_ADJUSTED_PRICE_CONSISTENCY",
            "provider": "vnstock", "version": "5.0.3", "source": "vci",
            "endpoint": "Market.equity.trade_history", "reference_price_unit": "VND",
            "history_checksum": checksum({"symbol": history.symbol, **history.payload}),
            "reference_checksum": checksum({"records": records}), "sessions": len(left),
            "first_session": min(left.index), "last_session": max(left.index),
            "maximum_price_difference_vnd": float(delta.max()),
            "independent_corporate_action_audit": False}

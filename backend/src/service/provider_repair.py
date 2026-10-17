"""Replace invalid source candles only with valid, corroborated source records."""
from service.market_data import DataError, checksum


def replace_invalid_ohlc(primary, secondary, secondary_source: str):
    import numpy as np
    import pandas as pd
    columns = ["open", "high", "low", "close", "volume"]
    if secondary.attrs.get("source") != secondary_source or not {"time", *columns}.issubset(secondary.columns):
        raise DataError("REPAIR_SOURCE_CONTRACT")

    def indexed(frame):
        result = frame.copy()
        result.index = pd.to_datetime(result.time).dt.date
        if not result.index.is_unique:
            raise DataError("REPAIR_DUPLICATE_SESSION")
        return result

    def valid(row):
        o, h, low, close, volume = [float(row[key]) for key in columns]
        return (np.isfinite([o, h, low, close, volume]).all() and min(o, h, low, close) > 0
            and volume >= 0 and volume == int(volume) and h >= max(o, close, low) and low <= min(o, close, h))

    left, right = indexed(primary), indexed(secondary)
    shared = left.index.intersection(right.index)
    # Reject a different instrument/unit/regime before considering any repairs.
    anchors = [day for day in shared if valid(left.loc[day]) and valid(right.loc[day])]
    if len(anchors) < 120 or any(abs(float(left.loc[day, "close"])/float(right.loc[day, "close"])-1) > .005 for day in anchors):
        raise DataError("REPAIR_CORROBORATION_FAILED")
    evidence = []
    for day in left.index:
        if valid(left.loc[day]):
            continue
        if day not in right.index or not valid(right.loc[day]):
            raise DataError("REPAIR_CANDLE_UNAVAILABLE")
        original = {key: float(left.loc[day, key]) for key in columns}
        replacement = {key: float(right.loc[day, key]) for key in columns}
        evidence.append({"session": day.isoformat(), "source": secondary_source,
            "original_checksum": checksum(original), "replacement_checksum": checksum(replacement)})
        left.loc[day, columns] = [replacement[key] for key in columns]
    left.attrs = dict(primary.attrs)
    return left.reset_index(drop=True), evidence

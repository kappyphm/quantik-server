from copy import deepcopy
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from service.adjustment_validation import verify_adjusted_prices
from service.market_data import DataError, normalize_history


def inputs():
    frame = pd.DataFrame({"time": pd.date_range("2026-10-12", periods=3),
        "open": [58., 59., 60.], "high": [60., 61., 62.],
        "low": [57., 58., 59.], "close": [59., 60., 61.], "volume": [1000, 2000, 3000]})
    history = normalize_history(frame, symbol="FPT", kind="EQUITY", source="vci",
        target_session=date(2026, 10, 14), observed_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        minimum_bars=3, provenance="LIVE_UNVERIFIED")
    reference = pd.DataFrame({"trading_date": pd.date_range("2026-10-12", periods=3,
        tz="Asia/Ho_Chi_Minh").tz_convert("UTC"),
        "open_price_adjusted": frame.open*1000, "highest_price_adjusted": frame.high*1000,
        "lowest_price_adjusted": frame.low*1000, "close_price_adjusted": frame.close*1000})
    reference.attrs = {"source": "vci", "symbol": "FPT"}
    return history, reference


def test_provider_adjustment_evidence_is_honest_and_content_bound():
    history, reference = inputs()
    original = deepcopy(history.payload)
    evidence = verify_adjusted_prices(history, reference.iloc[::-1])
    assert evidence["sessions"] == 3 and evidence["first_session"] == "2026-10-12"
    assert evidence["maximum_price_difference_vnd"] == 0
    assert not evidence["independent_corporate_action_audit"]
    assert history.payload == original
    history.payload["bars"][0]["volume"] += 1
    assert verify_adjusted_prices(history, reference)["history_checksum"] != evidence["history_checksum"]


@pytest.mark.parametrize("change,code", [
    (lambda f: f.iloc[1:].copy(), "ADJUSTMENT_REFERENCE_INCOMPLETE"),
    (lambda f: pd.concat([f, f.iloc[:1]]), "ADJUSTMENT_DUPLICATE_SESSION"),
    (lambda f: f.assign(close_price_adjusted=f.close_price_adjusted+1), "ADJUSTMENT_PRICE_MISMATCH"),
    (lambda f: f.assign(close_price_adjusted=float("nan")), "ADJUSTMENT_INVALID_PRICE"),
    (lambda f: f.assign(trading_date=f.trading_date.dt.tz_localize(None)), "ADJUSTMENT_REFERENCE_TIMEZONE"),
])
def test_bad_reference_cannot_certify_history(change, code):
    history, reference = inputs()
    changed = change(reference); changed.attrs = reference.attrs
    with pytest.raises(DataError, match=code):
        verify_adjusted_prices(history, changed)


def test_wrong_instrument_and_source_are_rejected():
    history, reference = inputs()
    for attrs in ({"source": "vnd", "symbol": "FPT"}, {"source": "vci", "symbol": "CTG"}):
        reference.attrs = attrs
        with pytest.raises(DataError, match="ADJUSTMENT_REFERENCE_CONTRACT"):
            verify_adjusted_prices(history, reference)


def test_provider_audit_windows_cover_all_history_without_cap_or_overlap():
    from types import SimpleNamespace
    from service.vnstock_provider import VnstockProvider
    from service.market_data import DailyHistory

    original, _ = inputs()
    dates = pd.date_range("2022-01-01", periods=1298)
    payload = deepcopy(original.payload)
    payload["bars"] = [{**payload["bars"][0], "session": day.date().isoformat()} for day in dates]
    history = DailyHistory("FPT", dates[-1].date(), "COMPLETED_DAILY", payload)
    windows, operations = [], []

    def trade_history(*, start, end, limit, source):
        days = pd.date_range(start, end, tz="Asia/Ho_Chi_Minh")
        windows.append((start, end))
        assert len(days) <= 500 and limit == 1000 and source == "vci"
        f = pd.DataFrame({"trading_date": days,
            "open_price_adjusted": 58000., "highest_price_adjusted": 60000.,
            "lowest_price_adjusted": 57000., "close_price_adjusted": 59000.})
        f.attrs = {"source": "vci", "symbol": "FPT"}
        return f

    provider = VnstockProvider.__new__(VnstockProvider)
    provider.market = SimpleNamespace(equity=lambda symbol: SimpleNamespace(trade_history=trade_history))

    def call(operation, **metadata):
        operations.append(metadata)
        return operation()

    provider.call = call
    evidence = provider.adjustment_evidence(history)
    assert evidence["sessions"] == 1298 and len(windows) == 3
    assert all(m["operation_name"] == "adjustment_validation" for m in operations)

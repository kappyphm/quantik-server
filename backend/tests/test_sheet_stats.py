"""Focused checks for the screening sheet statistics (groups A+C columns).

The formulas mirror the report engines, so the assertions compare against
StatEngine itself plus a few hand-computed anchors.
"""
import numpy as np
import pandas as pd

from service.quant.engines import StatEngine
from service.quant.screening import log_return_tail, sector_contexts, sheet_stats


def frame(closes, volumes=None, opens=None):
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range("2025-01-02", periods=len(closes))
    return pd.DataFrame({
        "open": np.asarray(opens, dtype=float) if opens is not None else closes.copy(),
        "high": closes * 1.01, "low": closes * 0.99, "close": closes,
        "volume": np.asarray(volumes, dtype=float) if volumes is not None else np.full(len(closes), 1_000_000.0),
    }, index=index)


def test_sheet_stats_matches_report_engine_and_liquidity_rules():
    rng = np.random.default_rng(7)
    closes = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.02, 300))
    stats = sheet_stats(frame(closes), "COMPLETED_DAILY", "HOSE")
    expected = StatEngine.returns(pd.Series(closes[-253:]))
    assert stats["sharpe"] == expected["sharpe"]
    assert stats["up_day_ratio_pct"] == expected["up_day_ratio_pct"]
    assert stats["max_drawdown_pct"] == expected["max_dd_pct"]
    assert stats["annual_return_pct"] == expected["ann_return_pct"]
    adv20_value = float(np.mean(closes[-20:] * 1_000_000.0))
    assert stats["adv20_value_vnd"] == round(adv20_value, 0)
    assert stats["liquidity_tier"] == ("A" if adv20_value >= 50e9 else "B" if adv20_value >= 20e9
                                       else "C" if adv20_value >= 5e9 else "D")
    assert stats["capacity_shares"] == int(1_000_000.0 * 0.05 // 100) * 100


def test_sheet_stats_steady_rise_anchors():
    closes = 100.0 * 1.001 ** np.arange(300)
    opens = np.concatenate([[closes[0]], closes[:-1]])  # opens at the prior close: no gap
    stats = sheet_stats(frame(closes, opens=opens), "COMPLETED_DAILY", "HOSE")
    assert stats["up_day_ratio_pct"] == 100.0
    assert stats["max_drawdown_pct"] == 0.0
    assert stats["gap_abs_p95_pct"] == 0.0


def test_sheet_stats_provisional_drops_last_bar_and_short_frame_is_none():
    closes = 100.0 * 1.001 ** np.arange(300)
    baseline = sheet_stats(frame(closes[:-1]), "COMPLETED_DAILY", "HOSE")
    spiked = closes.copy()
    spiked[-1] = spiked[-1] * 3
    provisional = sheet_stats(frame(spiked), "PROVISIONAL_DAILY", "HOSE")
    assert provisional["adv20_value_vnd"] == baseline["adv20_value_vnd"]
    assert sheet_stats(frame(closes[:10]), "COMPLETED_DAILY", "HOSE") is None
    assert sheet_stats(None, "COMPLETED_DAILY", "HOSE") is None


def test_sector_contexts_basket_trend_and_correlation():
    index = pd.bdate_range("2025-01-02", periods=61)
    wave = 0.01 + 0.004 * np.sin(np.arange(60) / 5.0)
    rising = pd.Series(wave, index=index[1:])
    falling = pd.Series(np.full(60, -0.01), index=index[1:])
    contexts = sector_contexts(
        {"AAA": rising, "BBB": rising.copy(), "CCC": falling},
        {"AAA": "8300", "BBB": "8300", "CCC": "9500"})
    assert contexts["AAA"]["sector_correlation"] == 1.0
    assert contexts["AAA"]["sector_return_20d_pct"] == round(float((np.exp(wave[-20:].sum()) - 1) * 100), 2)
    assert contexts["AAA"]["sector_trend"] == "🟢 TĂNG MẠNH"
    assert contexts["CCC"]["sector_correlation"] is None
    assert contexts["CCC"]["sector_trend"] == "🔴 GIẢM MẠNH"


def test_log_return_tail_shape():
    data = frame(100.0 * 1.001 ** np.arange(300))
    tail = log_return_tail(data)
    assert tail is not None and len(tail) == 60
    assert log_return_tail(None) is None

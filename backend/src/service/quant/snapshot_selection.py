"""Prototype prefilter before expensive Quant snapshot computation."""
import pandas as pd

from service.quant.screening_legacy import FeatureEngine, ScreeningEngine

POLICY = "prototype-liquidity-top70-1"


def select_candidates(data: dict[str, pd.DataFrame], index: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    scores, excluded = [], {}
    index_frame = index.reset_index(names="time")
    for symbol, frame in sorted(data.items()):
        if len(frame) < 20:
            excluded[symbol] = "SCREENING_INSUFFICIENT_HISTORY"
            continue
        if frame.volume.tail(20).mean() < 100_000:
            excluded[symbol] = "SCREENING_LOW_LIQUIDITY"
            continue
        features = FeatureEngine.compute_features(frame.reset_index(names="time"))
        features = FeatureEngine.compute_relative_strength(features, index_frame)
        scores.append((symbol, ScreeningEngine.screen_strong(features)["score"],
            ScreeningEngine.screen_accumulation(features)["score"],
            ScreeningEngine.screen_distribution(features)["score"]))
    # Same score priority as crawl_data.py; symbol resolves otherwise equal scores.
    scores.sort(key=lambda row: (-row[1], -row[2], row[3], row[0]))
    selected = [row[0] for row in scores[:70]]
    excluded.update({row[0]: "SCREENING_OUTSIDE_TOP70" for row in scores[70:]})
    return selected, excluded

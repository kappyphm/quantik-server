import ast
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from service.quant.snapshot_selection import select_candidates


def test_snapshot_prefilter_matches_prototype_and_caps_expensive_candidates():
    source = ast.parse(Path("docs/prototype/crawl_data.py").read_text(encoding="utf-8-sig"))
    nodes = [node for node in source.body if isinstance(node, ast.ClassDef)
        and node.name in {"FeatureEngine", "ScreeningEngine"}]
    original = {"pd": pd, "np": np, "Dict": Dict}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "prototype-selection", "exec"), original)
    days = pd.bdate_range(end="2026-10-14", periods=80)
    n = np.arange(len(days))
    data = {}
    for i in range(76):
        close = 100 + (i % 5 - 2)*n*.1 + 3*np.sin(n/(2+i % 7))
        data[f"S{i:02}"] = pd.DataFrame({"open": close*.999, "high": close*1.01,
            "low": close*.99, "close": close, "volume": np.full(len(n), 100000)}, index=days)
    data["LOW"] = data["S00"].assign(volume=99999)
    data["NEW"] = data["S00"].tail(19)
    data["YOUNG"] = data["S00"].tail(20)
    index = data["S01"]
    rows = []
    for symbol, frame in data.items():
        if len(frame) < 20 or frame.volume.tail(20).mean() < 100000:
            continue
        features = original["FeatureEngine"].compute_features(frame.reset_index(names="time"))
        features = original["FeatureEngine"].compute_relative_strength(features, index.reset_index(names="time"))
        rows.append({"symbol": symbol, **{group: getattr(original["ScreeningEngine"], method)(features)["score"]
            for group, method in [("strong", "screen_strong"), ("accum", "screen_accumulation"),
                ("dist", "screen_distribution")]}})
    expected = pd.DataFrame(rows).sort_values(["strong", "accum", "dist", "symbol"],
        ascending=[False, False, True, True]).symbol.tolist()
    selected, excluded = select_candidates(data, index)
    assert selected == expected[:70]
    assert len(selected) == 70
    assert excluded["LOW"] == "SCREENING_LOW_LIQUIDITY"
    assert excluded["NEW"] == "SCREENING_INSUFFICIENT_HISTORY"
    assert all(excluded[symbol] == "SCREENING_OUTSIDE_TOP70" for symbol in expected[70:])
    assert set(selected) | set(excluded) == set(data)
    # Like the prototype, 20 bars suffice even if a longer-lookback rule is false.
    assert select_candidates({"YOUNG": data["YOUNG"]}, index) == (["YOUNG"], {})

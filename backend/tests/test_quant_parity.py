import ast
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from service.quant import engines as current
from service.quant.configuration import QuantConfig, engine_context


def compare(left, right):
    if isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            compare(left[key], right[key])
    elif isinstance(left, (list, tuple)):
        assert len(left) == len(right)
        for a, b in zip(left, right):
            compare(a, b)
    elif isinstance(left, (float, int, np.number, np.ndarray)):
        np.testing.assert_allclose(left, right, rtol=1e-12, atol=1e-12, equal_nan=True)
    else:
        assert left == right


def test_extracted_engine_source_and_critical_numerics_match_prototype():
    source = Path("docs/prototype/quant.py").read_text(encoding="utf-8-sig")
    source = source.replace("stats.kstest(ret, 'norm', args=(mu, sig))",
        "stats.kstest(ret, stats.norm.cdf, args=(mu, sig))")
    original = ast.parse(source)
    extracted = ast.parse(Path(current.__file__).read_text(encoding="utf-8"))
    originals = {node.name: node for node in original.body if isinstance(node, ast.ClassDef)}
    removed = {"SectorEngine": {"_load_icb_map", "_load_watchlist_map", "map_symbols"},
        "CrossSectionalLightGBMEngine": {"fit_predict"}, "AdaptiveScorer": {"score_batch", "_score_absolute"}}
    nodes = []
    for node in extracted.body:
        if not isinstance(node, ast.ClassDef):
            continue
        baseline = originals[node.name]
        baseline.body = [method for method in baseline.body if not isinstance(method, ast.FunctionDef)
            or method.name not in removed.get(node.name, set())]
        assert ast.dump(node, include_attributes=False) == ast.dump(baseline, include_attributes=False), node.name
        nodes.append(baseline)
    # Execute only whitelisted pure AST nodes, without importing prototype/provider/exporters.
    config = originals["QuantConfig"]
    namespace = {}
    imports = ast.parse("import logging, hashlib, math\nimport numpy as np\nimport pandas as pd\n"
        "from datetime import datetime, timedelta\nfrom dataclasses import dataclass, field\n"
        "from typing import Dict, List, Optional, Tuple\nlog=logging.getLogger('parity')\n").body
    module = ast.Module(body=imports + [config] + ast.parse("CFG=QuantConfig()").body + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "prototype-pure-baseline", "exec"), namespace)
    days = pd.bdate_range(end="2026-10-14", periods=252)
    n = np.arange(252)
    prices = 90000 * np.exp(0.0004 * n + 0.01 * np.sin(n / 5))
    frame = pd.DataFrame({"open": prices * 0.999, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices, "volume": np.full(252, 1000000)}, index=days)
    cases = [
        ("DataQualityEngine", "audit", (frame,), {"as_of": "2026-10-15"}),
        ("LiquidityEngine", "analyze", (frame,), {}),
        ("StatEngine", "returns", (frame.close,), {}),
        ("DistributionAnalyzer", "full_test", (frame.close,), {}),
        ("FcastEngine", "ensemble", (frame.close,), {"symbol": "FPT"}),
        ("VNMarketRules", "price_band", (90000,), {"exchange": "HOSE"}),
    ]
    with engine_context(replace(QuantConfig(), PRICE_INPUT_UNIT="VND")):
        for engine, method, args, kwargs in cases:
            compare(getattr(namespace[engine], method)(*args, **kwargs),
                getattr(getattr(current, engine), method)(*args, **kwargs))
        simulation = current.FcastEngine.ensemble(frame.close, symbol="FPT")
        liquidity = current.LiquidityEngine.analyze(frame)
        costs = current.CostEngine.analyze(simulation, liquidity)
        compare(costs, namespace["CostEngine"].analyze(simulation, liquidity))
        stats = current.StatEngine.returns(frame.close)
        compare(current.RiskEng.stops(frame, rs=stats, fc=simulation, liquidity=liquidity, costs=costs),
            namespace["RiskEng"].stops(frame, rs=stats, fc=simulation, liquidity=liquidity, costs=costs))
    assert len(nodes) == 22


def test_request_configuration_isolated_and_restored():
    base = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")
    default = current.CostEngine.analyze()["roundtrip_cost_bps"]
    with engine_context(replace(base, COMMISSION_BPS_PER_SIDE=20)):
        first = current.CostEngine.analyze()["roundtrip_cost_bps"]
        with engine_context(replace(base, COMMISSION_BPS_PER_SIDE=30)):
            assert current.CostEngine.analyze()["roundtrip_cost_bps"] == first + 20
        assert current.CostEngine.analyze()["roundtrip_cost_bps"] == first
    assert current.CostEngine.analyze()["roundtrip_cost_bps"] == default

"""Reproduce the pure-engine baseline without importing/executing the prototype."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/prototype/quant.py"
OUTPUT = ROOT / "backend/src/service/quant"
CLASSES = {"VNMarketRules", "CostEngine", "DataQualityEngine", "LiquidityEngine", "ActionEngine",
    "SectorEngine", "CrossCorrelationEngine", "DistributionAnalyzer", "StatEngine", "ARIMAEngine",
    "GARCHEngine", "HMMEngine", "AlphaEngine", "StructureEngine", "MomentumAlphaEngine", "HurstRegimeEngine",
    "ConditionalResidualReversionEngine", "CrossSectionalLightGBMEngine", "AgreementEngine", "FcastEngine", "RiskEng", "AdaptiveScorer"}
EXCLUDED = {"SectorEngine": {"_load_icb_map", "_load_watchlist_map", "map_symbols"},
    "CrossSectionalLightGBMEngine": {"fit_predict"}, "AdaptiveScorer": {"score_batch", "_score_absolute"}}


def main() -> None:
    raw = SOURCE.read_bytes()
    source = raw.decode("utf-8-sig").replace("\r\n", "\n")
    lines, tree = source.splitlines(keepends=True), ast.parse(source)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    engines, inventory = [], {"source": "docs/prototype/quant.py", "sha256": sha, "engines": {}}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "QuantConfig":
            config = "@dataclass(frozen=True)\n" + "".join(lines[node.lineno-1:node.end_lineno])
            (OUTPUT / "configuration.py").write_text(
                '"""Versioned prototype configuration, with request-local immutable context."""\n'
                'from contextlib import contextmanager\nfrom contextvars import ContextVar\n'
                'from dataclasses import dataclass, field, replace\n\n' + config + '\n\n'
                '_CURRENT = ContextVar("quantik_quant_config", default=replace(QuantConfig(), PRICE_INPUT_UNIT="VND"))\n\n'
                'class ConfigProxy:\n    def __getattr__(self, name):\n        return getattr(_CURRENT.get(), name)\n\n'
                'CFG = ConfigProxy()\n\n'
                '@contextmanager\ndef engine_context(config: QuantConfig):\n'
                '    if config.PRICE_INPUT_UNIT != "VND" or config.ENABLE_INTERNAL_STOP_OPTIMIZATION:\n'
                '        raise ValueError("Service engines require VND and disabled stop optimization")\n'
                '    token = _CURRENT.set(config)\n    try:\n        yield\n    finally:\n        _CURRENT.reset(token)\n',
                encoding="utf-8")
        if not isinstance(node, ast.ClassDef) or node.name not in CLASSES:
            continue
        skip_lines = set()
        methods = {}
        for method in node.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            if method.name in EXCLUDED.get(node.name, set()):
                first = min([method.lineno, *[decorator.lineno for decorator in method.decorator_list]])
                skip_lines.update(range(first, method.end_lineno + 1))
                continue
            keys = sorted({key.value for sub in ast.walk(method) if isinstance(sub, ast.Dict)
                for key in sub.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)})
            methods[method.name] = {"source_line": method.lineno, "literal_output_and_internal_keys": keys}
        engines.append("".join(lines[i-1] for i in range(node.lineno, node.end_lineno+1) if i not in skip_lines))
        inventory["engines"][node.name] = methods
    header = '"""Pure engines extracted from preserved prototype; changes belong in service orchestration.\n'
    header += f'Baseline SHA256: {sha}\n"""\n'
    header += 'import logging, hashlib, math\nimport numpy as np\nimport pandas as pd\n'
    header += 'from datetime import datetime, timedelta\nfrom typing import Dict, List, Optional, Tuple\n'
    header += 'from service.quant.configuration import CFG\nlog = logging.getLogger(__name__)\n\n'
    generated = header + "\n\n".join(engines) + "\n"
    # SciPy 1.18 string-name fast path selects special.ndtr, which cannot accept
    # loc/scale args. The explicit norm.cdf callable preserves the same fitted CDF.
    generated = generated.replace("stats.kstest(ret, 'norm', args=(mu, sig))",
        "stats.kstest(ret, stats.norm.cdf, args=(mu, sig))")
    inventory["compatibility_changes"] = ["scipy-1.18-explicit-normal-cdf"]
    parsed = ast.parse(generated)
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module.startswith("vnstock") or node.module == "pathlib"):
            raise RuntimeError("Provider/file import leaked into engines")
        if isinstance(node, ast.Attribute) and node.attr in {"read_csv", "read_excel", "to_csv", "to_excel", "read_bytes", "write_bytes"}:
            raise RuntimeError("File access leaked into engines")
    (OUTPUT / "engines.py").write_text(generated, encoding="utf-8")
    narrative = '"""Preserved prototype commentary; provider/export/tracking methods excluded."""\n'
    narrative += 'import math\nimport numpy as np\nfrom service.quant.configuration import CFG\n\n'
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"RATINGS", "BUY_ACTIONS"} for t in node.targets):
            narrative += "".join(lines[node.lineno-1:node.end_lineno]) + "\n"
        if isinstance(node, ast.FunctionDef) and node.name in {"final_action", "action_rating", "num", "above", "below", "commentary"}:
            narrative += "".join(lines[node.lineno-1:node.end_lineno]) + "\n\n"
    narrative += "class PrototypeCommentary:\n"
    pipeline = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QuantPipeline")
    for method in pipeline.body:
        if isinstance(method, ast.FunctionDef) and (method.name == "generate_commentary" or method.name.startswith("_section_")):
            narrative += "".join(lines[method.lineno-1:method.end_lineno]) + "\n\n"
    ast.parse(narrative)
    (OUTPUT / "commentary.py").write_text(narrative, encoding="utf-8")
    (ROOT / "docs/saas-plan/quant-field-inventory.json").write_text(json.dumps(inventory,
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Extracted {len(engines)} pure engine classes; source hash recorded")


if __name__ == "__main__":
    main()

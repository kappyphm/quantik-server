"""Extract pure screening/reference narrative without importing provider/export code."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[2]
source = (root / "docs/prototype/crawl_data.py").read_text(encoding="utf-8-sig")
lines, tree = source.splitlines(keepends=True), ast.parse(source)
parts = []
for node in tree.body:
    if not isinstance(node, ast.ClassDef):
        continue
    if node.name in {"FeatureEngine", "ScreeningEngine", "AnalysisEngine"}:
        parts.append("".join(lines[node.lineno-1:node.end_lineno]))
    if node.name == "MarketAnalyzer":
        init = next(method for method in node.body if isinstance(method, ast.FunctionDef) and method.name == "__init__")
        part = "".join(lines[i-1] for i in range(node.lineno, node.end_lineno+1) if not init.lineno <= i <= init.end_lineno)
        part = part.replace("def analyze_index(self, index_symbol: str = 'VNINDEX', days: int = 90)",
            "def analyze_frame(self, index_df: pd.DataFrame, index_symbol: str = 'VNINDEX', input_mode: str = 'COMPLETED_DAILY')")
        part = part.replace("        index_df = self.data_provider.get_ohlcv(index_symbol, days=days)\n", "")
        part = part.replace("index_df = fe.compute_features(index_df)", "index_df = fe.compute_features(index_df, input_mode=input_mode)")
        parts.append(part)
    if node.name == "StockScreener":
        method = next(m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "_apply_regime_adjustment")
        first = min([method.lineno, *[d.lineno for d in method.decorator_list]])
        parts.append("class RegimeAdjustment:\n" + "".join(lines[first-1:method.end_lineno]))
body = "\n\n".join(parts)
body = body.replace("def compute_features(df: pd.DataFrame)",
    "def compute_features(df: pd.DataFrame, input_mode: str = 'COMPLETED_DAILY')")
needle = "        # Volume ratio (hôm nay vs trung bình 20 phiên)"
if needle not in body:
    raise RuntimeError("Prototype volume insertion point changed")
body = body.replace(needle, "        if input_mode == 'PROVISIONAL_DAILY' and len(df):\n"
    "            df.loc[df.index[-1], 'vol_avg20'] = df['volume'].iloc[:-1].tail(20).mean() if len(df) >= 21 else np.nan\n\n" + needle)
generated = '"""Preserved screening formulas; explicit provisional volume and provider-free market context."""\n'
generated += 'import logging\nfrom typing import Dict, List, Optional, Tuple\nimport numpy as np\nimport pandas as pd\nlogger=logging.getLogger(__name__)\n\n' + body + '\n'
if "self.data_provider" in generated:
    raise RuntimeError("Provider dependency leaked into screening")
ast.parse(generated)
(root / "backend/src/service/quant/screening_legacy.py").write_text(generated, encoding="utf-8")
print("Extracted provider-free screening/assessment/market context")

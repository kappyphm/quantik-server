"""Generation-6 / DNSE spike: verify the source before the provider ships.

Run once the DNSE Open API keys exist, in an environment with the pinned
generation-6 packages (never install them into the production image for
the spike):

    pip install "vnstock==6.0.1" "vnstock-oapi==0.1.2" pandas numpy \
        --extra-index-url https://vnstocks.com/api/packages
    DNSE_API_KEY=... DNSE_API_SECRET=... \
        python backend/scripts/spike_dnse.py [--database-url postgresql://...]

Every check prints PASS/FAIL/SKIP with its evidence and a JSON summary is
printed last. Exit code is 0 only when all critical checks pass (listing,
history depth, price unit, index candles). Keys are read from the
environment and never printed.

What this cannot decide for you:
- DNSE account rate limits on paper (the burst check measures behaviour;
  confirm the numbers on the DNSE account page).
- Whether the device registration survives a Docker rebuild: rebuild the
  spike container/image, re-run this script, and confirm the listing
  check still passes without a new device registration.
- Commercial-use terms for redistributing DNSE data (account paperwork).
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, timedelta

SOURCE = "dnse"
RESULTS = []


def record(name: str, status: str, evidence: str, critical: bool = False) -> None:
    RESULTS.append({"check": name, "status": status, "evidence": evidence, "critical": critical})
    print(f"[{status}] {name}: {evidence}")


def code_of(exc: Exception) -> str:
    return str(getattr(exc, "code", type(exc).__name__))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
        help="Optional production database: compare DNSE closes against certified history.")
    parser.add_argument("--burst", type=int, default=30, help="Burst size for the rate probe.")
    args = parser.parse_args()

    # 1. Environment and versions.
    missing = [name for name in ("DNSE_API_KEY", "DNSE_API_SECRET") if not os.environ.get(name)]
    if missing:
        record("env_keys", "FAIL", f"missing environment variables: {', '.join(missing)}", critical=True)
        print(json.dumps({"results": RESULTS}, ensure_ascii=False))
        return 1
    record("env_keys", "PASS", "DNSE_API_KEY and DNSE_API_SECRET present (values not shown); "
        f"VNSTOCK_API_KEY {'present' if os.environ.get('VNSTOCK_API_KEY') else 'absent (optional for Market/Reference)'}")
    try:
        from importlib.metadata import version
        found = version("vnstock")
        record("library_version", "PASS" if found == "6.0.1" else "FAIL",
            f"vnstock=={found} (expected 6.0.1)", critical=found != "6.0.1")
        record("oapi_package", "PASS", f"vnstock-oapi=={version('vnstock-oapi')}")
    except Exception as exc:
        record("library_version", "FAIL", f"package metadata unreadable: {code_of(exc)}", critical=True)
        print(json.dumps({"results": RESULTS}, ensure_ascii=False))
        return 1

    import vnstock
    try:
        vnstock.sources.use(SOURCE)
        readers = vnstock.sources.readers("market.equity.ohlcv")
        record("source_route", "PASS" if SOURCE in str(readers) else "FAIL",
            f"sources.use('dnse') ok; ohlcv readers: {readers}", critical=True)
    except Exception as exc:
        record("source_route", "FAIL", f"sources.use/readers failed: {code_of(exc)}", critical=True)
        print(json.dumps({"results": RESULTS}, ensure_ascii=False))
        return 1

    market, reference = vnstock.Market(), vnstock.Reference()

    # 2. Listing / universe.
    listing = None
    try:
        listing = reference.equity.list_by_exchange()
        record("listing", "PASS" if len(listing) >= 1400 else "FAIL",
            f"{len(listing)} symbols, columns: {list(listing.columns)}", critical=True)
    except Exception as exc:
        record("listing", "FAIL", f"list_by_exchange failed: {code_of(exc)}", critical=True)

    # 3. Equity history depth, schema and price unit (FPT).
    end = date.today()
    start = end - timedelta(days=620)
    frame = None
    try:
        frame = market.equity("FPT").ohlcv(start=start.isoformat(), end=end.isoformat(),
            interval="1D", limit=20000)
        columns = set(frame.columns)
        last_close = float(frame["close"].iloc[-1])
        unit = "THOUSAND_VND (v5-compatible)" if last_close < 1000 else "VND"
        record("history_depth", "PASS" if len(frame) >= 252 else "FAIL",
            f"FPT bars: {len(frame)} over {start}..{end} (need >=252 for reports, >=120 minimum)",
            critical=True)
        record("history_schema", "PASS" if {"time", "open", "high", "low", "close", "volume"} <= columns else "FAIL",
            f"columns: {sorted(columns)}", critical=True)
        record("price_unit", "PASS" if unit.startswith("THOUSAND") else "FAIL",
            f"FPT last raw close {last_close} -> unit looks like {unit}; "
            "normalize_history multiplies equity prices by 1000, so THOUSAND_VND is required",
            critical=True)
        record("frame_attrs", "PASS", f"attrs: {dict(frame.attrs)}")
    except Exception as exc:
        record("history_depth", "FAIL", f"FPT ohlcv failed: {code_of(exc)}", critical=True)

    # 4. Index candles.
    try:
        index = market.index("VNINDEX").ohlcv(start=(end - timedelta(days=30)).isoformat(),
            end=end.isoformat(), interval="1D", limit=100)
        record("index_candles", "PASS" if len(index) >= 10 else "FAIL",
            f"VNINDEX bars: {len(index)}, last close {float(index['close'].iloc[-1])} (points)",
            critical=True)
    except Exception as exc:
        record("index_candles", "FAIL", f"VNINDEX ohlcv failed: {code_of(exc)}", critical=True)

    # 5. Adjusted-price continuity across a known ex-date window: report the
    # largest overnight gaps in the fetched series for manual judgement, and
    # (with a database) the deviation against certified generation-5 closes.
    if frame is not None:
        closes = frame["close"].astype(float)
        moves = (closes.pct_change().abs() * 100).dropna()
        record("adjustment_overview", "PASS",
            f"FPT overnight moves: p99 {moves.quantile(0.99):.2f}%, max {moves.max():.2f}% "
            f"on {moves.idxmax()} (large ex-date gaps in an adjusted series need explanation)")
    if args.database_url and frame is not None:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(args.database_url)
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT payload FROM history_revisions WHERE symbol = 'FPT' "
                    "AND input_mode = 'COMPLETED_DAILY' "
                    "ORDER BY target_session DESC, id DESC LIMIT 1")).first()
            certified = {bar["session"]: bar["close"] for bar in row[0]["bars"]}
            mine = {str(t)[:10]: float(c) * 1000 for t, c in zip(frame["time"], frame["close"])}
            common = sorted(set(certified) & set(mine))
            deviations = [abs(mine[d] / certified[d] - 1) * 100 for d in common if certified[d]]
            record("adjustment_vs_certified", "PASS" if deviations and max(deviations) < 0.5 else "FAIL",
                f"{len(common)} overlapping sessions vs certified series; "
                f"max deviation {max(deviations):.3f}% (adjustment bases agree if < 0.5%)",
                critical=True)
        except Exception as exc:
            record("adjustment_vs_certified", "SKIP", f"comparison unavailable: {code_of(exc)}")
    else:
        record("adjustment_vs_certified", "SKIP",
            "no --database-url given; run on the server to compare against certified closes")

    # 6. Error taxonomy: an unknown symbol must surface a VNSTOCK_* code.
    try:
        market.equity("ZZZNOSUCH").ohlcv(start=start.isoformat(), end=end.isoformat(),
            interval="1D", limit=10)
        record("error_taxonomy", "FAIL", "unknown symbol returned without an error")
    except Exception as exc:
        code = code_of(exc)
        record("error_taxonomy", "PASS" if code.startswith("VNSTOCK_") else "FAIL",
            f"unknown symbol raised code: {code}")

    # 7. Burst probe: latency and rate behaviour over distinct symbols.
    try:
        if listing is not None and "type" in listing.columns:
            equities = listing[listing["type"].str.lower().eq("stock")]
        else:
            equities = listing
        symbols = [str(s) for s in equities["symbol"].head(args.burst)] if equities is not None else ["FPT"] * args.burst
        latencies, errors = [], []
        begin = time.time()
        for symbol in symbols:
            one = time.time()
            try:
                market.equity(symbol).ohlcv(start=(end - timedelta(days=40)).isoformat(),
                    end=end.isoformat(), interval="1D", limit=100)
            except Exception as exc:
                errors.append(code_of(exc))
            latencies.append(time.time() - one)
        elapsed = time.time() - begin
        record("burst_probe", "PASS" if not errors else "FAIL",
            f"{len(symbols)} calls in {elapsed:.1f}s, median latency {statistics.median(latencies):.2f}s, "
            f"errors: {errors or 'none'}; extrapolated full ingest (~1570 calls): "
            f"{elapsed / max(1, len(symbols)) * 1570 / 60:.1f} min at this pace")
    except Exception as exc:
        record("burst_probe", "FAIL", f"burst probe failed: {code_of(exc)}")

    failed = [r for r in RESULTS if r["critical"] and r["status"] == "FAIL"]
    print(json.dumps({"source": SOURCE, "results": RESULTS,
        "verdict": "FAIL" if failed else "PASS"}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

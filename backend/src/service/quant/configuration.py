"""Versioned prototype configuration, with request-local immutable context."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

@dataclass(frozen=True)
class QuantConfig:
    LOOKBACK_DAYS: int = 252; MIN_HISTORY: int = 120; RISK_FREE_RATE: float = 0.045
    VOL_WINDOW_SHORT: int = 10; VOL_WINDOW_MED: int = 20; VOL_WINDOW_LONG: int = 60
    GARCH_P: int = 1; GARCH_Q: int = 1
    ARIMA_MAX_ORDER: int = 4; HMM_N_STATES: int = 3; HMM_N_ITER: int = 100
    ATR_PERIOD: int = 14; MAX_POSITION_RISK: float = 0.02; ACCOUNT_SIZE: float = 500_000_000
    FORECAST_HORIZON: int = 10; MONTE_CARLO_SIMS: int = 1000; CONFIDENCE_LEVEL: float = 0.95
    NORMALITY_ALPHA: float = 0.05
    # Settlement configuration is defined once in the V6 market section below.
    SWING_HORIZON_MIN: int = 7
    SWING_HORIZON_MAX: int = 20
    SWING_DEFAULT: int = 10
    TICK_SIZE_RULES: dict = field(default_factory=lambda: {
        10000: 10, 50000: 50, 999999999: 100
    })
    # === V4 NEW CONFIG ===
    HMM_N_INITS: int = 10          # Multiple random inits for robustness
    HMM_VOL_WINDOW: int = 10       # Rolling vol window for HMM features
    HMM_MOM_WINDOW: int = 5        # Short momentum window for HMM features
    SR_EXTREMA_ORDER: int = 3      # argrelextrema order for swing detection
    # === V4.1 BUG FIXES ===
    VNI_CRASH_1D_PCT: float = -0.02     # 1-day drop to flag crash
    VNI_CRASH_5D_PCT: float = -0.05     # 5-day drop to flag crash
    CRISIS_VOL_MULT: float = 2.0        # Multiply GARCH vol in crisis
    FORECAST_CAP_CRISIS: float = 2.0    # Max forecast % in crisis regime
    ABSOLUTE_SCORE_ANCHOR: float = 0.5  # Blend: 50% cross-sectional + 50% absolute
    MC_SEED_PER_SYMBOL: bool = True     # Deterministic per-symbol seed

    # === V6 VIETNAM MARKET / PRODUCTION SAFETY ===
    PRICE_INPUT_UNIT: str = 'THOUSAND_VND'  # KBS/VCI via vnstock_data: giá thường ở nghìn VND
    PRICE_OUTPUT_UNIT: str = 'VND'          # Chuẩn nội bộ duy nhất của pipeline
    DEFAULT_EXCHANGE: str = 'HOSE'
    SETTLEMENT_DAYS: int = 2                # Cổ phiếu/CCQ/CW: T+2
    MIN_HOLD_SESSIONS: int = 2              # Daily-bar approximation: sớm nhất bán chiều T+2
    BOARD_LOT: int = 100

    # Data quality
    MAX_STALE_BUSINESS_DAYS: int = 5
    MAX_ZERO_VOLUME_20D_PCT: float = 20.0
    MAX_MISSING_BUSINESS_DAY_PCT: float = 15.0
    PRICE_LIMIT_TOLERANCE: float = 0.015

    # Liquidity / capacity
    MIN_ADV20_VALUE_VND: float = 5_000_000_000
    MAX_ADV20_PARTICIPATION: float = 0.05
    MAX_POSITION_PCT: float = 0.15

    # Execution-cost assumptions (broker commission is configurable)
    COMMISSION_BPS_PER_SIDE: float = 15.0
    SELL_TAX_BPS: float = 10.0
    BASE_SLIPPAGE_BPS_PER_SIDE: float = 8.0
    MIN_NET_FORECAST_PCT: float = 1.0

    # Forecast robustness
    FORECAST_DRIFT_SHRINK: float = 0.25
    ENABLE_INTERNAL_STOP_OPTIMIZATION: bool = False  # Backtest/tuning thuộc file riêng
    GENERATE_VISUALS: bool = False
    VISUAL_TOP_N: int = 10
    VISUAL_OUTPUT_DIR: str = 'output'

    # === V7 AGREEMENT ARCHITECTURE ===
    # Directional alpha is deliberately separated from distribution/risk models.
    MOMENTUM_ALPHA_WEIGHT: float = 0.40
    LIGHTGBM_ALPHA_WEIGHT: float = 0.45
    CONDITIONAL_MR_WEIGHT: float = 0.15
    ALPHA_NEUTRAL_RET_PCT: float = 0.50
    AGREEMENT_MIN_ACTIVE_MODELS: int = 2
    AGREEMENT_MIN_COVERAGE_PCT: float = 60.0
    AGREEMENT_MIN_PCT: float = 65.0
    AGREEMENT_MIN_SUPPORT_PCT: float = 25.0
    HURST_TREND_THRESHOLD: float = 0.55
    HURST_REVERSION_THRESHOLD: float = 0.45
    CONDITIONAL_MR_Z_MIN: float = 1.25
    LIGHTGBM_MIN_TRAIN_ROWS: int = 400
    LIGHTGBM_MIN_SYMBOLS: int = 5
    META_LABEL_MIN_SAMPLES: int = 80
    META_READY_PROB: float = 0.58
    META_WATCH_PROB: float = 0.50
    FORECAST_LOG_PATH: str = 'forecast_log.csv'


_CURRENT = ContextVar("quantik_quant_config", default=replace(QuantConfig(), PRICE_INPUT_UNIT="VND"))

class ConfigProxy:
    def __getattr__(self, name):
        return getattr(_CURRENT.get(), name)

CFG = ConfigProxy()


def analysis_window(frame, config: QuantConfig):
    """Prototype fetch_ohlcv/fetch_index retain the latest lookback sessions.

    Ingestion and training keep their full immutable history. Only analysis
    inputs are bounded here, consistently for reference and customer execution.
    """
    if config.LOOKBACK_DAYS < config.MIN_HISTORY:
        raise ValueError("Analysis lookback is shorter than required history")
    return frame.tail(config.LOOKBACK_DAYS).copy()

@contextmanager
def engine_context(config: QuantConfig):
    if config.PRICE_INPUT_UNIT != "VND" or config.ENABLE_INTERNAL_STOP_OPTIMIZATION:
        raise ValueError("Service engines require VND and disabled stop optimization")
    token = _CURRENT.set(config)
    try:
        yield
    finally:
        _CURRENT.reset(token)

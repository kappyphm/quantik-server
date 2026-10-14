"""Pure engines extracted from preserved prototype; changes belong in service orchestration.
Baseline SHA256: 6b850837fadaa28010c0a3e748c97e16a2b5e9d32e2d3ed6c9b4817edc728d24
"""
import logging, hashlib, math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from service.quant.configuration import CFG
log = logging.getLogger(__name__)

class VNMarketRules:
    """Quy tắc giao dịch cơ sở Việt Nam. Tất cả giá đầu vào của class này là VND."""
    PROFILES = {
        'HOSE': {'aliases': {'HOSE','HSX','HOCHIMINH'}, 'limit_pct': 0.07, 'lot_size': 100},
        'HNX': {'aliases': {'HNX','HANOI'}, 'limit_pct': 0.10, 'lot_size': 100},
        'UPCOM': {'aliases': {'UPCOM','UPCO','UP'}, 'limit_pct': 0.15, 'lot_size': 100},
    }

    @classmethod
    def normalize_exchange(cls, exchange):
        x = str(exchange or 'UNKNOWN').upper().replace(' ', '')
        for name, profile in cls.PROFILES.items():
            if x == name or x in profile['aliases']:
                return name
        return 'UNKNOWN'

    @classmethod
    def profile(cls, exchange):
        ex = cls.normalize_exchange(exchange)
        if ex not in cls.PROFILES:
            raise ValueError(f'Unknown exchange: {exchange}')
        return cls.PROFILES[ex]

    @classmethod
    def tick_size(cls, price_vnd, exchange='HOSE'):
        ex = cls.normalize_exchange(exchange)
        cls.profile(ex)  # Never silently apply a different exchange's tick rules.
        p = float(price_vnd)
        if ex == 'HOSE':
            if p < 10_000: return 10.0
            if p < 50_000: return 50.0
            return 100.0
        return 100.0

    @classmethod
    def round_price(cls, price_vnd, direction='nearest', exchange='HOSE'):
        if price_vnd is None or not np.isfinite(price_vnd):
            return None
        tick = cls.tick_size(price_vnd, exchange)
        q = float(price_vnd) / tick
        if direction == 'down': q = math.floor(q + 1e-12)
        elif direction == 'up': q = math.ceil(q - 1e-12)
        else: q = round(q)
        return float(q * tick)

    @classmethod
    def price_band(cls, reference_price_vnd, exchange='HOSE'):
        limit_pct = cls.profile(exchange)['limit_pct']
        floor_p = cls.round_price(reference_price_vnd * (1 - limit_pct), 'up', exchange)
        ceil_p = cls.round_price(reference_price_vnd * (1 + limit_pct), 'down', exchange)
        return {'floor': floor_p, 'ceiling': ceil_p, 'limit_pct': limit_pct}


class CostEngine:
    """Chi phí ước tính; commission/slippage là giả định cấu hình, không phải phí cố định toàn thị trường."""
    TIER_SLIPPAGE_BPS = {'A': 4.0, 'B': 7.0, 'C': 12.0, 'D': 25.0}

    @classmethod
    def analyze(cls, forecast=None, liquidity=None):
        forecast = forecast or {}; liquidity = liquidity or {}
        tier = liquidity.get('tier', 'D')
        slip = max(CFG.BASE_SLIPPAGE_BPS_PER_SIDE, cls.TIER_SLIPPAGE_BPS.get(tier, 25.0))
        roundtrip_bps = 2 * CFG.COMMISSION_BPS_PER_SIDE + CFG.SELL_TAX_BPS + 2 * slip
        gross = float(forecast.get('ensemble_ret_pct', 0) or 0)
        cost_pct = roundtrip_bps / 100.0
        return {
            'commission_bps_per_side': CFG.COMMISSION_BPS_PER_SIDE,
            'sell_tax_bps': CFG.SELL_TAX_BPS,
            'slippage_bps_per_side': slip,
            'roundtrip_cost_bps': round(roundtrip_bps, 1),
            'roundtrip_cost_pct': round(cost_pct, 3),
            'gross_forecast_pct': round(gross, 3),
            'net_forecast_pct': round(gross - cost_pct, 3),
            'breakeven_move_pct': round(cost_pct, 3),
        }


class DataQualityEngine:
    @staticmethod
    def audit(df, exchange='HOSE', as_of=None):
        if df is None or df.empty:
            return {'status': 'FAIL', 'score': 0, 'flags': ['NO_DATA']}
        flags = []; score = 100
        required = ['open','high','low','close','volume']
        missing_cols = [c for c in required if c not in df.columns]
        if missing_cols:
            return {'status':'FAIL','score':0,'flags':[f'MISSING_COLS:{missing_cols}']}

        numeric = df[required].apply(pd.to_numeric, errors='coerce')
        if not np.isfinite(numeric.to_numpy(dtype=float)).all():
            return {'status': 'FAIL', 'score': 0, 'flags': ['NONFINITE_OHLCV']}
        if (numeric['volume'] < 0).any():
            return {'status': 'FAIL', 'score': 0, 'flags': ['NEGATIVE_VOLUME']}
        if not isinstance(df.index, pd.DatetimeIndex) or df.index.hasnans:
            return {'status': 'FAIL', 'score': 0, 'flags': ['INVALID_DATE_INDEX']}
        if not df.index.is_monotonic_increasing:
            return {'status': 'FAIL', 'score': 0, 'flags': ['UNSORTED_DATES']}
        df = numeric
        audit_day = pd.Timestamp(as_of if as_of is not None else datetime.now()).date()
        if df.index.max().date() > audit_day:
            return {'status': 'FAIL', 'score': 0, 'flags': ['FUTURE_DATA']}

        n = len(df)
        if n < CFG.MIN_HISTORY:
            flags.append(f'SHORT_HISTORY:{n}<{CFG.MIN_HISTORY}'); score -= 35
        dup = int(df.index.duplicated().sum())
        if dup:
            flags.append(f'DUPLICATE_DATES:{dup}'); score -= min(20, dup * 2)

        bad_ohlc = int(((df['high'] < df[['open','close']].max(axis=1)) |
                        (df['low'] > df[['open','close']].min(axis=1)) |
                        (df['high'] < df['low'])).sum())
        nonpos = int((df[['open','high','low','close']] <= 0).any(axis=1).sum())
        if bad_ohlc: flags.append(f'INVALID_OHLC:{bad_ohlc}'); score -= min(35, bad_ohlc * 5)
        if nonpos: flags.append(f'NONPOSITIVE_PRICE:{nonpos}'); score -= min(40, nonpos * 10)

        zero_vol_pct = float((df['volume'].tail(20) <= 0).mean() * 100)
        if zero_vol_pct > CFG.MAX_ZERO_VOLUME_20D_PCT:
            flags.append(f'ZERO_VOLUME_20D:{zero_vol_pct:.1f}%'); score -= 20

        try:
            first = pd.Timestamp(df.index.min()).date(); last = pd.Timestamp(df.index.max()).date()
            expected = max(int(np.busday_count(first, last)) + 1, 1)
            missing_pct = max(0.0, (expected - n) / expected * 100)
        except Exception:
            missing_pct = 0.0
        if missing_pct > CFG.MAX_MISSING_BUSINESS_DAY_PCT:
            flags.append(f'MISSING_SESSIONS_APPROX:{missing_pct:.1f}%'); score -= 15

        try:
            last_date = np.datetime64(pd.Timestamp(df.index.max()).date())
            today = np.datetime64(audit_day)
            stale_bd = max(int(np.busday_count(last_date, today)), 0)
        except Exception:
            stale_bd = 0
        if stale_bd > CFG.MAX_STALE_BUSINESS_DAYS:
            flags.append(f'STALE_DATA:{stale_bd}BD'); score -= 35

        limit_pct = VNMarketRules.profile(exchange)['limit_pct']
        ret = df['close'].pct_change().abs()
        jump_count = int((ret > limit_pct + CFG.PRICE_LIMIT_TOLERANCE).sum())
        if jump_count:
            flags.append(f'PRICE_JUMP_OR_CORP_ACTION:{jump_count}'); score -= min(15, jump_count * 3)

        score = int(np.clip(score, 0, 100))
        fatal = any(x.startswith(('INVALID', 'NONPOSITIVE', 'STALE', 'DUPLICATE')) for x in flags)
        status = 'FAIL' if fatal or score < 55 else ('PASS' if score >= 80 else 'WARN')
        return {
            'status': status, 'score': score, 'flags': flags, 'n_rows': n,
            'zero_volume_20d_pct': round(zero_vol_pct, 1),
            'missing_sessions_approx_pct': round(missing_pct, 1),
            'stale_business_days': stale_bd, 'price_jump_count': jump_count,
            'price_unit': 'VND',
        }


class LiquidityEngine:
    @staticmethod
    def analyze(df, exchange='HOSE'):
        if df is None or len(df) < 20:
            return {'tier':'D','pass':False,'reason':'INSUFFICIENT_DATA'}
        close = df['close'].astype(float); vol = df['volume'].astype(float)
        value = close * vol
        adv20_sh = float(vol.tail(20).mean())
        adv20_val = float(value.tail(20).mean())
        med20_val = float(value.tail(20).median())
        value_ratio_5_20 = float(value.tail(5).mean() / adv20_val) if adv20_val > 0 else 0
        zero_vol_pct = float((vol.tail(20) <= 0).mean() * 100)
        ret = close.pct_change()
        prev = close.shift(1)
        gap = (df['open'] / prev - 1).replace([np.inf,-np.inf], np.nan).dropna()
        gap_p95 = float(gap.abs().quantile(0.95) * 100) if len(gap) else 0
        limit_pct = VNMarketRules.profile(exchange)['limit_pct']
        near_ceiling_60d = int((ret.tail(60) >= limit_pct * 0.97).sum())
        near_floor_60d = int((ret.tail(60) <= -limit_pct * 0.97).sum())
        amihud = (ret.abs() / (value / 1e9).replace(0, np.nan)).tail(60).mean()
        amihud = float(amihud) if pd.notna(amihud) else None

        if adv20_val >= 50e9: tier = 'A'
        elif adv20_val >= 20e9: tier = 'B'
        elif adv20_val >= CFG.MIN_ADV20_VALUE_VND: tier = 'C'
        else: tier = 'D'
        liq_pass = bool(adv20_val >= CFG.MIN_ADV20_VALUE_VND and zero_vol_pct <= CFG.MAX_ZERO_VOLUME_20D_PCT)
        lot = VNMarketRules.profile(exchange)['lot_size']
        capacity_shares = int((adv20_sh * CFG.MAX_ADV20_PARTICIPATION) // lot) * lot
        return {
            'tier': tier, 'pass': liq_pass, 'adv20_shares': round(adv20_sh, 0),
            'adv20_value_vnd': round(adv20_val, 0), 'median20_value_vnd': round(med20_val, 0),
            'value_ratio_5_20': round(value_ratio_5_20, 2), 'zero_volume_20d_pct': round(zero_vol_pct,1),
            'capacity_shares': max(capacity_shares, 0), 'max_adv_participation_pct': CFG.MAX_ADV20_PARTICIPATION * 100,
            'gap_abs_p95_pct': round(gap_p95, 2), 'near_ceiling_60d': near_ceiling_60d,
            'near_floor_60d': near_floor_60d, 'amihud_60d_per_bn': round(amihud, 6) if amihud is not None else None,
        }


class ActionEngine:
    """Tách xếp hạng (score) khỏi hành động. Hard gates luôn thắng score."""
    @staticmethod
    def decide(report):
        rc = report.get('rec',{}); fc = report.get('fcast',{}); dq = report.get('data_quality',{})
        liq = report.get('liquidity',{}); costs = report.get('costs',{}); sl = report.get('sl',{})
        score = int(rc.get('score',0)); vni = rc.get('vni_regime','UNKNOWN')
        timing = fc.get('timing',''); net_fc = costs.get('net_forecast_pct', fc.get('ensemble_ret_pct',0))
        reasons = []
        try:
            net_fc = float(net_fc)
        except (TypeError, ValueError):
            net_fc = float('nan')
        if not np.isfinite(net_fc): reasons.append('INVALID_FORECAST')
        if VNMarketRules.normalize_exchange(report.get('exchange')) == 'UNKNOWN': reasons.append('UNKNOWN_EXCHANGE')
        if vni == 'UNKNOWN': reasons.append('VNI_DATA_MISSING')
        if dq.get('status') not in ('PASS', 'WARN'): reasons.append('DATA_QUALITY_FAIL')
        if not liq.get('pass', False): reasons.append('LIQUIDITY_FAIL')
        if vni == 'CRISIS': reasons.append('VNI_CRISIS')
        if fc.get('timing_status') == 'BLOCKED': reasons.append('TIMING_BLOCKED')
        if sl.get('lock_risk_flag'): reasons.append('SETTLEMENT_LOCK_RISK')
        if net_fc < CFG.MIN_NET_FORECAST_PCT: reasons.append('NET_FORECAST_TOO_LOW')

        levels = [sl.get(k) for k in ('sl_swing', 'entry', 'tp1', 'tp2')]
        try:
            levels = [float(x) for x in levels]
            valid_plan = all(np.isfinite(x) for x in levels) and 0 < levels[0] < levels[1] < levels[2] < levels[3]
        except (TypeError, ValueError):
            valid_plan = False
        if not valid_plan or sl.get('plan_valid') is False: reasons.append('INVALID_TRADE_PLAN')
        if not (report.get('pos',{}).get('shares',0) > 0): reasons.append('ZERO_POSITION_SIZE')
        timing_ready = fc.get('timing_status') == 'READY'
        if not timing_ready: reasons.append('TIMING_NOT_READY')

        hard_block = any(x in reasons for x in ('DATA_QUALITY_FAIL','LIQUIDITY_FAIL','VNI_CRISIS','TIMING_BLOCKED',
                                               'INVALID_FORECAST','UNKNOWN_EXCHANGE','VNI_DATA_MISSING',
                                               'INVALID_TRADE_PLAN','ZERO_POSITION_SIZE'))
        if hard_block:
            action = 'AVOID'
        elif score >= 75 and vni not in ('BEAR','WEAK') and not reasons:
            action = 'BUY_NOW'
        elif score >= 65 and net_fc >= CFG.MIN_NET_FORECAST_PCT and vni != 'BEAR':
            # No explicit entry trigger yet: remain WATCH, never fabricate a setup.
            action = 'WATCH'
        elif score >= 50 and net_fc > 0:
            action = 'WATCH'
        else:
            action = 'AVOID'

        gate_pass = action == 'BUY_NOW'
        return {'action': action, 'gate_pass': gate_pass, 'executable': gate_pass,
                'reason_codes': reasons,
                'net_forecast_pct': round(net_fc,3) if np.isfinite(net_fc) else None}


class SectorEngine:
    _icb_cache = None  # {symbol: icb_name} — cache 1 lần / process
    _watchlist_cache = None  # {symbol: ten_nhom_watchlist} — fallback thủ công

    ICB_LEVEL = 2
    TREND_LOOKBACKS = {'5D': 5, '20D': 20, '60D': 60}  # phiên

    # Watchlist phân nhóm ngành thủ công (Watchlist.xlsx) — dùng làm FALLBACK
    # khi ICB map không có mã (hoặc load ICB lỗi).
    #
    # PURE_SECTORS: các ngành ICB-kiểu chuẩn, gần như KHÔNG trùng mã nhau
    # (chỉ còn vài mã trùng nhẹ do doanh nghiệp đa ngành thật sự, ví dụ PVI
    # là bảo hiểm thuộc PVN nên nằm cả Dầu khí lẫn Bảo hiểm) — dùng để tính
    # Tỷ trọng (%) GTGD toàn thị trường, KHÔNG bị double-count.
    #
    # THEMATIC_GROUPS: các nhóm chuyên đề cắt ngang (theo hệ sinh thái/quỹ
    # theo dõi riêng), CỐ Ý trùng mã với PURE_SECTORS và với nhau (vd FPT
    # vừa ở Công nghệ vừa ở FPT nhóm vừa ở Bán lẻ) — chỉ để theo dõi riêng,
    # KHÔNG cộng vào tổng GTGD thị trường vì sẽ đếm trùng.
    PURE_SECTORS = {
        'Ngân hàng (BANK)': ['LPB','VIB','NVB','BID','STB','HDB','TPB','MBB','ACB','TCB','VPB','VCB','CTG','NAB','EVF','ABB','MSB','EIB','SSB','BVB','SHB','VAB','BAB','OCB','KLB'],
        'Bất động sản (BĐS)': ['DPG','CII','DIG','KBC','IDC','CEO','HHS','VGC','DXG','HDG','TCH','HQC','NHA','SGR','DXS','QCG','PDR','HDC','KDH','KHG','VPI','NTL','NVL','VC7','TIG','SCR','CKG','NLG','AGG','L14','HPX','DC4','TAL'],
        'Chứng khoán': ['BVS','SBS','MBS','BSI','VCI','AAS','SHS','PSI','CTS','VFS','SSI','ABW','AGR','VIG','IPA','FTS','VIX','VCK','VDS','VPX','ORS','TCX','DSE','APG'],
        'Thép': ['NKG','VGS','HSG','TVN','TLH','HPG','HSV'],
        'XD-VLXD-Đầu tư công': ['VCG','FCN','G36','HHV','LCG','C4G','DPG','CII','HBC','CTI','VLB','KSB','MSR','SZC','BMP','CTD','IJC','VCS','BCC','C32','GDA','EVG','DC4','HT1','NTP'],
        'Dầu khí': ['PVD','PLC','PVS','PVC','PVT','LAS','PVB','GAS','OIL','BSR','PVI','PXL'],
        'Phân bón - Hoá chất': ['DGC','PHR','GVR','DPM','LAS','DCM','CSV','VFG','BFC','DHB','DDV'],
        'Cảng biển': ['PVP','PVT','VSC','SGP','MVN','GMD','VOS','TCL','DVP','PHP','HAH'],
        'Dệt may': ['VGT','TNG','MSH','HDM','GIL','TCM'],
        'Không thiết yếu': ['STH','DHC','VEA','PAN','DLG','YEG'],
        'Công nghệ': ['ELC','VTP','CTR','CMG','FPT','VGI'],
        'Hàng không': ['HVN','SCS','ACV','VJC','ATS'],
        'Điện': ['PC1','GEE','TV2','HDG','REE','TV1','GEX','GEG','NT2','POW','QTP','DRL','PPC'],
        'Bảo hiểm': ['BVH','BIC','PVI','MIG'],
        'Dược phẩm': ['DVM','DCL'],
        'Bán lẻ': ['PET','DGW','MSN','FRT','MWG','TLG','VNM','PNJ','FPT','HAX','SAB'],
        'Thuỷ sản': ['ASM','FMC','IDI','ACL','VHC','CMX','ANV'],
    }

    THEMATIC_GROUPS = {
        'BĐS Khu công nghiệp': ['PHR','KBC','CTI','GVR','IDC','VGC','SZC','CTD','SIP','IJC','LHG','BCM','TIP','NTC'],
        'Viettel': ['VTK','VTP','CTR','VGI'],
        'FPT nhóm': ['FRT','FOC','FTS','FPT','FOX'],
        'Vingroup': ['VPL','VRE','VHM','VIC'],
        'Tự doanh Tuấn Mượt': ['PC1','PET','GEE','VSC','VGC','MHC','GEX','EVF','BSR','EIB','GEL','VIX','PXL','HAH'],
        'List CMSC': ['PVD','PVS','HVN','PGS','PVT','SFG','PVB','GVR','DPM','LAS','MBB','DCM','PLX','GAS','OIL','ACV','BSR','PAC','CSV','POW','DPR','TRC','DHG','VNM','FPT','FOX','DDV','RTB','BRR'],
    }

    # Giữ WATCHLIST_SECTORS gộp cả 2 để tương thích code cũ (đọc mã 227 mã
    # phục vụ fetch_ohlcv) — CHỈ dùng compute_sector_cashflow() riêng với
    # PURE_SECTORS / THEMATIC_GROUPS, không dùng dict gộp này để tính GTGD.
    WATCHLIST_SECTORS = {**PURE_SECTORS, **THEMATIC_GROUPS}




    @staticmethod
    def log_returns(price_series):
        """ln(P_t / P_t-1) — quy về tốc độ tăng trưởng, cộng dồn được qua thời gian."""
        return np.log(price_series / price_series.shift(1)).dropna()

    @classmethod
    def sector_trend(cls, data: dict, sector_map: dict):
        """Xu hướng từng nhóm ngành dựa trên log-return trung bình của các mã cùng ngành.

        data: {symbol: ohlcv_df}; sector_map: {symbol: ten_nganh}
        Trả về {ten_nganh: {...}}
        """
        groups = {}
        for sym, sector in sector_map.items():
            df = data.get(sym)
            if df is None or len(df) < 30:
                continue
            groups.setdefault(sector, []).append(sym)

        result = {}
        for sector, syms in groups.items():
            rets = {s: cls.log_returns(data[s]['close']) for s in syms}
            ret_df = pd.DataFrame(rets).dropna(how='all')
            if ret_df.empty:
                continue
            avg_ret = ret_df.mean(axis=1)  # log-return trung bình ngành / phiên

            out = {'symbols': syms, 'n_symbols': len(syms)}
            for label, w in cls.TREND_LOOKBACKS.items():
                if len(avg_ret) >= w:
                    cum = avg_ret.tail(w).sum()
                    out[f'ret_{label}_pct'] = round(float((np.exp(cum) - 1) * 100), 2)
                else:
                    out[f'ret_{label}_pct'] = None

            m = out.get('ret_20D_pct')
            if m is None:
                m = out.get('ret_5D_pct') or 0
            if m > 3: tl = '🟢 TĂNG MẠNH'
            elif m > 0.5: tl = '🟢 TĂNG'
            elif m > -0.5: tl = '⚪ ĐI NGANG'
            elif m > -3: tl = '🔴 GIẢM'
            else: tl = '🔴 GIẢM MẠNH'
            out['trend_label'] = tl
            out['avg_daily_log_ret'] = round(float(avg_ret.mean()), 6)
            result[sector] = out
        return result


class CrossCorrelationEngine:
    MAX_LAG = 2
    MIN_OBS = 40          # tối thiểu số quan sát chung để tính CCF/VAR đáng tin
    MAX_SYMS_VAR = 8      # giới hạn số mã/VAR để tránh overfit (bậc tự do)
    VAR_MAXLAGS = 5

    @staticmethod
    def _build_return_matrix(data: dict, symbols: list):
        rets = {}
        for s in symbols:
            df = data.get(s)
            if df is not None and len(df) >= 30:
                rets[s] = SectorEngine.log_returns(df['close'])
        if len(rets) < 2:
            return None
        mat = pd.DataFrame(rets).dropna()
        return mat if len(mat) >= CrossCorrelationEngine.MIN_OBS else None

    @classmethod
    def lagged_ccf(cls, ret_mat: pd.DataFrame):
        """CCF lag -2..+2 cho mọi cặp mã. Trả về (pair_ccf, lead_lag_summary)."""
        cols = ret_mat.columns.tolist()
        pair_ccf, lead_lag_summary = {}, []
        for i, x in enumerate(cols):
            for y in cols[i+1:]:
                lags = {}
                for lag in range(-cls.MAX_LAG, cls.MAX_LAG + 1):
                    joined = pd.concat([ret_mat[x], ret_mat[y].shift(-lag)], axis=1).dropna()
                    if len(joined) < 20:
                        lags[lag] = None; continue
                    c = joined.iloc[:, 0].corr(joined.iloc[:, 1])
                    lags[lag] = round(float(c), 4) if pd.notna(c) else None
                pair_ccf[(x, y)] = lags
                valid = {k: v for k, v in lags.items() if v is not None}
                if valid:
                    best_lag = max(valid, key=lambda k: abs(valid[k]))
                    best_corr = valid[best_lag]
                    if best_lag > 0: relation = f'{x} dẫn dắt {y} (trễ {best_lag} phiên)'
                    elif best_lag < 0: relation = f'{y} dẫn dắt {x} (trễ {abs(best_lag)} phiên)'
                    else: relation = f'{x} và {y} biến động ĐỒNG THỜI'
                    lead_lag_summary.append({'pair': f'{x}-{y}', 'best_lag': best_lag,
                                              'best_corr': best_corr, 'relation': relation})
        return pair_ccf, lead_lag_summary

    @staticmethod
    def correlation_matrix(ret_mat: pd.DataFrame):
        return ret_mat.corr().round(4)

    @classmethod
    def fit_var(cls, ret_mat: pd.DataFrame):
        """VAR theo ngành (giới hạn MAX_SYMS_VAR mã có variance cao nhất) + Granger causality."""
        try:
            from statsmodels.tsa.api import VAR
        except ImportError:
            return {'error': 'pip install statsmodels'}

        cols = ret_mat.columns.tolist()
        if len(cols) > cls.MAX_SYMS_VAR:
            cols = ret_mat.var().sort_values(ascending=False).head(cls.MAX_SYMS_VAR).index.tolist()
        sub = ret_mat[cols].dropna()
        if len(sub) < cls.MIN_OBS or len(cols) < 2:
            return {'error': 'Không đủ dữ liệu cho VAR'}
        try:
            model = VAR(sub)
            maxlags = max(min(cls.VAR_MAXLAGS, len(sub)//10 or 1), 1)
            sel = model.select_order(maxlags=maxlags)
            order = sel.aic if sel.aic and sel.aic > 0 else 1
            res = model.fit(order)
            granger = {}
            for y in cols:
                for x in cols:
                    if x == y: continue
                    try:
                        gc = res.test_causality(y, [x], kind='f')
                        granger[f'{x}->{y}'] = {'p_value': round(float(gc.pvalue), 4),
                                                 'significant': bool(gc.pvalue < 0.05)}
                    except Exception:
                        continue
            return {'symbols': cols, 'order': int(order), 'aic': round(float(res.aic), 4),
                     'granger_causality': granger}
        except Exception as e:
            return {'error': str(e)}

    @classmethod
    def analyze_sector(cls, data: dict, symbols: list):
        """Chạy full: corr matrix + lagged CCF + VAR cho 1 nhóm ngành."""
        ret_mat = cls._build_return_matrix(data, symbols)
        if ret_mat is None or ret_mat.shape[1] < 2:
            return {'error': 'Không đủ mã/dữ liệu chung trong ngành để tính tương quan',
                    'avg_pairwise_corr': None}
        corr = cls.correlation_matrix(ret_mat)
        _, lead_lag = cls.lagged_ccf(ret_mat)
        var_res = cls.fit_var(ret_mat)
        vals = corr.values; n = vals.shape[0]
        off_diag = vals[~np.eye(n, dtype=bool)]
        avg_corr = round(float(np.nanmean(off_diag)), 4) if len(off_diag) else None
        return {
            'symbols': ret_mat.columns.tolist(), 'n_obs': len(ret_mat),
            'avg_pairwise_corr': avg_corr, 'corr_matrix': corr.to_dict(),
            'lead_lag': sorted(lead_lag, key=lambda d: abs(d['best_corr'] or 0), reverse=True),
            'var': var_res,
        }


class DistributionAnalyzer:
    @staticmethod
    def full_test(prices):
        from scipy import stats
        ret = prices.pct_change().dropna().values
        if len(ret) < 30: return {'error': 'Need 30+ obs'}
        r = {'n': len(ret), 'mean': round(float(np.mean(ret)),6), 'std': round(float(np.std(ret)),6),
             'skewness': round(float(stats.skew(ret)),4), 'excess_kurtosis': round(float(stats.kurtosis(ret)),4)}
        jb_s, jb_p = stats.jarque_bera(ret)
        sw_s, sw_p = stats.shapiro(ret[:5000])
        ad = stats.anderson(ret, dist='norm')
        dp_s, dp_p = stats.normaltest(ret) if len(ret) >= 20 else (0, 1)
        mu, sig = np.mean(ret), np.std(ret)
        ks_s, ks_p = stats.kstest(ret, stats.norm.cdf, args=(mu, sig))
        tests = {
            'jarque_bera': {'stat': round(float(jb_s),4), 'p': round(float(jb_p),6), 'reject': bool(jb_p < CFG.NORMALITY_ALPHA)},
            'shapiro_wilk': {'stat': round(float(sw_s),6), 'p': round(float(sw_p),6), 'reject': bool(sw_p < CFG.NORMALITY_ALPHA)},
            'anderson_darling': {'stat': round(float(ad.statistic),4), 'crit_5pct': round(float(ad.critical_values[2]),4),
                                 'reject': bool(ad.statistic > ad.critical_values[2])},
            'dagostino': {'stat': round(float(dp_s),4), 'p': round(float(dp_p),6), 'reject': bool(dp_p < CFG.NORMALITY_ALPHA)},
            'ks_test': {'stat': round(float(ks_s),4), 'p': round(float(ks_p),6), 'reject': bool(ks_p < CFG.NORMALITY_ALPHA)},
        }
        r['tests'] = tests
        ek = stats.kurtosis(ret)
        p2 = np.mean(np.abs(ret) > 2*sig); n2 = 2*(1-stats.norm.cdf(2))
        p3 = np.mean(np.abs(ret) > 3*sig); n3 = 2*(1-stats.norm.cdf(3))
        try:
            ar = np.abs(ret); th = np.quantile(ar, 0.95); exc = ar[ar > th]
            hill = len(exc)/np.sum(np.log(exc/th)) if len(exc)>=5 and np.sum(np.log(exc/th))>0 else None
        except: hill = None
        r['fat_tail'] = {
            'excess_kurtosis': round(float(ek),4),
            'severity': 'EXTREME' if ek>10 else 'STRONG' if ek>5 else 'MODERATE' if ek>1 else 'MILD' if ek>0 else 'THIN',
            'tail_ratio_2sigma': round(float(p2/n2),2) if n2>0 else 1,
            'tail_ratio_3sigma': round(float(p3/n3),2) if n3>0 else 1,
            'prob_beyond_3sigma_pct': round(float(p3)*100,3),
            'hill_index': round(float(hill),3) if hill else None,
        }
        norm_p = stats.norm.fit(ret); norm_aic = 4 - 2*np.sum(stats.norm.logpdf(ret, *norm_p))
        t_p = stats.t.fit(ret); t_aic = 6 - 2*np.sum(stats.t.logpdf(ret, *t_p))
        lap_p = stats.laplace.fit(ret); lap_aic = 4 - 2*np.sum(stats.laplace.logpdf(ret, *lap_p))
        fits = {'normal': norm_aic, 'student_t': t_aic, 'laplace': lap_aic}
        best = min(fits, key=fits.get)
        r['best_fit'] = {'winner': best, 'aic': {k: round(v,2) for k,v in fits.items()}, 't_df': round(float(t_p[0]),2)}
        reject_n = sum(t['reject'] for t in tests.values())
        r['verdict'] = {
            'reject_count': reject_n, 'is_gaussian': reject_n <= 1,
            'conclusion': ('Returns GAN Gaussian' if reject_n <= 1 else
                          'Returns KHONG Gaussian - can models robust' if reject_n <= 3 else
                          'Returns RAT KHAC Gaussian - fat-tail manh'),
            'model_rec': ('Standard OK' if reject_n <= 1 else
                         'Student-t VaR, GARCH-t, Historical Sim' if reject_n <= 3 else
                         'EVT, GPD tail, non-parametric'),
        }
        return r


class StatEngine:
    @staticmethod
    def returns(prices):
        if len(prices) < 20: return {}
        prices = prices.astype(float).dropna()
        ret = prices.pct_change().dropna()
        if ret.empty: return {}
        mr, sr = ret.mean(), ret.std(ddof=1)
        ann_mean = mr * 252
        av = sr * np.sqrt(252)
        years = max(len(ret) / 252.0, 1/252)
        total_ret = prices.iloc[-1] / prices.iloc[0] - 1 if prices.iloc[0] > 0 else 0
        cagr = (prices.iloc[-1] / prices.iloc[0]) ** (1/years) - 1 if prices.iloc[0] > 0 else 0
        conf = CFG.CONFIDENCE_LEVEL
        var_p = np.percentile(ret, (1-conf)*100)
        cvar_p = ret[ret <= var_p].mean() if len(ret[ret <= var_p]) else var_p
        up, dn = ret[ret > 0], ret[ret < 0]
        up_day_ratio = len(up) / len(ret)
        avg_up = up.mean() if len(up) else 0
        avg_down = abs(dn.mean()) if len(dn) else 0
        gross_up = up.sum(); gross_down = abs(dn.sum())
        daily_profit_factor = gross_up / gross_down if gross_down > 0 else None
        ex = ann_mean - CFG.RISK_FREE_RATE
        sharpe = ex / av if av > 0 else 0
        downside = np.sqrt(np.mean(np.minimum(ret.values, 0.0) ** 2)) * np.sqrt(252)
        sortino = ex / downside if downside > 0 else 0
        wealth = pd.concat([pd.Series([1.0]), (1 + ret).reset_index(drop=True).cumprod()], ignore_index=True)
        dd = wealth / wealth.cummax() - 1
        mdd = dd.min()
        calmar = cagr / abs(mdd) if mdd < 0 else 0
        kurt = ret.kurtosis()  # excess kurtosis
        return {
            'mean_daily_pct': round(mr*100,4), 'ann_mean_return_pct': round(ann_mean*100,2),
            'ann_return_pct': round(cagr*100,2), 'total_return_pct': round(total_ret*100,2),
            'ann_vol_pct': round(av*100,2), 'skewness': round(ret.skew(),3),
            'kurtosis': round(kurt,3), 'is_fat_tail': bool(kurt > 1),
            f'VaR_{int(conf*100)}': round(var_p*100,3),
            f'CVaR_{int(conf*100)}': round(cvar_p*100,3) if not pd.isna(cvar_p) else None,
            'max_dd_pct': round(mdd*100,2), 'sharpe': round(sharpe,3),
            'sortino': round(sortino,3), 'calmar': round(calmar,3),
            'win_rate_pct': round(up_day_ratio*100,1), 'up_day_ratio_pct': round(up_day_ratio*100,1),
            'profit_factor': round(daily_profit_factor,3) if daily_profit_factor is not None else None,
            'daily_profit_factor': round(daily_profit_factor,3) if daily_profit_factor is not None else None,
            'avg_win_pct': round(avg_up*100,3), 'avg_loss_pct': round(avg_down*100,3),
            'metric_scope': 'DAILY_PRICE_RETURNS_NOT_TRADES', 'n': len(ret)}

    @staticmethod
    def vol_regime(prices):
        ret = prices.pct_change().dropna()
        v10 = ret.rolling(CFG.VOL_WINDOW_SHORT).std()*np.sqrt(252)
        v20 = ret.rolling(CFG.VOL_WINDOW_MED).std()*np.sqrt(252)
        v60 = ret.rolling(CFG.VOL_WINDOW_LONG).std()*np.sqrt(252)
        c10,c20,c60 = v10.iloc[-1],v20.iloc[-1],v60.iloc[-1]
        vr = c10/c60 if c60>0 else 1
        vp = (v20<c20).mean()*100 if len(v20)>=60 else 50
        rg = 'CONTRACTION' if vr<0.7 else ('EXPANSION' if vr>1.3 else 'NORMAL')
        return {'vol_10d': round(c10*100,2) if not pd.isna(c10) else None,
                'vol_20d': round(c20*100,2) if not pd.isna(c20) else None,
                'vol_60d': round(c60*100,2) if not pd.isna(c60) else None,
                'vol_ratio': round(vr,3), 'vol_pctile': round(vp,1), 'regime': rg}

    @staticmethod
    def autocorr(prices, max_lag=10):
        ret = prices.pct_change().dropna()
        if len(ret)<max_lag+10: return {'behavior': 'INSUFFICIENT'}
        ac = {f'lag_{i}': round(ret.autocorr(lag=i),4) for i in range(1, max_lag+1)}
        avg = np.mean([ac.get(f'lag_{i}',0) for i in range(1,4)])
        th = 2/np.sqrt(len(ret))
        beh = 'MOMENTUM' if avg>th else ('MEAN_REVERSION' if avg<-th else 'RANDOM_WALK')
        return {'ac': ac, 'avg_short': round(avg,4), 'threshold': round(th,4), 'behavior': beh}


class ARIMAEngine:
    @staticmethod
    def fit(prices, max_p=4, max_q=4):
        try:
            from statsmodels.tsa.arima.model import ARIMA
            from statsmodels.tsa.stattools import adfuller
        except ImportError: return {'error': 'pip install statsmodels'}
        ret = prices.pct_change().dropna()
        if len(ret)<60: return {'error': 'Need 60+ obs'}
        adf_s, adf_p = adfuller(ret.values, maxlag=20)[:2]
        is_stat = adf_p < 0.05
        r = {'stationarity': {'adf_stat': round(adf_s,4), 'adf_p': round(adf_p,6), 'stationary': is_stat}}
        best_aic, best_order, best_m = np.inf, (0,0,0), None
        for p in range(0, max_p+1):
            for q in range(0, max_q+1):
                if p==0 and q==0: continue
                try:
                    m = ARIMA(ret.values, order=(p,0,q)).fit()
                    if m.aic < best_aic: best_aic, best_order, best_m = m.aic, (p,0,q), m
                except: pass
        if best_m is None: r['error'] = 'No ARIMA fit'; return r
        r['best'] = {'order': f'ARIMA{best_order}', 'aic': round(best_aic,2), 'bic': round(best_m.bic,2)}
        coefs = {}
        for nm, val, pv in zip(best_m.param_names, best_m.params, best_m.pvalues):
            coefs[nm] = {'val': round(float(val),6), 'p': round(float(pv),4), 'sig': bool(pv<0.05)}
        r['coefficients'] = coefs
        fc = best_m.forecast(steps=CFG.FORECAST_HORIZON)
        lp = prices.iloc[-1]; fp = [lp]
        for rt in fc: fp.append(fp[-1]*(1+rt))
        r['forecast'] = {'prices': [round(p,0) for p in fp[1:]], 'returns_pct': [round(x*100,3) for x in fc]}
        return r


class GARCHEngine:
    @staticmethod
    def fit(prices, p=1, q=1):
        try:
            from arch import arch_model
        except ImportError: return {'error': 'pip install arch'}
        ret = prices.pct_change().dropna()*100
        if len(ret)<100: return {'error': 'Need 100+ obs'}
        r = {}
        try:
            m = arch_model(ret, vol='Garch', p=p, q=q, dist='t').fit(disp='off')
            alpha = float(m.params.get('alpha[1]',0)); beta = float(m.params.get('beta[1]',0))
            persist = alpha+beta
            fc = m.forecast(horizon=CFG.FORECAST_HORIZON)
            cvol = np.sqrt(fc.variance.iloc[-1].values)
            r['garch'] = {
                'model': f'GARCH({p},{q})-t', 'aic': round(float(m.aic),2),
                'alpha': round(alpha,4), 'beta': round(beta,4), 'persistence': round(persist,4),
                'half_life': round(np.log(0.5)/np.log(persist),1) if 0<persist<1 else None,
                'persist_label': ('IGARCH-like' if persist>0.97 else 'High' if persist>0.9 else 'Moderate' if persist>0.7 else 'Low'),
                'current_vol_pct': round(float(m.conditional_volatility.iloc[-1]),3),
                'forecast_vol': [round(float(v),3) for v in cvol],
                'shock_sensitivity': 'HIGH' if alpha>0.15 else ('MODERATE' if alpha>0.08 else 'LOW'),
                # Unrounded values for path-wise Monte Carlo recursion. The
                # model is fitted to percentage returns, so omega is in pct^2.
                'simulation_params': {
                    'omega_pct2': float(m.params.get('omega', np.nan)),
                    'alpha': alpha,
                    'beta': beta,
                },
            }
            params = {}
            for nm in m.params.index:
                params[nm] = {'val': round(float(m.params[nm]),6), 'p': round(float(m.pvalues[nm]),4)}
            r['garch']['params'] = params
        except Exception as e: r['garch'] = {'error': str(e)}
        try:
            em = arch_model(ret, vol='EGARCH', p=1, o=1, q=1, dist='t').fit(disp='off')
            gamma = float(em.params.get('gamma[1]',0))
            r['egarch'] = {'aic': round(float(em.aic),2), 'gamma': round(gamma,4),
                           'leverage': bool(gamma<0),
                           'note': 'Tin xau tang vol MANH hon tin tot' if gamma<-0.05 else 'Leverage effect yeu'}
        except Exception as e: r['egarch'] = {'error': str(e)}
        return r


class HMMEngine:
    @staticmethod
    def _build_features(df):
        """Build multivariate feature matrix for HMM.
        4 features standardized to prevent scale dominance:
          1. Daily return         — directional
          2. Rolling vol (10d)    — vol regime
          3. Volume ratio (5/20)  — participation
          4. Short momentum (5d)  — trend smoothing
        """
        close = df['close']; volume = df['volume']
        ret = close.pct_change()
        rolling_vol = ret.rolling(CFG.HMM_VOL_WINDOW).std()
        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()
        vol_ratio = vol_ma5 / vol_ma20.replace(0, np.nan)
        short_mom = ret.rolling(CFG.HMM_MOM_WINDOW).mean()
        feat_df = pd.DataFrame({
            'return': ret, 'rolling_vol': rolling_vol,
            'vol_ratio': vol_ratio, 'short_mom': short_mom,
        }).dropna()
        if len(feat_df) < 100: return None, None
        values = feat_df.values.copy()
        means = values.mean(axis=0); stds = values.std(axis=0)
        stds[stds == 0] = 1.0
        values_z = (values - means) / stds
        return values_z, feat_df.index

    @staticmethod
    def fit(df, n=3, symbol=''):
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError: return HMMEngine._fallback(df)
        features, feat_idx = HMMEngine._build_features(df)
        if features is None: return HMMEngine._fallback(df)
        try:
            best_model, best_bic = None, np.inf
            n_samples, n_features = features.shape
            # V4.1: Symbol-based seed for stability across runs
            sym_seed = int.from_bytes(hashlib.blake2b(str(symbol).encode('utf-8'), digest_size=4).digest(), 'little') if symbol else 0
            for init_i in range(CFG.HMM_N_INITS):
                try:
                    m = GaussianHMM(n_components=n, covariance_type='full',
                                    n_iter=CFG.HMM_N_ITER, random_state=sym_seed + init_i, tol=1e-4)
                    m.fit(features)
                    ll = m.score(features)
                    n_params = (n*(n-1) + n*n_features + n*n_features*(n_features+1)//2 + (n-1))
                    bic = -2*ll + n_params * np.log(n_samples)
                    if bic < best_bic: best_bic, best_model = bic, m
                except: continue
            if best_model is None: return HMMEngine._fallback(df)
            m = best_model; states = m.predict(features)
            means = m.means_; ret_means = means[:, 0]
            order = np.argsort(ret_means)
            names = {order[0]: 'BEAR', order[1]: 'SIDEWAY', order[2]: 'BULL'}
            emj = {'BEAR': '🔴', 'SIDEWAY': '🟡', 'BULL': '🟢'}
            info = {}
            for i in range(n):
                nm = names.get(i, '?')
                state_mask = states == i
                state_ret = df['close'].pct_change().reindex(feat_idx).values[state_mask]
                state_ret = state_ret[~np.isnan(state_ret)]
                info[nm] = {
                    'mean_daily_pct': round(float(np.mean(state_ret))*100, 4) if len(state_ret)>0 else 0,
                    'ann_ret_pct': round(float(np.mean(state_ret))*252*100, 2) if len(state_ret)>0 else 0,
                    'ann_vol_pct': round(float(np.std(state_ret))*np.sqrt(252)*100, 2) if len(state_ret)>1 else 0,
                    'pct_time': round(float(np.mean(state_mask))*100, 1),
                    'avg_vol_regime': round(float(means[i, 1]), 2),
                    'avg_volume_activity': round(float(means[i, 2]), 2),
                }
            cs = states[-1]; cn = names.get(cs, '?')
            probs = m.predict_proba(features)[-1]
            np_d = {names.get(i,'?'): round(float(probs[i])*100,1) for i in range(n)}
            trans = {}
            for i in range(n):
                f = names.get(i,'?')
                trans[f] = {names.get(j,'?'): round(float(m.transmat_[i,j])*100,1) for j in range(n)}
            return {
                'current': f"{emj.get(cn,'')} {cn}", 'prob_pct': round(float(max(probs))*100,1),
                'state_probs': np_d, 'states': info, 'transitions': trans,
                'recent_20': [names.get(s,'?') for s in states[-20:]],
                '_state_dates': feat_idx.tolist(),
                '_state_labels': [names.get(s, 'SIDEWAY') for s in states],
                'warning': '⚠️ Regime co the doi' if max(probs)<0.6 else 'On dinh',
                'method': 'multivariate_hmm', 'n_features': features.shape[1],
                'n_inits': CFG.HMM_N_INITS, 'bic': round(best_bic, 2),
            }
        except Exception as e:
            log.warning(f"HMM fit error: {e}"); return HMMEngine._fallback(df)

    @staticmethod
    def _fallback(df):
        prices = df['close'] if isinstance(df, pd.DataFrame) else df
        ma20, ma50 = prices.rolling(20).mean(), prices.rolling(50).mean()
        p, m2, m5 = prices.iloc[-1], ma20.iloc[-1], ma50.iloc[-1]
        r20 = prices.iloc[-1]/prices.iloc[-20]-1 if len(prices)>=20 else 0
        st = '🟢 BULL' if p>m2>m5 and r20>0.02 else ('🔴 BEAR' if p<m2<m5 and r20<-0.02 else '🟡 SIDEWAY')
        return {'method': 'fallback', 'current': st, 'note': 'pip install hmmlearn cho HMM'}


class AlphaEngine:
    @staticmethod
    def extract(df, index_df=None):
        if len(df)<30: return {}
        c = df['close']; v = df['volume']
        sig = {}
        delta = c.diff(); gain = delta.where(delta>0,0).rolling(14).mean()
        loss = -delta.where(delta<0,0).rolling(14).mean()
        rsi = 100 - 100/(1+gain/loss.replace(0,1e-9)); crsi = rsi.iloc[-1]
        ema12, ema26 = c.ewm(span=12).mean(), c.ewm(span=26).mean()
        macd_h = (ema12-ema26) - (ema12-ema26).ewm(span=9).mean()
        roc10 = (c.iloc[-1]/c.iloc[-11]-1)*100 if len(c)>=11 else 0
        ms = 0
        if crsi>70: ms-=0.3
        elif crsi<30: ms+=0.3
        elif crsi>50: ms+=0.1
        else: ms-=0.1
        if roc10>5: ms+=0.3
        elif roc10<-5: ms-=0.3
        if macd_h.iloc[-1]>0: ms+=0.2
        else: ms-=0.2
        sig['momentum'] = {'rsi': round(float(crsi),1), 'roc_10d': round(roc10,2),
                          'macd_hist': round(float(macd_h.iloc[-1]),2), 'score': round(np.clip(ms,-1,1),3)}
        # V7: unconditional MA20 reversion was removed from alpha. Conditional
        # factor-residual reversion is computed later with Hurst/HMM routing.
        sig['mean_reversion'] = {'deprecated': True, 'score': 0.0,
                                 'note': 'replaced_by_conditional_factor_residual_reversion'}
        obv = (np.sign(c.diff())*v).cumsum()
        obv_up = obv.iloc[-1]>obv.iloc[-20] if len(obv)>=20 else True
        vr = v.rolling(5).mean().iloc[-1] / v.rolling(20).mean().iloc[-1] if v.rolling(20).mean().iloc[-1]>0 else 1
        vs = (0.3 if obv_up else -0.3) + (0.2 if vr>1.5 else (-0.2 if vr<0.5 else 0))
        sig['volume'] = {'obv_trend': 'UP' if obv_up else 'DOWN', 'vol_ratio_5_20': round(float(vr),2),
                        'score': round(np.clip(vs,-1,1),3)}
        if index_df is not None and 'close' in index_df.columns:
            # Deduplicate index BEFORE reindex to avoid ValueError
            index_df = index_df[~index_df.index.duplicated(keep='last')]
            c = c[~c.index.duplicated(keep='last')]
            ic = index_df['close'].reindex(c.index).ffill()
            if len(c)>=20 and len(ic)>=20:
                sr20 = c.pct_change(20).iloc[-1] - ic.pct_change(20).iloc[-1]
                sr, ir = c.pct_change().dropna(), ic.pct_change().reindex(c.pct_change().dropna().index).dropna()
                common = sr.index.intersection(ir.index)
                if len(common)>=20:
                    cov = np.cov(sr[common].values, ir[common].values)
                    beta = cov[0,1]/cov[1,1] if cov[1,1]>0 else 1
                    alpha_ann = (sr[common].mean()-beta*ir[common].mean())*252
                else: beta, alpha_ann = 1, 0
                sig['cross_sectional'] = {'rs_20d_pct': round(float(sr20)*100,2), 'beta': round(float(beta),3),
                                          'alpha_ann_pct': round(float(alpha_ann)*100,2)}
        # Legacy technical composite remains a ranking aid only; Agreement V2
        # uses the dedicated directional engines instead.
        cs = (0.6*sig.get('momentum',{}).get('score',0) +
              0.4*sig.get('volume',{}).get('score',0))
        sig['composite'] = {'alpha': round(cs,3),
                           'label': '🟢 BULLISH' if cs>0.3 else ('🔴 BEARISH' if cs<-0.3 else '⚪ NEUTRAL')}
        return sig


class StructureEngine:
    @staticmethod
    def sr_levels(df, n=3, lb=120):
        from scipy.signal import argrelextrema
        if len(df) < 30: return {'supports': [], 'resistances': []}
        tail = df.tail(lb)
        highs = tail['high'].values; lows = tail['low'].values
        close_now = tail['close'].iloc[-1]
        order = CFG.SR_EXTREMA_ORDER
        high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
        low_idx = argrelextrema(lows, np.less_equal, order=order)[0]
        atr = RiskEng.atr(df)
        cluster_dist = max(atr * 0.5, close_now * 0.005)

        def cluster_levels(prices, indices, n_levels):
            if len(indices) == 0: return []
            n_bars = len(prices)
            levels = [(prices[idx], 1.0 + (idx / n_bars)) for idx in indices]
            levels.sort(key=lambda x: x[0])
            clusters = []; current = [levels[0]]
            for i in range(1, len(levels)):
                if levels[i][0] - current[-1][0] <= cluster_dist:
                    current.append(levels[i])
                else:
                    clusters.append(current); current = [levels[i]]
            clusters.append(current)
            result = []
            for cl in clusters:
                avg_p = np.mean([c[0] for c in cl])
                touches = len(cl)
                avg_rec = np.mean([c[1] for c in cl])
                result.append({'price': round(float(avg_p),0), 'str': round(float(touches*avg_rec),2), 'touches': touches})
            result.sort(key=lambda x: x['str'], reverse=True)
            return result[:n_levels]

        all_res = cluster_levels(highs, high_idx, n*2)
        all_sup = cluster_levels(lows, low_idx, n*2)
        supports = sorted([s for s in all_sup if s['price']<close_now], key=lambda x:x['price'], reverse=True)[:n]
        resistances = sorted([r for r in all_res if r['price']>=close_now], key=lambda x:x['price'])[:n]
        return {'price': round(close_now,0), 'supports': supports, 'resistances': resistances, 'method': 'swing_extrema'}

    @staticmethod
    def trend(df, period=20):
        if len(df)<period+1: return {}
        p = df.tail(period+1)['close'].values
        d = abs(p[-1]-p[0]); vol = np.sum(np.abs(np.diff(p)))
        er = d/vol if vol>0 else 0; slope = (p[-1]-p[0])/p[0]
        x = np.arange(len(p)); co = np.polyfit(x,p,1); tl = np.polyval(co,x)
        ss_r = np.sum((p-tl)**2); ss_t = np.sum((p-np.mean(p))**2)
        r2 = 1-ss_r/ss_t if ss_t>0 else 0
        lb = ('📈 UPTREND' if er>0.6 and slope>0 else '📉 DOWNTREND' if er>0.6 and slope<0 else
              '↗️ UP NHE' if er>0.3 and slope>0 else '↘️ DOWN NHE' if er>0.3 else '↔️ SIDEWAY')
        return {'er': round(er,4), 'r2': round(r2,4), 'slope_pct': round(slope*100,2), 'label': lb}

    @staticmethod
    def flow(df, lb=20):
        r = df.tail(lb)
        if len(r)<5: return {}
        h,l,c,v = r['high'].values, r['low'].values, r['close'].values, r['volume'].values.astype(float)
        rng = np.where((h-l)==0, 1e-9, h-l)
        mf = ((c-l)-(h-c))/rng
        cmf = np.sum(mf*v)/np.sum(v) if np.sum(v)>0 else 0
        bp = np.mean((c-l)/rng)
        lb2 = ('🟢 MUA' if cmf>0.1 and bp>0.6 else '🔴 BAN' if cmf<-0.1 else '🟡 THIEN MANH' if cmf>0 else '🟡 THIEN YEU')
        return {'cmf': round(cmf,4), 'buy_p': round(bp,3), 'label': lb2}


class MomentumAlphaEngine:
    """One multi-horizon momentum vote; avoids counting correlated horizons twice."""
    WINDOWS = ((5, 0.20), (10, 0.35), (20, 0.30), (60, 0.15))

    @classmethod
    def analyze(cls, prices, horizon=None):
        horizon = int(horizon or CFG.FORECAST_HORIZON)
        p = pd.to_numeric(prices, errors='coerce').dropna().astype(float)
        if len(p) < 65:
            return {'signal': 'NEUTRAL', 'direction': 0, 'strength': 0.0,
                    'proj_pct': 0.0, 'active': False, 'reason': 'need_65_obs'}
        daily = p.pct_change().dropna()
        vol20 = float(daily.tail(20).std(ddof=1))
        components = {}
        raw_score = 0.0; projected = 0.0
        for window, weight in cls.WINDOWS:
            ret = float(p.iloc[-1] / p.iloc[-window-1] - 1)
            scale = max(vol20 * np.sqrt(window), 1e-6)
            normalized = float(np.tanh(ret / scale))
            raw_score += weight * normalized
            projected += weight * ((1.0 + ret) ** (horizon / window) - 1.0)
            components[f'ret_{window}d_pct'] = round(ret * 100, 3)

        tail = np.log(p.tail(21).values)
        x = np.arange(len(tail), dtype=float)
        coef = np.polyfit(x, tail, 1); fitted = np.polyval(coef, x)
        ss_tot = float(np.sum((tail - tail.mean()) ** 2))
        r2 = 1.0 - float(np.sum((tail - fitted) ** 2)) / ss_tot if ss_tot > 0 else 0.0
        change = abs(float(p.iloc[-1] - p.iloc[-21]))
        path = float(p.tail(21).diff().abs().sum())
        efficiency = change / path if path > 0 else 0.0
        trend_quality = np.sign(coef[0]) * np.sqrt(max(r2, 0.0)) * efficiency
        score = float(np.clip(0.80 * raw_score + 0.20 * trend_quality, -1.0, 1.0))
        direction = 1 if score > 0.15 else (-1 if score < -0.15 else 0)
        strength = float(np.clip((abs(score) - 0.10) / 0.70, 0.0, 1.0)) if direction else 0.0
        signal = 'BULLISH' if direction > 0 else ('BEARISH' if direction < 0 else 'NEUTRAL')
        return {
            'signal': signal, 'direction': direction, 'strength': round(strength, 4),
            'score': round(score, 4), 'proj_pct': round(float(np.clip(projected * 100, -20, 20)), 3),
            'active': bool(direction), 'trend_r2': round(r2, 4),
            'trend_efficiency': round(efficiency, 4), 'components': components,
        }


class HurstRegimeEngine:
    """Estimate persistence on returns; Hurst routes weights and never votes direction."""

    @staticmethod
    def _estimate(values):
        x = np.asarray(values, dtype=float)
        x = x[np.isfinite(x)]
        if len(x) < 48 or np.std(x) <= 1e-12:
            return None
        sizes = [n for n in (8, 16, 32, 64) if n <= len(x) // 2]
        points = []
        for size in sizes:
            rs_values = []
            for start in range(0, len(x) - size + 1, size):
                segment = x[start:start + size]
                centered = segment - segment.mean()
                spread = np.cumsum(centered)
                denom = segment.std(ddof=1)
                if denom > 1e-12:
                    rs_values.append((spread.max() - spread.min()) / denom)
            if rs_values and np.mean(rs_values) > 0:
                points.append((size, float(np.mean(rs_values))))
        if len(points) < 2:
            return None
        lx = np.log([p[0] for p in points]); ly = np.log([p[1] for p in points])
        slope, intercept = np.polyfit(lx, ly, 1)
        fitted = slope * lx + intercept
        denom = np.sum((ly - ly.mean()) ** 2)
        r2 = 1.0 - np.sum((ly - fitted) ** 2) / denom if denom > 0 else 0.0
        return float(np.clip(slope, 0.0, 1.0)), float(np.clip(r2, 0.0, 1.0))

    @classmethod
    def analyze(cls, prices):
        returns = pd.to_numeric(prices, errors='coerce').pct_change().dropna().values
        estimates = []
        for window in (64, 128, 252):
            if len(returns) >= min(window, 48):
                result = cls._estimate(returns[-window:])
                if result is not None:
                    estimates.append({'window': min(window, len(returns)), 'hurst': result[0], 'r2': result[1]})
        if not estimates:
            return {'hurst': 0.5, 'regime': 'UNCERTAIN', 'confidence': 0.0,
                    'method': 'multi_window_rs_returns', 'windows': []}
        h_values = np.array([x['hurst'] for x in estimates]); r2_values = np.array([x['r2'] for x in estimates])
        hurst = float(np.median(h_values)); stability = float(np.clip(1.0 - 4.0 * np.std(h_values), 0.0, 1.0))
        confidence = float(np.clip(stability * np.mean(r2_values), 0.0, 1.0))
        if confidence < 0.35:
            regime = 'UNCERTAIN'
        elif hurst >= CFG.HURST_TREND_THRESHOLD:
            regime = 'PERSISTENT'
        elif hurst <= CFG.HURST_REVERSION_THRESHOLD:
            regime = 'MEAN_REVERTING'
        else:
            regime = 'RANDOM_WALK'
        return {'hurst': round(hurst, 4), 'regime': regime, 'confidence': round(confidence, 4),
                'method': 'multi_window_rs_returns', 'windows': estimates}


class ConditionalResidualReversionEngine:
    """Factor-neutral reversal; active only in a range/reverting regime."""

    @staticmethod
    def analyze(df, idx_df=None, sector_returns=None, hurst=None, hmm=None, vni_regime='NEUTRAL'):
        neutral = {'signal': 'NEUTRAL', 'direction': 0, 'strength': 0.0, 'proj_pct': 0.0,
                   'active': False, 'method': 'factor_residual_reversion'}
        if df is None or len(df) < 80:
            return dict(neutral, reason='need_80_obs')
        stock = pd.to_numeric(df['close'], errors='coerce').pct_change().rename('stock')
        factors = []
        if idx_df is not None and 'close' in idx_df:
            market = pd.to_numeric(idx_df['close'], errors='coerce').pct_change().rename('market')
            factors.append(market)
        if sector_returns is not None:
            factors.append(pd.Series(sector_returns, copy=False).rename('sector'))
        if not factors:
            return dict(neutral, reason='missing_market_sector_factors')
        aligned = pd.concat([stock] + factors, axis=1).dropna().tail(180)
        if len(aligned) < 60:
            return dict(neutral, reason='insufficient_aligned_factors')
        y = aligned['stock'].values
        factor_cols = [c for c in aligned.columns if c != 'stock']
        x = np.column_stack([np.ones(len(aligned))] + [aligned[c].values for c in factor_cols])
        try:
            beta = np.linalg.lstsq(x, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return dict(neutral, reason='residual_regression_failed')
        residual = pd.Series(y - x @ beta, index=aligned.index)
        residual_5d = residual.rolling(5).sum().dropna()
        if len(residual_5d) < 30:
            return dict(neutral, reason='insufficient_residual_history')
        current = float(residual_5d.iloc[-1]); median = float(residual_5d.median())
        mad = float(np.median(np.abs(residual_5d.values - median)))
        robust_scale = max(1.4826 * mad, float(residual_5d.std(ddof=1)) * 0.5, 1e-6)
        z = float((current - median) / robust_scale)

        close = pd.to_numeric(df['close'], errors='coerce').dropna()
        tail = close.tail(21)
        path = float(tail.diff().abs().sum()); change = abs(float(tail.iloc[-1] - tail.iloc[0]))
        efficiency = change / path if path > 0 else 0.0
        hurst = hurst or {}; hmm = hmm or {}; state_probs = hmm.get('state_probs', {})
        sideway_prob = float(state_probs.get('SIDEWAY', 0) or 0) / 100.0
        regime_support = hurst.get('regime') == 'MEAN_REVERTING' or sideway_prob >= 0.55
        active = bool(abs(z) >= CFG.CONDITIONAL_MR_Z_MIN and regime_support and efficiency < 0.45
                      and vni_regime not in ('CRISIS', 'UNKNOWN'))
        direction = -int(np.sign(z)) if active else 0
        strength = float(np.clip((abs(z) - CFG.CONDITIONAL_MR_Z_MIN) / 1.75, 0.0, 1.0)) if active else 0.0
        projected = float(np.clip(-0.5 * current * 100, -10.0, 10.0)) if active else 0.0
        coefficients = {'intercept': round(float(beta[0]), 6)}
        coefficients.update({name: round(float(beta[i + 1]), 4) for i, name in enumerate(factor_cols)})
        return {
            'signal': 'UP' if direction > 0 else ('DOWN' if direction < 0 else 'NEUTRAL'),
            'direction': direction, 'strength': round(strength, 4), 'proj_pct': round(projected, 3),
            'active': active, 'residual_z': round(z, 4), 'residual_5d_pct': round(current * 100, 3),
            'trend_efficiency': round(efficiency, 4), 'sideway_probability': round(sideway_prob, 4),
            'hurst_regime': hurst.get('regime', 'UNCERTAIN'), 'coefficients': coefficients,
            'method': 'factor_residual_reversion',
            'reason': 'active' if active else 'regime_or_extreme_not_confirmed',
        }


class CrossSectionalLightGBMEngine:
    """Pooled point-in-time LightGBM models for 10-session absolute and relative returns."""
    FEATURE_COLUMNS = [
        'ret_1d', 'ret_5d', 'ret_10d', 'ret_20d', 'ret_60d', 'vol_10d', 'vol_20d',
        'downside_20d', 'atr_14_pct', 'range_pos_20d', 'volume_ratio_5_20',
        'value_ratio_5_20', 'cmf_20', 'market_ret_5d', 'market_ret_20d',
        'sector_ret_5d', 'sector_ret_20d', 'excess_market_20d', 'excess_sector_20d',
    ]

    @staticmethod
    def sector_return_series(data, sector_map):
        groups = {}
        for symbol, sector in sector_map.items():
            frame = data.get(symbol)
            if frame is None or 'close' not in frame or len(frame) < 30:
                continue
            groups.setdefault(sector, {})[symbol] = pd.to_numeric(frame['close'], errors='coerce').pct_change()
        return {sector: pd.DataFrame(series).mean(axis=1, skipna=True).sort_index()
                for sector, series in groups.items() if series}

    @classmethod
    def _feature_frame(cls, df, market_returns=None, sector_returns=None, horizon=None):
        horizon = int(horizon or CFG.FORECAST_HORIZON)
        c = pd.to_numeric(df['close'], errors='coerce').astype(float)
        h = pd.to_numeric(df['high'], errors='coerce').astype(float)
        l = pd.to_numeric(df['low'], errors='coerce').astype(float)
        v = pd.to_numeric(df['volume'], errors='coerce').astype(float)
        r = c.pct_change()
        out = pd.DataFrame(index=df.index)
        for window in (1, 5, 10, 20, 60):
            out[f'ret_{window}d'] = c.pct_change(window)
        out['vol_10d'] = r.rolling(10).std()
        out['vol_20d'] = r.rolling(20).std()
        out['downside_20d'] = r.clip(upper=0).pow(2).rolling(20).mean().pow(0.5)
        tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        out['atr_14_pct'] = tr.rolling(14).mean() / c.replace(0, np.nan)
        low20 = l.rolling(20).min(); high20 = h.rolling(20).max()
        out['range_pos_20d'] = (c - low20) / (high20 - low20).replace(0, np.nan)
        out['volume_ratio_5_20'] = v.rolling(5).mean() / v.rolling(20).mean().replace(0, np.nan)
        value = c * v
        out['value_ratio_5_20'] = value.rolling(5).mean() / value.rolling(20).mean().replace(0, np.nan)
        money_mult = ((c-l) - (h-c)) / (h-l).replace(0, np.nan)
        out['cmf_20'] = (money_mult*v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)

        market_returns = pd.Series(dtype=float) if market_returns is None else pd.Series(market_returns, copy=False)
        sector_returns = pd.Series(dtype=float) if sector_returns is None else pd.Series(sector_returns, copy=False)
        market_price = (1 + market_returns.reindex(out.index).fillna(0)).cumprod()
        sector_price = (1 + sector_returns.reindex(out.index).fillna(0)).cumprod()
        out['market_ret_5d'] = market_price.pct_change(5)
        out['market_ret_20d'] = market_price.pct_change(20)
        out['sector_ret_5d'] = sector_price.pct_change(5)
        out['sector_ret_20d'] = sector_price.pct_change(20)
        out['excess_market_20d'] = out['ret_20d'] - out['market_ret_20d']
        out['excess_sector_20d'] = out['ret_20d'] - out['sector_ret_20d']
        out['target_abs_pct'] = (c.shift(-horizon) / c - 1) * 100
        market_forward = (market_price.shift(-horizon) / market_price - 1) * 100
        sector_forward = (sector_price.shift(-horizon) / sector_price - 1) * 100
        out['target_alpha_pct'] = out['target_abs_pct'] - 0.5*market_forward - 0.5*sector_forward
        return out.replace([np.inf, -np.inf], np.nan)



class AgreementEngine:
    """Combine directional alpha only. Risk and regime models never cast direction votes."""

    @staticmethod
    def combine(distribution, momentum, lightgbm, conditional_mr, hurst, hmm, vni_regime='NEUTRAL'):
        distribution = dict(distribution or {})
        momentum = momentum or {}; lightgbm = lightgbm or {}; conditional_mr = conditional_mr or {}
        hurst = hurst or {}; hmm = hmm or {}
        components = {
            'momentum': momentum,
            'lightgbm_cross_sectional': lightgbm,
            'conditional_residual_reversion': conditional_mr,
        }
        weights = {
            'momentum': CFG.MOMENTUM_ALPHA_WEIGHT,
            'lightgbm_cross_sectional': CFG.LIGHTGBM_ALPHA_WEIGHT,
            'conditional_residual_reversion': CFG.CONDITIONAL_MR_WEIGHT,
        }
        if hurst.get('regime') == 'PERSISTENT':
            weights['momentum'] *= 1.25; weights['conditional_residual_reversion'] = 0.0
        elif hurst.get('regime') == 'MEAN_REVERTING':
            weights['momentum'] *= 0.75; weights['conditional_residual_reversion'] *= 1.50
        probs = hmm.get('state_probs', {})
        trend_prob = (float(probs.get('BULL', 0) or 0) + float(probs.get('BEAR', 0) or 0)) / 100.0
        side_prob = float(probs.get('SIDEWAY', 0) or 0) / 100.0
        weights['momentum'] *= 0.75 + 0.50*trend_prob
        weights['conditional_residual_reversion'] *= 0.50 + side_prob

        total_weight = sum(weights.values()) or 1.0
        active = {name: value for name, value in components.items()
                  if weights.get(name, 0) > 0
                  and int(value.get('direction', 0) or 0) != 0
                  and float(value.get('strength', 0) or 0) > 0}
        active_weight = sum(weights[name] for name in active)
        vote_score = sum(weights[name] * int(value['direction']) * float(value['strength'])
                         for name, value in active.items())
        forecast_numerator = sum(weights[name] * float(value.get('proj_pct', 0) or 0)
                                 for name, value in active.items())
        forecast_ret = forecast_numerator / active_weight if active_weight > 0 else 0.0
        vote_direction = 1 if vote_score > 0 else (-1 if vote_score < 0 else 0)
        ret_direction = 1 if forecast_ret > CFG.ALPHA_NEUTRAL_RET_PCT else (
            -1 if forecast_ret < -CFG.ALPHA_NEUTRAL_RET_PCT else 0)
        direction = vote_direction if vote_direction == ret_direction else 0
        matching_weight = sum(weights[name] for name, value in active.items()
                              if int(value.get('direction', 0)) == direction) if direction else 0.0
        agreement = matching_weight / active_weight * 100 if active_weight > 0 and direction else 0.0
        coverage = active_weight / total_weight * 100
        support = abs(vote_score) / total_weight * 100
        active_count = len(active)
        signal_strong = bool(
            direction > 0 and forecast_ret > 2.0
            and active_count >= CFG.AGREEMENT_MIN_ACTIVE_MODELS
            and coverage >= CFG.AGREEMENT_MIN_COVERAGE_PCT
            and agreement >= CFG.AGREEMENT_MIN_PCT
            and support >= CFG.AGREEMENT_MIN_SUPPORT_PCT
        )

        if vni_regime == 'CRISIS':
            forecast_ret = float(np.clip(forecast_ret, -CFG.FORECAST_CAP_CRISIS*3, CFG.FORECAST_CAP_CRISIS))
        elif vni_regime == 'BEAR':
            forecast_ret = float(np.clip(forecast_ret, -20, CFG.FORECAST_CAP_CRISIS*2))
        lock = distribution.get('lock_risk', {})
        lock_safe = float(lock.get('prob_loss_gt_3pct', 100) or 100) < 20
        if direction <= 0 or vni_regime in ('UNKNOWN', 'CRISIS'):
            timing_status = 'BLOCKED'
        elif vni_regime in ('BEAR', 'WEAK') or not signal_strong or not lock_safe:
            timing_status = 'WATCH'
        else:
            timing_status = 'READY'
        timing = {
            'READY': '🟢 SẴN SÀNG — directional alpha mạnh, coverage đủ và T-lock đạt',
            'WATCH': '🟡 THEO DÕI — directional alpha có tín hiệu nhưng regime/coverage/risk chưa đủ',
            'BLOCKED': '🔴 KHÔNG VÀO — directional alpha hoặc hard regime chưa ủng hộ',
        }[timing_status]
        consensus = '📈 TANG' if direction > 0 else ('📉 GIAM' if direction < 0 else '↔️ TRUNG LAP')
        last_price = float(distribution.get('last_price', 0) or 0)
        component_returns = {
            'momentum_ret_pct': float(momentum.get('proj_pct', 0) or 0),
            'lightgbm_ret_pct': float(lightgbm.get('proj_pct', 0) or 0),
            'conditional_mr_ret_pct': float(conditional_mr.get('proj_pct', 0) or 0),
            'mc_distribution_mean_ret_pct': float(distribution.get('mc_distribution_mean_ret_pct', 0) or 0),
        }
        distribution.update({
            'weights': {k: round(v/total_weight, 4) for k, v in weights.items()},
            'component_returns': component_returns,
            'directional_components': components, 'hurst': hurst,
            'ensemble_uncapped_ret_pct': round(forecast_numerator / active_weight, 4) if active_weight else 0.0,
            'ensemble_ret_pct': round(forecast_ret, 2),
            'ensemble_price': round(last_price*(1+forecast_ret/100), 0) if last_price > 0 else None,
            'consensus': consensus, 'ensemble_direction': 'UP' if direction > 0 else ('DOWN' if direction < 0 else 'NEUTRAL'),
            'agreement_pct': round(agreement, 1), 'coverage_pct': round(coverage, 1),
            'support_pct': round(support, 1), 'confidence': round(agreement, 1),
            'active_models': active_count, 'total_models': len(components),
            'active_model_names': list(active), 'calibrated_prob_up': None,
            'meta_trust_probability': None, 'meta_status': 'WARMUP',
            'timing': timing, 'timing_status': timing_status, 'signal_strong': signal_strong,
        })
        return distribution


class FcastEngine:
    @staticmethod
    def ensemble(prices, garch_result=None, symbol='', vni_regime='NEUTRAL'):
        if len(prices)<30: return {}
        lr = np.log(prices/prices.shift(1)).dropna().values
        if len(lr) < 20: return {}
        q_lo, q_hi = np.quantile(lr, [0.02, 0.98])
        lr_robust = np.clip(lr, q_lo, q_hi)
        mu = np.mean(lr_robust) * CFG.FORECAST_DRIFT_SHRINK
        hist_sigma = np.std(lr_robust, ddof=1); lp = prices.iloc[-1]

        # V4.1 FIX #1: Per-symbol deterministic RNG instead of global seed
        sym_seed = int.from_bytes(hashlib.blake2b(str(symbol).encode('utf-8'), digest_size=4).digest(), 'little') if symbol else 42
        rng = np.random.RandomState(sym_seed)

        H = int(CFG.FORECAST_HORIZON); LOCK = CFG.MIN_HOLD_SESSIONS; N = CFG.MONTE_CARLO_SIMS
        if H < LOCK or H < 1:
            raise ValueError('FORECAST_HORIZON must cover the settlement lock window')

        # V4.1 FIX #2: Extract GARCH forecast vols with sanity bounds
        garch_vols = None
        if garch_result is not None:
            gv = garch_result.get('garch', {}).get('forecast_vol', [])
            if gv and len(gv) >= H:
                garch_vols = np.array(gv[:H]) / 100.0
            elif gv:
                extended = list(gv) + [gv[-1]] * (H - len(gv))
                garch_vols = np.array(extended[:H]) / 100.0
            if garch_vols is not None:
                # Sanity: cap vol at 3x historical, floor at 0.3x historical
                vol_floor = hist_sigma * 0.3
                vol_cap = hist_sigma * 3.0
                garch_vols = np.clip(garch_vols, vol_floor, vol_cap)
                # V4.1: Crisis vol multiplier
                if vni_regime == 'CRISIS':
                    garch_vols = garch_vols * CFG.CRISIS_VOL_MULT

        # Monte Carlo — GARCH-aware; dùng Student-t innovations khi model có nu.
        nu = None
        try:
            nu = float(garch_result.get('garch',{}).get('params',{}).get('nu',{}).get('val'))
            if not np.isfinite(nu) or nu <= 2.1: nu = None
        except Exception:
            nu = None
        def draw_z(size):
            if nu is not None:
                return rng.standard_t(nu, size=size) / np.sqrt(nu/(nu-2))
            return rng.standard_normal(size)

        sims = np.zeros((N, H))
        recursive_garch = False
        sim_params = (garch_result or {}).get('garch', {}).get('simulation_params', {})
        try:
            omega = float(sim_params.get('omega_pct2')) / 10000.0
            alpha = float(sim_params.get('alpha'))
            beta = float(sim_params.get('beta'))
            recursive_garch = (
                garch_vols is not None and nu is not None
                and np.isfinite([omega, alpha, beta]).all()
                and omega >= 0 and alpha >= 0 and beta >= 0
                and alpha + beta < 1.0
            )
        except (TypeError, ValueError):
            recursive_garch = False

        if recursive_garch:
            crisis_mult = CFG.CRISIS_VOL_MULT if vni_regime == 'CRISIS' else 1.0
            omega *= crisis_mult ** 2
            sigma2 = np.full(N, garch_vols[0] ** 2)
            variance_floor = (hist_sigma * 0.3) ** 2
            variance_cap = (hist_sigma * 3.0 * crisis_mult) ** 2
            prev_price = np.full(N, lp, dtype=float)
            for step in range(H):
                sigma2 = np.clip(sigma2, variance_floor, variance_cap)
                innovation = np.sqrt(sigma2) * draw_z(N)
                next_price = prev_price * np.exp(mu + innovation)
                sims[:, step] = next_price
                sigma2 = omega + alpha * innovation ** 2 + beta * sigma2
                prev_price = next_price
        elif garch_vols is not None:
            # Compatibility fallback for cached/legacy results that do not
            # contain the parameters required for recursive simulation.
            for step in range(H):
                z = draw_z(N)
                if step == 0:
                    sims[:, step] = lp * np.exp(mu + garch_vols[step] * z)
                else:
                    sims[:, step] = sims[:, step-1] * np.exp(mu + garch_vols[step] * z)
        else:
            sigma_use = hist_sigma
            if vni_regime == 'CRISIS': sigma_use *= CFG.CRISIS_VOL_MULT
            for i in range(N):
                sims[i] = lp * np.exp(np.cumsum(mu + sigma_use * draw_z(H)))

        fin = sims[:,-1]
        mc = {'price': round(np.mean(fin),0), 'prob_up': round(np.mean(fin>lp)*100,1),
              'ci_lo': round(np.percentile(fin,2.5),0), 'ci_hi': round(np.percentile(fin,97.5),0),
              'vol_source': 'recursive GARCH-t' if recursive_garch else ('GARCH-t' if garch_vols is not None and nu is not None else ('GARCH-normal' if garch_vols is not None else 'historical')),
              'innovation_df': round(nu,2) if nu else None,
              'recursive_vol': recursive_garch}

        lock_prices = sims[:, :LOCK]; lock_mins = lock_prices.min(axis=1)
        lock_dd = (lock_mins - lp) / lp * 100
        lock_risk = {
            'max_dd_lock_pct': round(np.percentile(lock_dd, 5), 2),
            'avg_dd_lock_pct': round(np.mean(lock_dd[lock_dd<0]), 2) if np.any(lock_dd<0) else 0,
            'prob_loss_in_lock_pct': round(np.mean(lock_mins < lp)*100, 1),
            'prob_loss_gt_3pct': round(np.mean(lock_dd < -3)*100, 1),
            'lock_sessions': LOCK,
        }
        unlock_prices = sims[:, LOCK-1]
        unlock = {
            'price_at_unlock': round(np.mean(unlock_prices), 0),
            'prob_up_at_unlock': round(np.mean(unlock_prices > lp)*100, 1),
            'ci_lo_unlock': round(np.percentile(unlock_prices, 10), 0),
            'ci_hi_unlock': round(np.percentile(unlock_prices, 90), 0),
        }
        # ── MC path summary for Forecast chart export ──
        mc_median_path = np.median(sims, axis=0).tolist()
        mc_ci_upper = np.percentile(sims, 97.5, axis=0).tolist()
        mc_ci_lower = np.percentile(sims, 2.5, axis=0).tolist()

        return {
            'distribution_only': True, 'last_price': float(lp),
            'mc_distribution_mean_ret_pct': round(float((np.mean(fin)/lp-1)*100), 3),
            'horizon': H, 'mc': mc, 'lock_risk': lock_risk, 'unlock': unlock,
            'hold_plan_sessions': H,
            'hold_plan_label': f'{H} phien (~{round(H*7/5)} ngay)',
            'mc_path': {'median': mc_median_path, 'upper': mc_ci_upper, 'lower': mc_ci_lower},
            '_mc_paths': sims,
        }


class RiskEng:
    @staticmethod
    def atr(df, per=14):
        if len(df)<per+1: return 0
        h,l,c = df['high'].values, df['low'].values, df['close'].values
        tr = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
        a = pd.Series(tr).rolling(per).mean().iloc[-1]
        return a if not pd.isna(a) else 0

    @staticmethod
    def tick_round(price, direction='down', exchange='HOSE'):
        return VNMarketRules.round_price(price, direction=direction, exchange=exchange)

    # Grid search bounds cho ATR-multiplier cua SL va 2 chot loi TP1 (chot mot phan)/TP2 (chot phan con lai)
    # Quy uoc: SL = entry - k_sl*ATR ; TP1 = entry + k_tp1*ATR ; TP2 = entry + k_tp2*ATR (k_tp2 > k_tp1)
    SL_GRID  = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5)
    TP1_GRID = (0.8, 1.0, 1.5, 2.0, 2.5)
    TP2_GRID = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
    BACKTEST_LOOKBACK = 120     # so phien lich su dung de backtest expectancy
    MAX_HOLD_SESSIONS = 20      # so phien toi da nam giu khi backtest 1 trade (label horizon)
    TP1_EXIT_FRACTION = 0.5     # ty trong chot tai TP1; phan con lai chay tiep toi TP2/SL

    # --- Sanity floors/caps cho SL: chan cac truong hop SL vo ly (0, am, hoac qua sat entry) ---
    MIN_SL_PCT = 0.015   # SL toi thieu phai cach entry >= 1.5% (du xa hon phi giao dich + noise 1 tick)
    MAX_SL_PCT = 0.12    # SL toi da 12% (chan ATR qua lon o co phieu bien dong cuc manh lam SL phi thuc te)
    VAR_SL_MULT = 1.1    # SL (theo %) nen rong hon toi thieu 1.1x |VaR95| 1-ngay, tranh bi quet boi bien dong thuong nhat

    @staticmethod
    def _simulate_trade_2tp(highs, lows, closes, i, k_sl, k_tp1, k_tp2, atr_i, max_hold,
                             tp1_frac=None, move_to_be_after_tp1=True):
        """
        Mo phong 1 lenh mo tai phien i, chot mot phan (tp1_frac) tai TP1, phan con lai chay toi TP2/SL/timeout.
        Sau khi cham TP1, SL cua phan con lai duoc doi ve breakeven (move_to_be_after_tp1) -- ky thuat quan tri
        rui ro chuan giup bao toan loi nhuan da thuc hien va khong de mot lenh thang bien thanh lenh lo.
        Tra ve R-multiple thuc hien duoc (blended theo tp1_frac), dung intrabar high/low de xac dinh cham truoc.
        Quy uoc: neu trong cung phien ca high cham TP va low cham SL thi coi la SL cham truoc (gia dinh bao thu).
        """
        tp1_frac = RiskEng.TP1_EXIT_FRACTION if tp1_frac is None else tp1_frac
        entry = closes[i]
        risk = k_sl * atr_i
        if risk <= 0:
            return None
        sl_price = entry - risk
        tp1_price = entry + k_tp1 * atr_i
        tp2_price = entry + k_tp2 * atr_i
        n = len(closes)
        end = min(i + max_hold, n - 1)

        tp1_hit = False
        r1 = None
        active_sl = sl_price
        for j in range(i + 1, end + 1):
            hit_sl = lows[j] <= active_sl
            hit_tp1 = (not tp1_hit) and highs[j] >= tp1_price
            hit_tp2 = tp1_hit and highs[j] >= tp2_price

            if not tp1_hit:
                if hit_sl and hit_tp1:
                    return -1.0  # bao thu: SL cham truoc khi kip chot TP1 -> toan bo lenh lo
                if hit_sl:
                    return -1.0
                if hit_tp1:
                    tp1_hit = True
                    r1 = k_tp1 / k_sl
                    if move_to_be_after_tp1:
                        active_sl = entry  # doi SL ve breakeven cho phan con lai
                    continue
            else:
                if hit_sl and hit_tp2:
                    r_rem = (active_sl - entry) / risk  # bao thu: SL (breakeven hoac goc) cham truoc TP2
                    return tp1_frac * r1 + (1 - tp1_frac) * r_rem
                if hit_sl:
                    r_rem = (active_sl - entry) / risk
                    return tp1_frac * r1 + (1 - tp1_frac) * r_rem
                if hit_tp2:
                    r2 = k_tp2 / k_sl
                    return tp1_frac * r1 + (1 - tp1_frac) * r2

        # Het thoi gian nam giu ma chua cham het cac moc -> dong phan con lai theo gia dong cua cuoi ky
        exit_price = closes[end]
        r_timeout = (exit_price - entry) / risk
        if tp1_hit:
            return tp1_frac * r1 + (1 - tp1_frac) * r_timeout
        return r_timeout

    @staticmethod
    def _expectancy_grid_search(df, atr_series):
        """
        Grid search (k_sl, k_tp1, k_tp2) tren BACKTEST_LOOKBACK phien gan nhat, tinh expectancy (theo R, da
        blend chot 1 phan tai TP1 + phan con lai chay toi TP2) cho moi bo ba, chon bo co expectancy cao nhat
        (EV = WinRate*AvgWin - (1-WinRate)*AvgLoss, chuan hoa theo R de so sanh cong bang giua cac ma).
        Tie-break: uu tien n_trades nhieu hon (dang tin hon) roi toi RR2 thap hon (TP2 gan hon, it "ao").
        Tra ve dict ket qua tot nhat + bang top ket qua de tham khao.
        """
        highs = df['high'].values; lows = df['low'].values; closes = df['close'].values
        n = len(closes)
        lookback = min(RiskEng.BACKTEST_LOOKBACK, n - RiskEng.MAX_HOLD_SESSIONS - 1)
        if lookback < 20:
            return None
        start_idx = n - lookback - RiskEng.MAX_HOLD_SESSIONS
        start_idx = max(start_idx, CFG.ATR_PERIOD + 1)
        results = []
        for k_sl in RiskEng.SL_GRID:
            for k_tp1 in RiskEng.TP1_GRID:
                if k_tp1 <= k_sl * 0.4:
                    continue  # TP1 qua gan SL, R:R phan chot dau khong dang risk
                for k_tp2 in RiskEng.TP2_GRID:
                    if k_tp2 <= k_tp1 * 1.2:
                        continue  # TP2 phai ro rang xa hon TP1
                    outcomes = []
                    for i in range(start_idx, n - 1):
                        atr_i = atr_series[i]
                        if not atr_i or atr_i <= 0 or np.isnan(atr_i):
                            continue
                        r = RiskEng._simulate_trade_2tp(highs, lows, closes, i, k_sl, k_tp1, k_tp2,
                                                         atr_i, RiskEng.MAX_HOLD_SESSIONS)
                        if r is not None:
                            outcomes.append(r)
                    if len(outcomes) < 15:
                        continue
                    outcomes = np.array(outcomes)
                    wr = (outcomes > 0).mean()
                    avg_win = outcomes[outcomes > 0].mean() if (outcomes > 0).any() else 0
                    avg_loss = abs(outcomes[outcomes <= 0].mean()) if (outcomes <= 0).any() else 0
                    expectancy_r = wr * avg_win - (1 - wr) * avg_loss
                    rr2 = k_tp2 / k_sl
                    results.append({
                        'k_sl': k_sl, 'k_tp1': k_tp1, 'k_tp2': k_tp2,
                        'rr1': round(k_tp1 / k_sl, 2), 'rr2': round(rr2, 2),
                        'win_rate': round(wr * 100, 1), 'avg_win_r': round(avg_win, 2),
                        'avg_loss_r': round(avg_loss, 2), 'expectancy_r': round(expectancy_r, 3),
                        'n_trades': len(outcomes),
                    })
        if not results:
            return None
        # Chon theo expectancy cao nhat; neu hoa, uu tien mau lon hon roi RR2 thap hon (it "ao")
        results.sort(key=lambda x: (-x['expectancy_r'], -x['n_trades'], x['rr2']))
        return {'best': results[0], 'top5': results[:5]}

    @staticmethod
    def stops(df, rs=None, fc=None, exchange='HOSE', liquidity=None, costs=None):
        """
        rs: dict thong ke tu StatEngine.returns() (co VaR_95, CVaR_95) -- dung de sanity-check SL.
        fc: dict tu FcastEngine.ensemble() (co confidence, ensemble_ret_pct) -- dung de blend xac suat
            thanh cong va dieu chinh do "tham vong" cua TP theo muc do dong thuan cua mo hinh du bao.
        """
        rs = rs or {}; fc = fc or {}; liquidity = liquidity or {}; costs = costs or {}
        if len(df)<20: return {}
        e = df['close'].iloc[-1]; a = RiskEng.atr(df)
        if a <= 0: return {'entry': round(e,2), 'atr': 0, 'warning': 'ATR=0'}

        # ATR series theo tung phien (de backtest khong bi lookahead bias)
        atr_series = pd.Series(np.maximum(
            df['high'].values[1:] - df['low'].values[1:],
            np.maximum(abs(df['high'].values[1:] - df['close'].values[:-1]),
                       abs(df['low'].values[1:] - df['close'].values[:-1]))
        )).rolling(CFG.ATR_PERIOD).mean()
        atr_series = pd.concat([pd.Series([np.nan]), atr_series], ignore_index=True).values  # align voi df

        gs = RiskEng._expectancy_grid_search(df, atr_series) if CFG.ENABLE_INTERNAL_STOP_OPTIMIZATION else None

        if gs is None:
            # Khong du du lieu de backtest -> fallback ve mac dinh an toan (RR1 1:0.75, RR2 1:1.5)
            k_sl, k_tp1, k_tp2 = 2.0, 1.5, 3.0
            wr_backtest, n_trades = None, 0
            gs_note = 'Internal stop optimization OFF; dùng cấu hình ổn định SL=2.0xATR / TP1=1.5xATR / TP2=3.0xATR'
            gs_meta = {}
        else:
            b = gs['best']
            k_sl, k_tp1, k_tp2 = b['k_sl'], b['k_tp1'], b['k_tp2']
            wr_backtest, n_trades = b['win_rate'] / 100.0, b['n_trades']
            gs_note = (f"Grid search (lookback {min(RiskEng.BACKTEST_LOOKBACK, len(df))} phien, "
                       f"{n_trades} trades): SL={k_sl}xATR / TP1={k_tp1}xATR (R:R 1:{b['rr1']}) / "
                       f"TP2={k_tp2}xATR (R:R 1:{b['rr2']}), WR={b['win_rate']}%, "
                       f"expectancy={b['expectancy_r']}R/trade")
            gs_meta = gs

        # ---------------------------------------------------------------
        # 1) SANITY CHECK cho SL: chan cac gia tri vo ly (0/am/qua sat entry)
        #    - Bug goc: ATR nho (co phieu gia thap / it bien dong) + tick-round co the trieu tieu risk ve 0.
        #    - Fix: ap floor toi thieu theo % entry (MIN_SL_PCT), doi chieu voi VaR95 thuc te (SL phai >=
        #      VAR_SL_MULT x |VaR95|, neu khong se bi quet lien tuc boi bien dong ngay thuong), va cap tran
        #      (MAX_SL_PCT) de tranh ATR bung no lam SL phi thuc te.
        # ---------------------------------------------------------------
        sl_atr_pct = (k_sl * a) / e if e > 0 else 0
        var95_pct = abs(rs.get('VaR_95', 0)) / 100.0  # VaR_95 luu duoi dang % (vd -3.44 nghia la -3.44%)
        sl_floor_pct = max(RiskEng.MIN_SL_PCT, RiskEng.VAR_SL_MULT * var95_pct) if var95_pct > 0 else RiskEng.MIN_SL_PCT
        sl_target_pct = min(RiskEng.MAX_SL_PCT, max(sl_atr_pct, sl_floor_pct))
        sl_sanity_note = None
        if sl_atr_pct < sl_floor_pct:
            sl_sanity_note = (f"SL theo ATR ({sl_atr_pct*100:.2f}%) qua sat entry so voi VaR95 "
                               f"({var95_pct*100:.2f}%) -> da noi rong SL len {sl_target_pct*100:.2f}% de tranh "
                               f"bi quet boi bien dong ngay thuong.")
        elif sl_atr_pct > RiskEng.MAX_SL_PCT:
            sl_sanity_note = (f"SL theo ATR ({sl_atr_pct*100:.2f}%) qua rong (co phieu bien dong manh) -> "
                               f"da gioi han lai o muc tran {RiskEng.MAX_SL_PCT*100:.0f}%.")

        sl_swing = RiskEng.tick_round(e * (1 - sl_target_pct), 'down', exchange)
        risk = round(e - sl_swing, 4)
        if risk <= 0:
            # Phong ho cuoi cung: khong bao gio duoc phep tra ve SL >= entry
            sl_swing = RiskEng.tick_round(e * (1 - RiskEng.MIN_SL_PCT), 'down', exchange)
            risk = round(e - sl_swing, 4)
            sl_sanity_note = (sl_sanity_note or '') + ' [FORCE-FIX] SL tinh ra >= entry, da ep ve floor toi thieu.'

        # ---------------------------------------------------------------
        # 2) Quy doi RR toi uu (tu grid search) sang risk THUC TE (sau sanity-check + tick-round)
        # ---------------------------------------------------------------
        rr1 = k_tp1 / k_sl
        rr2 = k_tp2 / k_sl
        tp1_raw = e + rr1 * risk
        tp2_raw = e + rr2 * risk

        # ---------------------------------------------------------------
        # 3) Cap TP theo bien do thuc te: percentile 90 cua max-favorable-move trong MAX_HOLD_SESSIONS
        #    phien gan day, tranh truong hop TP vuot xa nhung gi co phieu tung dat duoc.
        # ---------------------------------------------------------------
        highs = df['high'].values; closes = df['close'].values
        mfe_pct = []
        for i in range(max(0, len(df) - RiskEng.BACKTEST_LOOKBACK), len(df) - 1):
            end = min(i + RiskEng.MAX_HOLD_SESSIONS, len(df) - 1)
            if end <= i: continue
            best_high = highs[i+1:end+1].max()
            mfe_pct.append((best_high - closes[i]) / closes[i] * 100)
        realistic_cap_pct = np.percentile(mfe_pct, 90) if len(mfe_pct) >= 10 else None
        tp_realistic_cap = e * (1 + realistic_cap_pct/100) if realistic_cap_pct else None

        # ---------------------------------------------------------------
        # 4) Dieu chinh TP theo do dong thuan cua mo hinh du bao (Confidence) + huong ky vong (EnsRet%):
        #    - Neu Confidence thap hoac EnsRet% <=0, mo hinh chua co edge ro rang -> keo TP2 ve gan TP1 hon
        #      (giam "tham vong", uu tien chot loi som) thay vi giu nguyen muc TP2 lac quan tu backtest.
        #    - Neu Confidence cao va EnsRet% ho tro huong TP2 goc thi giu nguyen.
        # ---------------------------------------------------------------
        meta_probability = fc.get('meta_trust_probability')
        conf_pct = (float(meta_probability) * 100 if meta_probability is not None
                    else float(fc.get('support_pct', 0) or 0))
        ens_ret_pct = fc.get('ensemble_ret_pct', 0)
        # Meta probability is preferred once trained. During warm-up, weighted
        # directional support is a conservative sizing proxy, not a win rate.
        conviction = max(0.0, min(1.0, (conf_pct - 25) / 75))
        if ens_ret_pct <= 0:
            conviction *= 0.5  # mo hinh du bao khong ung ho chieu long -> giam tham vong TP hon nua
        tp2_adj = tp1_raw + conviction * (tp2_raw - tp1_raw)  # shrink TP2 ve TP1 khi conviction thap

        tp1 = RiskEng.tick_round(tp1_raw, 'up', exchange)
        tp2_before_cap = RiskEng.tick_round(tp2_adj, 'up', exchange)
        tp2 = RiskEng.tick_round(min(tp2_before_cap, tp_realistic_cap), 'down', exchange) if tp_realistic_cap else tp2_before_cap
        tp_was_capped = tp_realistic_cap is not None and tp2_before_cap > tp_realistic_cap
        if tp_realistic_cap is not None:
            tp1 = min(tp1, RiskEng.tick_round(tp_realistic_cap, 'down', exchange))
        plan_valid = 0 < sl_swing < e < tp1 < tp2

        # ---------------------------------------------------------------
        # 5) Xac suat thanh cong "blended": ket hop WinRate tu backtest (empirical, phu thuoc mau) voi
        #    Confidence cua mo hinh du bao hien tai (phan anh trang thai tin hieu MOI NHAT, khong co trong
        #    backtest lich su). Trong so backtest tang theo can bac hai so luong trade (shrinkage chuan),
        #    tranh qua tin vao mau nho nhung cung khong bo qua tin hieu du bao hien tai.
        # ---------------------------------------------------------------
        # Agreement giữa mô hình KHÔNG phải xác suất thắng. Chỉ tính EV khi có xác suất đã calibration OOS.
        calibrated_p = fc.get('p_trade_win_calibrated')
        p_win_blended = float(calibrated_p) if calibrated_p is not None else None
        avg_r_win = RiskEng.TP1_EXIT_FRACTION * rr1 + (1 - RiskEng.TP1_EXIT_FRACTION) * (tp2 - e) / risk if risk > 0 else 0
        if p_win_blended is not None:
            ev_r = p_win_blended * avg_r_win - (1 - p_win_blended)
            ev_vnd_per_share = round(ev_r * risk, 2)
        else:
            ev_r = None; ev_vnd_per_share = None
        roundtrip_cost_vnd = e * float(costs.get('roundtrip_cost_pct', 0) or 0) / 100.0
        weighted_gain = RiskEng.TP1_EXIT_FRACTION * (tp1-e) + (1-RiskEng.TP1_EXIT_FRACTION) * (tp2-e)
        rr_net_cost = (weighted_gain - roundtrip_cost_vnd) / (risk + roundtrip_cost_vnd) if risk > 0 else 0
        plan_valid = plan_valid and weighted_gain > roundtrip_cost_vnd

        sl_wide  = RiskEng.tick_round(e - (k_sl+1)*a, 'down', exchange)
        sl_max   = RiskEng.tick_round(e - (k_sl+2)*a, 'down', exchange)
        swing_low_10 = df['low'].tail(10).min()
        swing_low_20 = df['low'].tail(20).min()
        trail_atr = round(2*a, 2)

        if len(df) >= 10:
            lock_hist_dd = []
            closes_arr = df['close'].values; lows = df['low'].values
            lock_sessions = CFG.MIN_HOLD_SESSIONS
            for i in range(lock_sessions, len(closes_arr)):
                entry_p = closes_arr[i-lock_sessions]
                worst_in_lock = lows[i-lock_sessions+1:i+1].min()
                lock_hist_dd.append((worst_in_lock - entry_p) / entry_p * 100)
            mae_lock = round(np.percentile(lock_hist_dd, 10), 2) if lock_hist_dd else 0
        else: mae_lock = 0

        # Historical low-based MAE uses the same post-entry session window as MC lock risk.
        sl_pct_display = -sl_target_pct * 100
        lock_risk_flag = mae_lock < sl_pct_display  # Lock MAE is deeper than the displayed stop buffer.

        return {
            'entry': round(e,2), 'atr': round(a,2), 'plan_valid': bool(plan_valid),
            'lock_sessions': CFG.MIN_HOLD_SESSIONS,
            'construction': {'atr_stop_pct': sl_atr_pct * 100, 'stop_floor_pct': sl_floor_pct * 100,
                             'base_tp2': tp2_raw, 'conviction_tp2': tp2_adj,
                             'optimization_enabled': bool(gs)},
            'sl_swing': sl_swing, 'sl_wide': sl_wide, 'sl_max': sl_max,
            'sl_pct': round(sl_pct_display, 2), 'sl_sanity_note': sl_sanity_note,
            'lock_risk_flag': lock_risk_flag,
            'swing_low_10': round(swing_low_10,2), 'swing_low_20': round(swing_low_20,2),
            # TP1 (chot mot phan, vd 50%) + TP2 (phan con lai) toi uu hoa qua grid search + expectancy,
            # da dieu chinh theo conviction cua mo hinh du bao va cap theo bien do thuc te (MFE p90)
            'tp1': tp1, 'tp2': tp2, 'tp1_exit_fraction': RiskEng.TP1_EXIT_FRACTION,
            'rr1': round(rr1, 2), 'rr2': round((tp2-e)/risk, 2) if risk > 0 else 0,
            # Giu key 'tp_optimal'/'rr_optimal' de tuong thich nguoc voi report/Excel hien co (= TP2)
            'tp_optimal': tp2, 'rr_optimal': round((tp2-e)/risk, 2) if risk > 0 else 0,
            'k_sl': k_sl, 'k_tp1': k_tp1, 'k_tp2': k_tp2, 'tp_was_capped': tp_was_capped,
            'tp_realistic_cap': round(tp_realistic_cap, 2) if tp_realistic_cap else None,
            'mfe_p90_pct': round(realistic_cap_pct, 2) if realistic_cap_pct else None,
            'conviction': round(conviction, 2),
            'p_win_backtest': round(wr_backtest*100, 1) if wr_backtest is not None else None,
            'model_agreement_pct': round(float(fc.get('agreement_pct', 0) or 0), 1),
            'forecast_conviction_pct': round(conf_pct, 1),
            'p_win_blended': round(p_win_blended*100, 1) if p_win_blended is not None else None,
            'n_trades_backtest': n_trades,
            'ev_r_per_trade': round(ev_r, 3) if ev_r is not None else None,
            'ev_vnd_per_share': ev_vnd_per_share, 'rr_net_cost': round(rr_net_cost,2),
            'grid_search_note': gs_note, 'grid_search_detail': gs_meta,
            # Giu lai cac moc 2R/3R/4R de tuong thich nguoc, tinh theo risk thuc te
            'tp_2r': RiskEng.tick_round(e + 2*risk, 'up', exchange), 'tp_3r': RiskEng.tick_round(e + 3*risk, 'up', exchange),
            'tp_4r': RiskEng.tick_round(e + 4*risk, 'up', exchange), 'risk_per_share': round(risk,2),
            'trail_atr': trail_atr, 'trail_note': f'Sau khi chứng khoán về T+2: trailing {trail_atr:,.0f} VND (2x ATR)',
            'mae_lock_10pct': mae_lock,
            'mae_lock_note': f'Lich su: 90% truong hop DD trong lock < {abs(mae_lock):.1f}%'
                              + (' -- CANH BAO: SL hien tai hep hon muc DD nay, co the "chet" trong settlement lock.' if lock_risk_flag else ''),
            'settlement': f'T+{CFG.SETTLEMENT_DAYS} — daily-bar approximation: sớm nhất bán chiều T+2', 'exchange': exchange,
        }
    @staticmethod
    def sizing(entry, stop, acct=None, hold_sessions=None, exchange='HOSE', liquidity=None):
        acct = float(acct or CFG.ACCOUNT_SIZE); liquidity = liquidity or {}
        rps = abs(float(entry) - float(stop))
        if rps <= 0 or entry <= 0: return {}
        risk_budget = acct * CFG.MAX_POSITION_RISK
        lot = VNMarketRules.profile(exchange)['lot_size']
        shares_risk = int((risk_budget / rps) // lot) * lot
        shares_alloc = int(((acct * CFG.MAX_POSITION_PCT) / entry) // lot) * lot
        capacity = max(0, int(liquidity.get('capacity_shares', shares_alloc) or 0))
        capacity = (capacity // lot) * lot
        shares = max(0, min(shares_risk, shares_alloc, capacity))
        limits = {'risk': shares_risk, 'allocation': shares_alloc, 'liquidity': capacity}
        binding = [name for name, limit in limits.items() if limit == min(limits.values())]
        value = shares * entry
        actual_max_loss = shares * rps
        pct_acct = value / acct * 100 if acct > 0 else 0
        hold = hold_sessions or CFG.SWING_DEFAULT
        capital_lock_days = round(hold * 7/5)
        return {
            'shares': shares, 'value': round(value,0), 'pct_acct': round(pct_acct,1),
            'risk_budget_vnd': round(risk_budget,0), 'max_loss': round(actual_max_loss,0),
            'risk_per_share_vnd': round(rps,0), 'shares_by_risk': shares_risk,
            'shares_by_allocation': shares_alloc, 'shares_by_liquidity': capacity,
            'binding_constraints': binding, 'account_size_vnd': acct,
            'risk_pct_nav': round(actual_max_loss / acct * 100, 4),
            'capital_lock_days': capital_lock_days,
            'capital_lock_note': f'Vốn {value:,.0f} VND dự kiến khóa ~{capital_lock_days} ngày',
        }

    @staticmethod
    def kelly(wr, aw, al):
        if al==0: return {}
        b = aw/al; p,q = wr, 1-wr; k = (p*b-q)/b
        return {'full_pct':round(max(0,k)*100,2), 'half_pct':round(max(0,k/2)*100,2),
                'edge_pct':round((p*b-q)*100,2), 'has_edge':bool(k>0)}


class AdaptiveScorer:
    WEIGHTS = {
        'sharpe': 0.08, 'forecast_ret': 0.12, 'forecast_conf': 0.05,
        'cmf': 0.08, 'trend_quality': 0.10, 'alpha_composite': 0.10,
        'hmm_regime': 0.07, 'lock_safety': 0.07, 'vol_regime': 0.05,
        'distribution_quality': 0.04, 'liquidity': 0.12, 'data_quality': 0.12,
    }

    @staticmethod
    def _robust_zscore(value, values_array):
        if len(values_array) < 3: return 0.0
        med = np.median(values_array)
        iqr = np.percentile(values_array, 75) - np.percentile(values_array, 25)
        if iqr < 1e-9: return 0.0
        return float(np.clip((value - med) / iqr, -1.0, 1.0))

    @staticmethod
    def extract_factors(report):
        rs = report.get('stats', {}); fc = report.get('fcast', {})
        fl = report.get('flow', {}); tr = report.get('trend', {})
        al = report.get('alpha', {}).get('composite', {}); hm = report.get('hmm', {})
        vl = report.get('vol', {}); di = report.get('dist', {}).get('verdict', {})
        lock = fc.get('lock_risk', {}); liq = report.get('liquidity', {}); dq = report.get('data_quality', {}); costs = report.get('costs', {})
        hmm_cur = hm.get('current', '')
        hmm_num = 1.0 if 'BULL' in hmm_cur else (-1.0 if 'BEAR' in hmm_cur else 0.0)
        hmm_score = hmm_num * (hm.get('prob_pct', 50) / 100.0)
        er = tr.get('er', 0); slope = tr.get('slope_pct', 0)
        trend_q = er * np.sign(slope) if slope != 0 else 0
        lock_safety = 100 - lock.get('prob_loss_gt_3pct', 50)
        vol_score = 1.0 - vl.get('vol_ratio', 1.0)
        dist_score = 1.0 if di.get('is_gaussian', True) else -0.5
        return {
            'sharpe': rs.get('sharpe', 0),
            'forecast_ret': costs.get('net_forecast_pct', fc.get('ensemble_ret_pct', 0)),
            'forecast_conf': ((float(fc.get('meta_trust_probability')) * 2 - 1)
                              if fc.get('meta_trust_probability') is not None
                              else (float(fc.get('support_pct', 0) or 0) - 50) / 50),
            'cmf': fl.get('cmf', 0), 'trend_quality': trend_q,
            'alpha_composite': al.get('alpha', 0), 'hmm_regime': hmm_score,
            'lock_safety': (lock_safety - 50) / 50,
            'vol_regime': vol_score, 'distribution_quality': dist_score,
            'liquidity': {'A':1.0,'B':0.5,'C':0.0,'D':-1.0}.get(liq.get('tier','D'),-1.0),
            'data_quality': (dq.get('score',50)-50)/50,
        }



    @staticmethod
    def _detect_vni_regime(idx_df):
        if idx_df is None or 'close' not in idx_df.columns or len(idx_df)<50: return 'UNKNOWN'
        try:
            c = pd.to_numeric(idx_df['close'], errors='coerce')
            if not np.isfinite(c).all() or (c <= 0).any(): return 'UNKNOWN'
            ma20 = c.rolling(20).mean().iloc[-1]; ma50 = c.rolling(50).mean().iloc[-1]
            last = c.iloc[-1]
            ret_20d = (last / c.iloc[-21] - 1) if len(c)>=21 else 0
            ret_5d = (last / c.iloc[-6] - 1) if len(c)>=6 else 0
            ret_1d = (last / c.iloc[-2] - 1) if len(c)>=2 else 0

            # V4.1 FIX #4: CRISIS = extreme short-term damage
            # Condition 1: 1-day crash AND 5-day decline
            if ret_1d < CFG.VNI_CRASH_1D_PCT and ret_5d < CFG.VNI_CRASH_5D_PCT:
                return 'CRISIS'
            # Condition 2: Severe 5-day crash alone
            if ret_5d < CFG.VNI_CRASH_5D_PCT * 1.5:  # -7.5% in 5 days
                return 'CRISIS'
            # Condition 3: Single-day capitulation (e.g. -3% flash crash)
            if ret_1d < CFG.VNI_CRASH_1D_PCT * 1.5:  # -3% in 1 day
                return 'CRISIS'
            # Existing logic
            if last > ma20 > ma50 and ret_20d > 0.02: return 'BULL'
            elif last < ma20 < ma50 and ret_20d < -0.02: return 'BEAR'
            elif last < ma20 and ret_5d < -0.01: return 'WEAK'
            else: return 'NEUTRAL'
        except: return 'UNKNOWN'


"""Preserved prototype commentary; provider/export/tracking methods excluded."""
import math
import numpy as np
from service.quant.configuration import CFG

BUY_ACTIONS = frozenset({'BUY_NOW'})

RATINGS = {'BUY_NOW': 'MUA NGAY', 'BUY_SETUP': 'CHỜ ĐIỂM MUA',
           'WATCH': 'THEO DÕI', 'AVOID': 'TRÁNH'}

def final_action(r):
    if 'error' in r:
        return 'AVOID'
    action = r.get('action', {}).get('action', 'AVOID')
    return action if action in RATINGS else 'AVOID'


def action_rating(r):
    return RATINGS[final_action(r)]


def num(value, digits=2, suffix=''):
    try:
        value = float(value)
        return f'{value:,.{digits}f}{suffix}' if math.isfinite(value) else 'N/A'
    except (TypeError, ValueError):
        return 'N/A'


def above(value, threshold):
    try:
        return float(value) > threshold
    except (TypeError, ValueError):
        return False


def below(value, threshold):
    try:
        return float(value) < threshold
    except (TypeError, ValueError):
        return False


def commentary(r, detailed=False):
    action = final_action(r)
    if 'error' in r:
        return f'Không đủ dữ liệu: {r["error"]}. Action = {action}.'
    stats = r.get('stats', {}); alpha = r.get('alpha', {})
    rel = alpha.get('cross_sectional', {}); momentum = alpha.get('momentum', {})
    volume = alpha.get('volume', {}); trend = r.get('trend', {}); flow = r.get('flow', {})
    fc = r.get('fcast', {}); costs = r.get('costs', {}); hm = r.get('hmm', {})
    vol = r.get('vol', {}); ga = r.get('garch', {}).get('garch', {})
    eg = r.get('garch', {}).get('egarch', {}); sl = r.get('sl', {}); pos = r.get('pos', {})
    rc = r.get('rec', {}); dist = r.get('dist', {}); lock = fc.get('lock_risk', {})
    agreement = fc.get('agreement_pct', fc.get('confidence'))
    mom_alpha = r.get('momentum_alpha', {}); lgbm_alpha = r.get('lightgbm_cross_sectional', {})
    conditional_mr = r.get('conditional_mr', {}); hurst = r.get('hurst', {})
    ret5 = mom_alpha.get('components', {}).get('ret_5d_pct')
    extended = above(momentum.get('rsi'), 75) or above(ret5, 8)
    short = (f'RS20 {num(rel.get("rs_20d_pct"))}%, CMF {num(flow.get("cmf"))}; HMM {hm.get("current", "N/A")}. '
             f'Net forecast {num(costs.get("net_forecast_pct"))}%, Agreement {num(agreement, 0)}%, '
             f'Coverage {num(fc.get("coverage_pct"), 0)}%, Support {num(fc.get("support_pct"), 0)}%; '
             f'entry {"extended" if extended else "cần đối chiếu hỗ trợ/kháng cự"}. Action = {action}.')
    if not detailed or action not in BUY_ACTIONS:
        return short

    # BUY_ONLY narrative: a readable thesis, while Summary keeps the short note above.
    symbol = r.get('symbol', '?')
    sr = r.get('sr', {})
    supports = ', '.join(num(x.get('price'), 0) for x in sr.get('supports', [])[:2]) or 'N/A'
    resistances = ', '.join(num(x.get('price'), 0) for x in sr.get('resistances', [])[:2]) or 'N/A'
    state = hm.get('current', 'N/A')
    prob_text = ', '.join(f'{k} {num(v, 1)}%' for k, v in hm.get('state_probs', {}).items()) or 'chưa có xác suất tin cậy'
    components = fc.get('component_returns', {}); weights = fc.get('weights', {})
    directional_returns = [components.get('momentum_ret_pct'), components.get('lightgbm_ret_pct'),
                           components.get('conditional_mr_ret_pct')]
    directional_returns = [v for v in directional_returns if isinstance(v, (int, float))]
    mixed = any(above(v, 0) for v in directional_returns) and any(below(v, 0) for v in directional_returns)
    forecast_read = ('directional alpha đang có xung đột nội bộ' if mixed
                     else 'các directional component hiện nghiêng cùng hướng')
    construction = sl.get('construction', {})
    constraints = {'risk': pos.get('shares_by_risk'), 'allocation': pos.get('shares_by_allocation'), 'liquidity': pos.get('shares_by_liquidity')}
    valid = {k: v for k, v in constraints.items() if isinstance(v, (int, float)) and v > 0}
    binding = ', '.join(k for k, v in valid.items() if v == min(valid.values())) if valid else 'chưa xác định'
    verdict = dist.get('verdict', {})
    tail_text = ('return distribution có fat-tail rõ, vì vậy VaR/CVaR lịch sử và GARCH-t đáng tin hơn giả định Gaussian' if verdict.get('is_gaussian') is False
                 else 'tail risk chưa cho thấy tín hiệu cực đoan, nhưng vẫn cần đọc cùng VaR/CVaR và gap risk')
    entry_text = 'đang extended, entry quality thấp' if extended else 'chưa extended rõ, entry quality ở mức trung bình'
    setup_quality = 'cao' if above(rc.get('score'), 74) else 'trung bình'
    risk_text = 'cao' if sl.get('lock_risk_flag') or above(lock.get('prob_loss_gt_3pct'), 20) else 'đáng theo dõi'
    blocks = [
        f'### {symbol} — Quality {setup_quality}, edge tồn tại nhưng timing cần được kiểm soát',
        f'{symbol} có historical return profile đáng chú ý: CAGR {num(stats.get("ann_return_pct"))}%, Sharpe {num(stats.get("sharpe"))}, Sortino {num(stats.get("sortino"))}, Calmar {num(stats.get("calmar"))} và MaxDD {num(stats.get("max_dd_pct"))}%. Chất lượng lựa chọn vì vậy không chỉ nằm ở mức tăng, mà ở hiệu quả so với volatility, downside và drawdown. RS20 {num(rel.get("rs_20d_pct"))}% so với VNINDEX, beta {num(rel.get("beta"))} và alpha proxy {num(rel.get("alpha_ann_pct"))}% cho thấy {"sức mạnh gần đây có phần độc lập với beta thị trường" if above(rel.get("rs_20d_pct"), 0) and below(rel.get("beta"), .8) else "relative edge chưa đủ tách khỏi biến động thị trường"}.',
        f'Bức tranh hiện tại được củng cố bởi volatility và money flow: Vol Ratio {num(vol.get("vol_ratio"))}, Vol Percentile {num(vol.get("vol_pctile"), 1)}%, CMF {num(flow.get("cmf"))} và OBV {volume.get("obv_trend", "N/A")}. {"Đây là confluence của volatility contraction, buying pressure và relative strength" if below(vol.get("vol_ratio"), .7) and above(flow.get("cmf"), 0) and above(rel.get("rs_20d_pct"), 0) else "Các tín hiệu chưa hoàn toàn đồng thuận, nên money flow cần được xác nhận thêm bằng price action"}.',
        f'Tuy nhiên trend quality mới là phần quyết định regime confirmation. ER {num(trend.get("er"))}, slope {num(trend.get("slope_pct"))}%/phiên và R² {num(trend.get("r2"))} cho thấy {"giá còn đi qua nhiều noise dù hướng chính tích cực" if below(trend.get("er"), .4) else "đường giá có cấu trúc tương đối liền mạch"}. HMM hiện ở {state}, với state probabilities {prob_text}; vì vậy stock selection quality có thể cao hơn regime confirmation. Đây là setup sớm hơn là breakout đã được xác nhận hoàn toàn.',
        f'Momentum multi-horizon có score {num(mom_alpha.get("score"), 3)}, lợi nhuận 5 phiên {num(ret5)}%; Hurst {num(hurst.get("hurst"), 3)} thuộc regime {hurst.get("regime", "N/A")}. Conditional residual reversion {"đang hoạt động" if conditional_mr.get("active") else "đang abstain"}, residual z-score {num(conditional_mr.get("residual_z"), 2)}. {"Giá đang extended nên entry quality thấp" if extended else f"Entry chưa quá extended nhưng vẫn nên đối chiếu hỗ trợ {supports}"}. Kháng cự gần nhất là {resistances}.',
        f'Directional Alpha {num(fc.get("ensemble_ret_pct"))}% trong {num(fc.get("horizon"), 0)} phiên đến từ Momentum {num(components.get("momentum_ret_pct"))}% (w {num(weights.get("momentum"), 2)}), LightGBM {num(components.get("lightgbm_ret_pct"))}% (w {num(weights.get("lightgbm_cross_sectional"), 2)}, rank {num(lgbm_alpha.get("rank_pct"), 1)}%) và Conditional Reversion {num(components.get("conditional_mr_ret_pct"))}% (w {num(weights.get("conditional_residual_reversion"), 2)}). {forecast_read}. GARCH–MC không bỏ phiếu hướng; nó ước lượng distribution mean {num(components.get("mc_distribution_mean_ret_pct"))}% và T-lock risk. Gross {num(costs.get("gross_forecast_pct", fc.get("ensemble_ret_pct")))}% trừ cost {num(costs.get("roundtrip_cost_pct"))}% còn net {num(costs.get("net_forecast_pct"))}%. Agreement {num(agreement, 0)}%, Coverage {num(fc.get("coverage_pct"), 0)}%, Support {num(fc.get("support_pct"), 0)}%; Meta-label {num(float(fc.get("meta_trust_probability"))*100 if fc.get("meta_trust_probability") is not None else None, 1)}%.',
        f'Risk Engine phản ứng với mức conviction đó bằng trade construction có kiểm soát. Entry {num(sl.get("entry"), 0)}, ATR {num(sl.get("atr"))}, SL {num(sl.get("sl_swing"), 0)} sau khi so ATR stop với VaR-adjusted floor {num(construction.get("stop_floor_pct"))}%. TP1 {num(sl.get("tp1"), 0)}; base TP2 {num(construction.get("base_tp2"), 0)} được kéo về {num(construction.get("conviction_tp2"), 0)} rồi chốt final TP2 {num(sl.get("tp2"), 0)} vì conviction {num(sl.get("conviction"))}. Đây là risk-adjusted/conviction-adjusted target, không gọi là optimized khi internal optimization đang tắt. R:R TP1 1:{num(sl.get("rr1"))}, TP2 1:{num(sl.get("rr2"))}; expected holding {fc.get("hold_plan_label", "N/A")}.',
        f'Tail và settlement risk hiện ở mức {risk_text}. {tail_text}. VaR95 {num(stats.get("VaR_95"))}% và CVaR95 {num(stats.get("CVaR_95"))}%; GARCH persistence {num(ga.get("persistence"), 3)}, alpha {num(ga.get("alpha"), 3)} và EGARCH gamma {num(eg.get("gamma"), 3)} cho thấy {"negative shock có thể làm volatility tăng mạnh và kéo dài" if below(eg.get("gamma"), -.05) or above(ga.get("persistence"), .95) else "shock risk cần tiếp tục theo dõi"}. Trong settlement lock {num(lock.get("lock_sessions", sl.get("lock_sessions")), 0)} phiên, MC lock drawdown {num(lock.get("max_dd_lock_pct"))}% và historical MAE {num(sl.get("mae_lock_10pct"))}%; {"stop có thể không phải guaranteed loss boundary trước khi vị thế được xử lý" if sl.get("lock_risk_flag") else "lock risk chưa cho thấy cảnh báo vượt stop rõ ràng"}.',
        f'Position sizing cho phép {num(constraints.get("risk"), 0)} cp theo risk, {num(constraints.get("allocation"), 0)} cp theo allocation và {num(constraints.get("liquidity"), 0)} cp theo liquidity; vị thế cuối {num(pos.get("shares"), 0)} cp, giá trị {num(pos.get("value"), 0)} VND ({num(pos.get("pct_acct"), 1)}% NAV), theoretical loss tại SL {num(pos.get("max_loss"), 0)} VND ({num(pos.get("risk_pct_nav"))}% NAV). Binding constraint là {binding}. Score {num(rc.get("score"), 0)}/100 là composite ranking/quality score, không phải xác suất profit.',
        f'### Overall\n{symbol} — {num(rc.get("score"), 0)}/100 | selection edge {"mạnh" if above(rc.get("score"), 74) else "trung bình"} | entry quality {entry_text}. Action Gate = {action}. Luận điểm mua đến từ historical edge, money flow và forecast net dương; điểm cần kỷ luật là {"HMM chưa xác nhận BULL, Agreement thấp và RSI extension" if below(agreement, 66) else "giữ nguyên kỷ luật quanh entry và settlement risk"}. Đây là một swing setup cần chờ pullback/xác nhận khi timing chưa tương xứng, không phải kết luận buy-and-hold dài hạn.'
    ]
    return '\n\n'.join(blocks)


class PrototypeCommentary:
    def generate_commentary(self, r, detailed=False):
        """Keep the legacy report narrative in Summary; use the new thesis only in BUY_ONLY."""
        if detailed:
            return commentary(r, detailed=True)
        if 'error' in r:
            return 'Không đủ dữ liệu để phân tích.'
        rs = r.get('stats', {}); vl = r.get('vol', {}); hm = r.get('hmm', {})
        fc = r.get('fcast', {}); ga = r.get('garch', {}).get('garch', {})
        eg = r.get('garch', {}).get('egarch', {}); sl = r.get('sl', {})
        di = r.get('dist', {}).get('verdict', {}); ft = r.get('dist', {}).get('fat_tail', {})
        ky = r.get('kelly', {}); rc = r.get('rec', {}); fl = r.get('flow', {})
        tr = r.get('trend', {}); al = r.get('alpha', {}); lock = fc.get('lock_risk', {})
        sym = r.get('symbol', '?')
        return '\n'.join([
            f'{sym} — Điểm {rc.get("score", 0)} ({rc.get("rating", "")})',
            self._section_health(rs, ky),
            self._section_technical(vl, tr, fl, al),
            self._section_models(hm, fc, ky, r.get('screener_signal', '') or ''),
            self._section_risk(ga, eg, di, ft, rs, lock, sl),
            self._section_conclusion(sym, rc, fc, sl, r.get('pos', {}), lock),
        ])


    def _section_health(self, rs, ky):
        """① Hiệu suất giá và rủi ro lịch sử; không phải sức khỏe tài chính doanh nghiệp."""
        ar = rs.get('ann_return_pct', 0)
        sh = rs.get('sharpe', 0)
        so = rs.get('sortino', 0)
        cal = rs.get('calmar', 0)
        mdd = rs.get('max_dd_pct', 0)
        wr = rs.get('win_rate_pct', 0)
        pf = rs.get('profit_factor', 0)

        lines = []

        # Determine thesis
        if sh >= 1.5:
            thesis = 'Hiệu suất giá–rủi ro xuất sắc'
        elif sh >= 0.5:
            thesis = 'Hiệu suất giá–rủi ro tích cực'
        elif sh >= 0:
            thesis = 'Hiệu suất giá–rủi ro trung bình'
        else:
            thesis = 'Hiệu suất giá–rủi ro yếu'

        lines.append(f'① {thesis}')

        # Build narrative
        narr = f'AnnRet {ar:.1f}%'
        if sh >= 0.5:
            narr += f' với Sharpe {sh:.2f} — mỗi đơn vị rủi ro chấp nhận, cổ phiếu trả lại {sh:.1f} đơn vị lợi nhuận'
            if so > sh * 1.2:
                narr += f'. Sortino {so:.2f} cao hơn Sharpe đáng kể — phần lớn biến động là biến động tăng, không phải giảm'
        elif sh >= 0:
            narr += f', Sharpe {sh:.2f} — lợi nhuận có bù được rủi ro nhưng chưa ấn tượng'
        else:
            narr += f', Sharpe {sh:.2f} — lợi nhuận KHÔNG bù được rủi ro đã chấp nhận. '
            narr += f'Nói thẳng: gửi tiết kiệm có thể tốt hơn nếu chỉ nhìn lịch sử'

        # Calmar — relate to MaxDD
        if cal > 3:
            narr += f'. Calmar {cal:.1f} — lợi nhuận năm gấp {cal:.1f}x mức thua xấu nhất (MaxDD {mdd:.1f}%)'
        elif mdd < -15:
            narr += f'. MaxDD {mdd:.1f}% — từng mất hơn 15% từ đỉnh, đây là mức drawdown nặng'
        elif mdd < -10:
            narr += f'. MaxDD {mdd:.1f}% — cần chuẩn bị tâm lý cho drawdown 2 chữ số'

        # Win rate + profit factor context
        if wr > 0 and pf > 0:
            if pf > 1.5 and wr > 55:
                narr += f'. Tỷ lệ ngày tăng {wr:.0f}%, Daily PF {pf:.2f} — thắng nhiều hơn thua, và khi thắng thì lớn hơn khi thua'
            elif pf < 1:
                narr += f'. Daily PF {pf:.2f} (<1) — khi thua, mức lỗ trung bình lớn hơn mức lãi. Đây là dấu hiệu cần cải thiện'

        lines.append(narr + '.')
        return '\n'.join(lines)


    def _section_technical(self, vl, tr, fl, al):
        """② Thesis: current technical state — vol, trend, flow, RS."""
        lines = []

        # Determine thesis from vol regime
        vol_rg = vl.get('regime', '')
        vol_ratio = vl.get('vol_ratio', 1)
        vol_pct = vl.get('vol_pctile', 50)

        if vol_rg == 'CONTRACTION':
            thesis = 'Đang "nén lò xo" — biến động thấp bất thường'
            vol_detail = (f'Vol Ratio {vol_ratio:.3f} tại Percentile {vol_pct:.1f}% — '
                          f'biến động 10 ngày chỉ bằng {vol_ratio:.0%} so với 60 ngày, '
                          f'thấp hơn {100-vol_pct:.0f}% lịch sử. '
                          f'Im lặng kéo dài thường đi trước bùng nổ — đây là giai đoạn tích lũy')
        elif vol_rg == 'EXPANSION':
            thesis = 'Biến động đang mở rộng — thị trường "nóng"'
            vol_detail = (f'Vol Ratio {vol_ratio:.3f} — biến động ngắn hạn cao hơn {vol_ratio:.0%} so với trung hạn. '
                          f'Cần cẩn trọng: biến động cao = cả cơ hội lẫn rủi ro lớn hơn')
        else:
            thesis = 'Biến động bình thường'
            vol_detail = f'Vol Ratio {vol_ratio:.3f} — không có tín hiệu bất thường về biến động'

        lines.append(f'② {thesis}')
        narr = vol_detail

        # Trend quality
        tr_label = tr.get('label', '')
        er = tr.get('er', 0)
        slope = tr.get('slope_pct', 0)
        if er > 0.5:
            narr += f'. Xu hướng rõ ràng (ER {er:.2f}, slope {slope:+.1f}%) — giá di chuyển có hướng, không lắc lư ngẫu nhiên'
        elif er > 0.3:
            narr += f'. Xu hướng nhẹ (ER {er:.2f}) — có hướng nhưng nhiễu khá nhiều'
        else:
            narr += f'. Chưa có xu hướng rõ (ER {er:.2f}) — giá lắc lư không phương hướng'

        # Money flow
        cmf = fl.get('cmf', 0)
        if cmf > 0.1:
            narr += f'. Dòng tiền tích cực (CMF {cmf:+.3f}) — tiền thông minh đang tích lũy'
        elif cmf < -0.1:
            narr += f'. Dòng tiền tiêu cực (CMF {cmf:+.3f}) — có dấu hiệu phân phối, tiền đang rút ra'
        elif abs(cmf) <= 0.1 and cmf != 0:
            narr += f'. Dòng tiền trung tính (CMF {cmf:+.3f})'

        # Relative Strength vs VNINDEX
        cs = al.get('cross_sectional', {})
        if cs:
            rs20 = cs.get('rs_20d_pct', 0)
            beta = cs.get('beta', 1)
            alpha_ann = cs.get('alpha_ann_pct', 0)
            if rs20 > 3:
                narr += f'. RS 20d +{rs20:.1f}% vs VNINDEX — vượt trội thị trường rõ rệt, dòng tiền ưu tiên mã này'
            elif rs20 > 0:
                narr += f'. RS 20d +{rs20:.1f}% vs VNINDEX — mạnh hơn thị trường nhẹ'
            elif rs20 < -3:
                narr += f'. RS 20d {rs20:.1f}% — yếu hơn VNINDEX đáng kể, dòng tiền đang né tránh'
            if abs(alpha_ann) > 5:
                narr += f' (alpha {alpha_ann:+.1f}%/năm, beta {beta:.2f})'

        # Momentum/RSI context
        mom = al.get('momentum', {})
        rsi = mom.get('rsi', 50)
        if rsi > 70:
            narr += f'. RSI {rsi:.0f} — vùng quá mua, cẩn trọng đuổi giá'
        elif rsi < 30:
            narr += f'. RSI {rsi:.0f} — vùng quá bán, có thể hồi kỹ thuật'

        lines.append(narr + '.')
        return '\n'.join(lines)


    def _section_models(self, hm, fc, ky, sig=''):
        """③ Thesis: statistical models agree/disagree — HMM, forecast, Kelly."""
        lines = []

        hmm_cur = hm.get('current', '')
        hmm_prob = hm.get('prob_pct', 0)
        hmm_warning = hm.get('warning', '')
        ens_ret = fc.get('ensemble_ret_pct', 0)
        conf = fc.get('agreement_pct', 0)
        coverage = fc.get('coverage_pct', 0)
        support = fc.get('support_pct', 0)
        meta_prob = fc.get('meta_trust_probability')
        mc_src = fc.get('mc', {}).get('vol_source', '')
        prob_up = fc.get('mc', {}).get('prob_up', 50)

        # HMM is context, not a direction vote. The thesis comes from the
        # directional-alpha bundle and the optional meta-label gate.
        if fc.get('timing_status') == 'READY':
            thesis = 'Mô hình thống kê đồng thuận TĂNG'
        elif fc.get('ensemble_direction') == 'UP':
            thesis = 'Tín hiệu đang hình thành nhưng chưa chín muồi'
        elif fc.get('active_models', 0):
            thesis = 'Tín hiệu yếu — mô hình chưa đồng thuận'
        else:
            thesis = 'Không có tín hiệu rõ từ mô hình'

        lines.append(f'③ {thesis}')

        # HMM narrative
        narr = ''
        hmm_n = hm.get('n_features', 1)
        if 'BULL' in hmm_cur:
            narr = f'HMM {hmm_cur} xác suất {hmm_prob:.0f}%'
            if hmm_prob >= 80:
                narr += ' — xác suất áp đảo, regime ổn định'
            elif hmm_prob >= 60:
                narr += ' — nghiêng tăng nhưng chưa áp đảo'
            else:
                narr += ' — chưa chắc chắn, regime có thể đổi'
            if hmm_n > 1:
                narr += f' (phân tích {hmm_n} chiều: giá, vol, volume, momentum)'
        elif 'BEAR' in hmm_cur:
            narr = f'HMM {hmm_cur} xác suất {hmm_prob:.0f}% — cổ phiếu đang trong trạng thái giảm'
        else:
            narr = f'HMM {hmm_cur} — thị trường đi ngang, chưa rõ hướng'

        # Forecast
        narr += f'. Directional Alpha {ens_ret:+.2f}% trong {fc.get("horizon",10)} phiên'
        if conf >= 66:
            narr += f', Agreement {conf:.0f}% — các mô hình khá đồng thuận'
        elif conf >= 50:
            narr += f', Agreement {conf:.0f}% — tín hiệu đang hình thành, chưa chín muồi'
        else:
            narr += f', Agreement {conf:.0f}% — mô hình chưa rõ hướng'
        narr += f', Coverage {coverage:.0f}% và Support {support:.0f}%'
        if meta_prob is not None:
            narr += f', Meta-label P(win) {float(meta_prob)*100:.1f}%'

        if mc_src:
            narr += f' (MC dùng {mc_src} vol)'

        # Kelly: chỉ hiển thị nếu được cấp thống kê trade calibration từ hệ thống ngoài.
        if ky.get('disabled'):
            pass
        elif ky.get('has_edge'):
            k_full = ky.get('full_pct', 0)
            k_half = ky.get('half_pct', 0)
            edge = ky.get('edge_pct', 0)
            narr += (f'. Kelly tham khảo từ thống kê ngày (KHÔNG phải edge trade): Half Kelly {k_half:.1f}%, '
                     f'edge {edge:.1f}% — kỳ vọng dương trên mỗi giao dịch')
        elif ky:
            narr += '. Kelly ngày âm — không dùng để sizing chiến lược, xác suất thắng chưa đủ bù thua'

        # Screener confluence
        if sig and sig != 'nan':
            if 'MẠNH' in sig:
                narr += f'. Screener cũng xác nhận tín hiệu MẠNH (đa tiêu chí kỹ thuật) — tăng niềm tin'
            elif 'TÍCH LŨY' in sig:
                narr += f'. Screener: đang TÍCH LŨY — phù hợp với giai đoạn chờ breakout'
            elif 'PHÂN PHỐI' in sig:
                narr += f'. ⚠️ Screener cảnh báo PHÂN PHỐI — mâu thuẫn với tín hiệu tăng, cần thận trọng'

        lines.append(narr + '.')
        return '\n'.join(lines)


    def _section_risk(self, ga, eg, di, ft, rs, lock, sl):
        """④ Honest risk warnings with explanation of WHY each metric matters."""
        lines = []
        items = []

        # GARCH
        persist = ga.get('persistence', 0)
        alpha_g = ga.get('alpha', 0)
        shock = ga.get('shock_sensitivity', '')
        if persist > 0.9:
            p_label = 'IGARCH-like' if persist > 0.97 else 'cao'
            item = f'GARCH persistence {persist:.3f} ({p_label})'
            if alpha_g > 0.15:
                item += f', Alpha {alpha_g:.4f} — nếu có tin xấu bất ngờ, biến động cao sẽ kéo dài rất lâu'
            else:
                item += ' — cú sốc biến động tắt chậm'
            hl = ga.get('half_life')
            if hl and hl > 0:
                item += f' (half-life {hl:.0f} phiên)'
            items.append(item)

        # EGARCH leverage
        if eg and not eg.get('error'):
            gamma = eg.get('gamma', 0)
            if gamma < -0.05:
                items.append(f'EGARCH gamma {gamma:.3f} — tin xấu tăng biến động MẠNH hơn tin tốt (leverage effect)')

        # Distribution
        if not di.get('is_gaussian', True):
            reject_n = di.get('reject_count', 0)
            kurt = ft.get('excess_kurtosis', 0)
            sev = ft.get('severity', '')
            item = f'Phân phối NON-GAUSSIAN ({reject_n}/5 test bác bỏ)'
            if kurt > 5:
                item += f', kurtosis {kurt:.1f} ({sev}) — đuôi rất dày, "thiên nga đen" xảy ra thường hơn mô hình dự đoán'
            elif kurt > 1:
                item += f', kurtosis {kurt:.1f} — cần dùng VaR non-parametric thay vì Gaussian'
            items.append(item)

        # VaR / CVaR context
        var95 = rs.get('VaR_95', 0)
        cvar95 = rs.get('CVaR_95', 0)
        if var95 and cvar95:
            items.append(f'VaR 95%: {var95:.2f}% (95/100 ngày lỗ không quá mức này), CVaR: {cvar95:.2f}% (trung bình 5 ngày tệ nhất)')

        # Lock risk
        prob_l3 = lock.get('prob_loss_gt_3pct', 0)
        prob_lock = lock.get('prob_loss_in_lock_pct', 0)
        mae = sl.get('mae_lock_10pct', 0)
        if prob_l3 > 15 or prob_lock > 50:
            item = f'T+2 settlement lock: {prob_lock:.0f}% khả năng lỗ khi chưa bán được'
            if prob_l3 > 20:
                item += f', {prob_l3:.0f}% khả năng lỗ >3%'
            if mae:
                item += f'. Lịch sử: 90% trường hợp DD trong lock < {abs(mae):.1f}%'
            items.append(item)

        # Build section
        if items:
            lines.append('④ Rủi ro cần lưu ý')
            for item in items:
                lines.append(item + '.')
        else:
            lines.append('④ Rủi ro: Không có cảnh báo đặc biệt.')

        return '\n'.join(lines)


    def _section_conclusion(self, sym, rc, fc, sl, pos, lock):
        """Conclusion: verdict + concrete levels + sizing."""
        score = rc.get('score', 0)
        rating = rc.get('rating', '')
        ens_ret = fc.get('ensemble_ret_pct', 0)
        hold_plan = fc.get('hold_plan_label', '')

        entry = sl.get('entry', 0)
        sl_swing = sl.get('sl_swing', 0)
        tp_opt = sl.get('tp_optimal', sl.get('tp_2r', 0))
        rr_opt = sl.get('rr_optimal', 0)
        shares = pos.get('shares', 0)
        value = pos.get('value', 0)

        lines = []

        # Verdict paragraph
        risk_per = entry - sl_swing if entry and sl_swing else 0
        risk_pct = risk_per / entry * 100 if entry > 0 else 0
        rr_actual = (tp_opt - entry) / risk_per if risk_per > 0 and tp_opt else 0

        if score >= 80:
            verdict = (f'Tổng kết {sym}: Đủ điều kiện SWING BUY — tín hiệu mạnh, rủi ro kiểm soát được')
        elif score >= 65:
            verdict = (f'Tổng kết {sym}: Có thể MUA với quản lý chặt — tín hiệu tích cực nhưng cần kỷ luật')
        elif score >= 50:
            verdict = (f'Tổng kết {sym}: THEO DÕI — tín hiệu đang hình thành nhưng chưa đủ mạnh để vào lệnh')
        elif score >= 35:
            verdict = (f'Tổng kết {sym}: TÍN HIỆU YẾU — không nên mở vị thế mới')
        else:
            verdict = (f'Tổng kết {sym}: TRÁNH — rủi ro cao hơn cơ hội')

        lines.append(verdict + '.')

        # Strategy line
        if entry and sl_swing and tp_opt and score >= 50:
            strat = f'Chiến lược: '
            if score >= 65:
                strat += f'Entry {entry:,.2f}'
            else:
                strat += f'Alert tại {entry:,.2f}, vào khi có xác nhận'
            strat += f'. SL {sl_swing:,.2f} (-{risk_pct:.0f}%) — TP {tp_opt:,.2f} (R:R 1:{rr_actual:.1f}'
            if sl.get('tp_was_capped'):
                strat += ', đã giới hạn theo biên độ thực tế'
            strat += ')'

            if shares > 0 and value > 0:
                strat += f'. Size: {shares:,.0f} cp ({value/1e6:,.1f}M VND)'

            if hold_plan:
                strat += f'. Nắm giữ: {hold_plan}'


            lines.append(strat + '.')

        # Priority ranking hint
        if score >= 75:
            lines.append(f'Ưu tiên CAO trong danh sách.')
        elif score >= 60:
            lines.append(f'Ưu tiên TRUNG BÌNH — cần thêm xác nhận.')

        return '\n'.join(lines)



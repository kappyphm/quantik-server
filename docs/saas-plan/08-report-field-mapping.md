# 08 — Report v1 mapping và compatibility

Mapping thực thi tại backend/src/service/report_assembly.py. Các nested keys của mỗi engine được giữ nguyên
dưới tên legacy, thay vì flatten hoặc chỉ giữ summary Excel. Literal-key inventory từ prototype tại
quant-field-inventory.json hỗ trợ audit; inventory đó bao gồm cả internal keys, không thay hợp đồng runtime.

| Section | Legacy inputs được giữ |
|---|---|
| data_quality | data_quality, status/score/flags/history/gaps/staleness |
| liquidity | liquidity, ADV/tier/capacity/gap/price-lock proxies |
| distribution | dist, tests/fat_tail/fits/verdict |
| statistics | stats, vol, ac, n, range, date, _price_dates, _price_close |
| arima | arima, stationarity/model/coefficients/forecast/diagnostics |
| garch | garch và egarch model/params/forecast/diagnostics |
| hmm | hmm, regime/probabilities/states/transitions/history |
| technical | alpha, sr, trend, flow |
| context | sector, cross_corr (full universe reference) |
| directional_alpha | momentum_alpha, hurst, conditional_mr, lightgbm_cross_sectional, cross_sectional_model |
| agreement | fcast, weights/component returns/coverage/support/direction/timing |
| simulation_risk | simulation_risk, MC/lock/unlock/quantile trajectories |
| costs | costs, assumptions/gross/net/breakeven |
| trade_plan | sl, pos, sizing_mode, kelly, meta_label |
| decision | rec (score/factor breakdown), action (executable/gates/reason_codes) |
| commentary | preserved summary/detailed text, explicit compatibility metadata |

ReportResult có report_id do storage cấp; envelope gồm schema_version/job_id/symbol/evaluated_at/data_as_of,
input_mode, versions, sections, decision, commentary và warnings. OpenAPI expose ReportResult/Report/Section.
Timestamp envelope UTC. Giá equity nội bộ VND, index context points, volume shares, returns _pct khác _ratio.

Quyết định chủ động:

- Không lưu _mc_paths nguyên mảng; giữ mc_path median/upper/lower và mọi distribution/tail/settlement summaries.
- Meta-label, tracking outcomes, Kelly disabled; probability không được fabricate. Meta status DISABLED rõ.
- Screener labels không cộng/trừ điểm; normalization dùng market snapshot, không dùng request universe.
- Sizing mặc định REFERENCE 500 triệu; PERSONALIZED theo vốn/risk input, không đổi shared numerical factors.
- Group UNAVAILABLE giữ metrics/error/reasons, không giả như AVAILABLE rỗng. Required sections vẫn gate trước commit.
- Nonfinite numeric chuyển null với warning path, không đưa NaN/Inf vào JSON.
- Khi LightGBM unavailable, action không executable và không BUY_NOW, reason MODEL_UNAVAILABLE.

Mapping/assembly đã kiểm tra bằng fixtures có baseline/stat/risk calculations; full nested live regression,
bootstrap model và scientific review vẫn là gate trước mở beta. Fixture không thay scientific evidence.

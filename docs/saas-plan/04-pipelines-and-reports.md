# 04 — Pipelines và hợp đồng báo cáo

## Bảo toàn prototype

Nguồn chính: [crawl_data.py](../prototype/crawl_data.py), [quant.py](../prototype/quant.py). Bảng dưới là inventory cấp nhóm, chưa phải danh sách mọi nested key. Trước khóa schema v1 phải trích xuất và đối chiếu từng field thực tế, summary columns, commentary, diagnostics và tính toán phục vụ visuals. Không tự giản lược vì field không có trong summary Excel.

| Source | Report group | Nội dung |
|---|---|---|
| DataQualityEngine | data_quality | status/score/flags, history, gaps, staleness, invalid OHLC |
| LiquidityEngine | liquidity | ADV20 shares/value, tier, capacity, gap/price-lock proxies |
| DistributionAnalyzer | distribution | tests, tail/normality diagnostics, verdict |
| StatEngine | statistics | return/risk metrics, vol regimes, autocorrelation |
| ARIMAEngine | arima | reference forecast và fit diagnostics |
| GARCHEngine | garch | conditional volatility, forecasts, diagnostics |
| HMMEngine | hmm | states/probabilities và lịch sử regime cần cho FE |
| AlphaEngine/StructureEngine | technical | relative strength, composite alpha, SR/trend/flow |
| SectorEngine/CrossCorrelationEngine | context | sector trend, correlation/VAR khi đủ inputs |
| Momentum/Hurst/ConditionalMR/LightGBM | directional_alpha | component outputs, availability, strength/direction |
| AgreementEngine | agreement | weights, coverage, support, direction, timing |
| FcastEngine | simulation_risk | quantiles, probability cone summaries, tails, settlement lock |
| CostEngine | costs | fees/slippage assumptions, gross/net forecast |
| RiskEng | trade_plan/sizing | entry/SL/TP, holding plan, capacity/risk constraints |
| AdaptiveScorer/ActionEngine | decision | score breakdown, action, executable, reasons |
| Commentary | commentary | diễn giải từng nhóm và kết luận chi tiết |

Loại meta-label/outcome fields hoặc đánh dấu disabled trong compatibility mapping; không trả probability giả. Kelly vốn disabled trong prototype tiếp tục disabled. ARIMA không tự thêm phiếu vào directional agreement.

## Report envelope đề xuất

```json
{
  "schema_version": "1.0",
  "report_id": "uuid",
  "job_id": "uuid",
  "symbol": "FPT",
  "evaluated_at": "UTC timestamp",
  "input_mode": "COMPLETED_DAILY",
  "data_as_of": "UTC timestamp",
  "versions": {"data_manifest": "id", "market_snapshot": "id", "model": "id", "config": "id", "engines": {}},
  "sections": {},
  "decision": {},
  "commentary": {},
  "warnings": []
}
```

Section có status AVAILABLE/UNAVAILABLE/DISABLED, reason_codes, units và metrics. JSON không NaN/Inf. Agreement không là probability thắng. Paths Monte Carlo không lưu toàn bộ trong mỗi report: lưu quantile trajectories và summaries đủ cho FE; exact fields cần parity inventory. APIs có thể phân trang series dài nhưng không bỏ dữ liệu âm thầm.

Sizing PROPOSED: khi có vốn/risk budget hợp lệ thì personalized; nếu thiếu giữ reference 500 triệu theo prototype, mode REFERENCE rõ trong mọi output. Không coi là position recommendation cho khách. Công thức/giới hạn theo prototype, mọi thay đổi có version.

## Ingestion Vnstock v5

Adapter dùng giao diện v5, pin package version. Tách instrument/index units; normalize timezone/schema/source metadata, validate finite/OHLC/dates/volume và revision policy. Không đoán multiplier chỉ từ độ lớn giá. Contract test với dữ liệu live nhỏ trước backfill.

Backfill chiều dài theo feature/model requirement; không nhầm ngày lịch với số nến. Incremental có overlap để phát hiện correction. Shared limiter kiểm soát requests theo tier/route thực tế, tier caps và retry-after; entitlement/provider error không giả thành bảng rỗng hợp lệ.

Readiness kiểm tra required index, freshness và coverage. Missing corporate-action adjustment hoặc exchange reference phải xuất warning/gate theo policy. Không khẳng định nguồn fallback có schema/adjustment tương đương trước kiểm tra.

## Screening

CONFIRMED: toàn sàn, hai slot, sáng provisional daily. PROPOSED: trigger 11:30/15:00 VN; lịch nghỉ và readiness làm điều kiện thực thi, không ép dữ liệu thành completed lúc 15:00.

Flow: claim unique slot → ensure manifest → market context một lần → chunk criteria → global ranking → completeness validation → atomic publish latest.

Giữ 21 tiêu chí gốc và lý do từng criterion. Criterion TRUE/FALSE/UNAVAILABLE; không coi thiếu history cần thiết là false. Mỗi criterion có input requirements/minimum bars và reason code khi unavailable. Không áp một ngưỡng history duy nhất cho cả 21 tiêu chí.

### Score, coverage và provisional volume đề xuất

Mỗi nhóm MẠNH/TÍCH LŨY/PHÂN PHỐI có 7 tiêu chí. Trả `true_count`, `false_count`, `unavailable_count`, `coverage_ratio = (true_count + false_count) / 7` và `score = true_count / 7 × 100`. UNAVAILABLE giữ nguyên trạng thái và lý do; score khi coverage < 1 chỉ là điểm trên tổng 7 tiêu chí, không diễn giải là mọi tiêu chí còn lại đều không đạt. Không chia lại cho số tiêu chí có dữ liệu.

Ranking chính từng nhóm chỉ nhận mã có coverage 7/7 của nhóm đó và pass các hard filters/readiness tương ứng; không yêu cầu đủ cả ba nhóm khi chỉ xếp một nhóm. Overall Top 70 dùng max(score MẠNH, score TÍCH LŨY) khi cả hai nhóm đủ coverage. Nhóm PHÂN PHỐI thiếu dữ liệu phải có warning, không được diễn giải là không có rủi ro. Tie-break theo symbol tăng dần để pagination/ranking ổn định. Mã thiếu coverage vẫn lưu và trả trong all-results, có `ranking_eligible=false` và reasons; không trộn vào Top 70.

Sáng: dùng volume quan sát thực tế tới cutoff và baseline 20 phiên completed trước phiên hiện tại, loại nến provisional khỏi baseline. Chưa nhân volume theo tỷ lệ thời gian hoặc dự đoán cả ngày. Các rolling volume features có nến hiện tại phải được liệt kê trong criterion mapping cùng input mode và cửa sổ thực tế. Trả raw volume, observed_at, baseline, ratio và policy version; field projected/adjusted volume để null với reason DISABLED khi chưa có phương pháp đã kiểm định. Ngưỡng criterion gốc chỉ là beta policy đề xuất trên số liệu provisional, không tuyên bố tương đương completed policy.

Run readiness tách khỏi criterion coverage: kiểm tra target session/cutoff, required index và số mã đủ inputs so universe kỳ vọng. Coverage threshold của run, thời hạn chờ và xử lý zero eligible phải cấu hình/chốt trước nghiệm thu. Không publish một run thiếu dữ liệu dưới tên run hoàn chỉnh; quá deadline giữ latest cũ và công bố failed/stale status.

Lưu tất cả evaluated/rejected symbols và reasons. Top 70 mặc định PROPOSED để gần prototype; API filter không âm thầm mất phần còn lại. Run lỗi giữ latest cũ, hiển thị stale và run failure.

## Analysis graph

Load immutable request/target session → resolve completed history/index/sector và market snapshot → pin versions → baseline components + simulation_risk → published LightGBM inference → agreement → costs → stops/trade_plan → sizing → score → action → commentary/output validation → commit.

`simulation_risk` là forecast distribution/Monte Carlo trước agreement; `trade_plan` là entry/SL/TP sau khi có final agreement và costs. `RiskEng.stops()` nhận final costs, không tính trước rồi sửa lại. Sizing dùng plan cuối cùng; ActionEngine chỉ đọc score/plan/sizing cuối cùng.

Stock-specific và market/sector components reuse theo dependency. Tính một pass trên final inputs, tránh recompute stops trước và sau pooled prediction như prototype. Score bỏ screener bonus/penalty, normalization dùng reference universe; scientific behavior change ghi policy version.

Monte Carlo giữ seed/method/config để reproducible; không giảm models/inits/paths vì performance mà không regression. Unknown/failed data, insufficient alpha coverage và invalid plan chặn executable theo final policy. Không fabricate BUY_NOW nếu LightGBM unavailable.

## Snapshot cross-sectional đề xuất

Mục tiêu: cùng symbol, target session, data/model/config và sizing inputs cho cùng kết quả khi gọi riêng hoặc trong batch. Không dùng tập mã của request, screening candidates hoặc các jobs đang chờ để tính LightGBM rank, sector context hay score normalization.

Sau khi completed manifest ready, background builder chạy shared graph: eligible universe → sector returns/trends/correlation context → features + inference bằng published model → cross-sectional rank → baseline factor distributions. Training chạy hằng tuần nhưng inference/rank/reference được cập nhật mỗi phiên completed. Chi phí tính factors toàn universe phải được benchmark riêng và được reuse; không lặp toàn graph này cho từng khách. Screening sáng dùng manifest/context provisional riêng và không ghi đè snapshot completed.

| Thành phần | Contract |
|---|---|
| Identity | snapshot_id, target_session, input_mode=COMPLETED_DAILY, data_manifest_id, universe_version, sector_reference_version, model_version hoặc null có reason, engine/config/policy versions |
| Membership | Danh sách eligible/excluded symbols và reasons; mỗi member pin history revision, feature as_of, sector và availability |
| LightGBM | Predictions absolute/alpha, rank_pct và n_ranked_symbols; rank trên các eligible predictions cùng phiên, không lấy last valid row cũ của mã stale vào rank hiện tại |
| Scoring | Raw reference factors theo mã, n/median/Q1/Q3 từng factor và normalization policy; zero-IQR/min-sample behavior phải explicit và versioned |
| Sector | Member universe, return-series/history window và các outputs ngành; missing members/coverage có metadata |
| Publication | BUILDING/READY/FAILED; checksum, built_at, data_as_of và validation evidence; unique dependency key, atomic publish |

Normalization dùng cùng công thức robust z-score/blend với policy đã version, thay reference batch bằng reference snapshot. Tách việc bỏ screener bonus/penalty và thay universe thành các scientific changes có expected differences, không yêu cầu numerical parity tuyệt đối cho những fields chủ động đổi. Universe eligibility, minimum coverage/sample và zero-IQR policy còn OPEN tại O-09, cần khóa khi implementation phần này.

Request pin target session lúc admission. Snapshot/dependency chưa ready thì WAITING_DATA và shared dependency request được deduplicate. Job có dependency deadline từ config; hết deadline → FAILED với reason SNAPSHOT_NOT_READY/DATA_NOT_READY và release reservation. Không âm thầm dùng phiên cũ. Symbol ngoài screening vẫn được phân tích nếu đủ dữ liệu; symbol ngoài eligible snapshot có thể nhận report với cross-sectional sections UNAVAILABLE, không tự gán rank 100 hoặc thêm vào reference universe. Việc report degraded có được coi là success/thu lượt phải theo policy tại mục 05.

Correction dữ liệu hoặc thay model/config tạo snapshot revision mới. Retry đã pin dùng revision cũ; request mới resolve bản READY tương thích. Tuổi model được đánh giá tại thời điểm đánh giá đã pin, ghi trong report; model quá tuổi có contribution unavailable thay vì dùng snapshot để lách age policy. Nếu không có model hợp lệ, snapshot vẫn có thể chứa sector/factor context nhưng không fabricate LightGBM predictions; gate bootstrap model trước mở beta vẫn áp dụng.

Nghiệm thu: single/batch cho cùng inputs có rank/strength/score/action giống nhau; snapshot thiếu không tính rank cục bộ; stale symbol không trộn phiên; correction/retry giữ revisions; publish crash không đổi latest; đo riêng thời gian/resource xây snapshot và thời gian customer analysis.

## Training hằng tuần

CONFIRMED weekly. PROPOSED thứ Bảy 02:00 sau manifest thứ Sáu ready; holiday dùng phiên hoàn tất gần nhất.

Universe là cổ phiếu hợp lệ toàn sàn, không dùng screening candidates; lưu eligibility/exclusions. Training labels chỉ dùng horizon đã mature tại cutoff. Temporal split theo ngày, purge/embargo tối thiểu horizon phù hợp; không random row split gây leak giữa mã cùng ngày. Fit preprocessing trên train only.

Validation gồm MAE/RMSE, directional diagnostics, rank IC và performance so baseline/published trên cùng holdout; threshold/sample minimum phải chốt, không đặt promotion tùy tiện. Artifact có checksum và dependency versions, publish transaction atomic, job pin model.

Age PROPOSED: warn 14 ngày, disable contribution 28 ngày, coverage/action phản ánh thiếu model. First deployment có bootstrap train kiểm định; không đợi cuối tuần mới có model. Failure giữ model hợp lệ trước, admin thấy reason. Tắt tracking outcomes không tắt historical label generation.

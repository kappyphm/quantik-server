# 09 — Screening beta-1: inputs, coverage và publication

Implementations: service.quant.screening (pure), screening_legacy (preserved prototype), service.screening
(orchestration), screening_contracts/OpenAPI (DTO). Không sửa snapshot crawl_data.py.

## Tiêu chí và minimum bars

| ID | Điều kiện gốc | Minimum bars completed |
|---|---|---:|
| S1_RS_market | RS 1 phiên > VNINDEX cùng các ngày stock | 2 |
| S2_vol_spike | Volume / avg20 >=1.5 | 20 |
| S3_near_high20 | Close >=95% High20 | 20 |
| S4_vol_dry_pullback | AvgVol5 < AvgVol20 | 20 |
| S5_breakout_high20 | Close >=High20 | 20 |
| S6_hh_hl_structure | Hai đỉnh/đáy gần nhất tăng; pivot phải được xác nhận | 21 |
| S7_RS_10d | RS 10 phiên >0 | 11 |
| A1_tight_range | Range20 <=15% | 20 |
| A2_vol_declining | AvgVol5 < AvgVol20 | 20 |
| A3_absorption | Trong 20 phiên: volume >=2x avg20, return >-2% | 39 |
| A4_above_ma20 | Close >MA20 | 20 |
| A5_near_resistance | Close >=90% High20 | 20 |
| A6_long_accumulation | >=15/20 ranges20 <=15%, giữ guard 40 bars của prototype | 40 |
| A7_spring | Trong 10 phiên: low thủng prior Low20, close >open | 30 |
| D1_vol_no_price_up | VolRatio>=2, abs return<=1% | 20 |
| D2_long_upper_shadow | Upper-shadow/range >=50%; zero range theo prototype là 0 | 1 |
| D3_failed_breakout | Trong cửa sổ 10: vượt prior High20 rồi close dưới nó | 29 |
| D4_vol_on_down | Mean down-volume >mean up-volume trong 10 phiên | 11 |
| D5_below_ma20 | Close <MA20 | 20 |
| D6_drop_from_peak | Giảm >=5% từ High20 | 20 |
| D7_multi_dist_sessions | >=3/10 phiên vol>=1.5x avg20, abs return<=1.5% | 29 |

Thiếu history hoặc field finite → UNAVAILABLE, không false. D4 thiếu một phía up/down →
UNAVAILABLE với NO_COMPARISON_SESSIONS. RS không forward-fill index thiếu ngày. Các thay đổi availability
này chủ động khác prototype; completed data đủ inputs giữ các boolean/công thức gốc.

## Volume sáng và scoring

AM dùng raw volume tới quan sát; avg20 hiện tại là 20 phiên completed trước đó, không chứa provisional.
S2/S4/A2/D1 cần 21 bars ở AM. Avg5 vẫn có current raw candle. A3/D7 giữ cách tính historical rows gốc,
riêng provisional row dùng completed-history baseline. Price MA/high/range có provisional như D-05.
Không ngoại suy volume ngày; projected_volume null, reason DISABLED. Response ghi observed_at và policy.

Mỗi nhóm có 7 criteria TRUE/FALSE/UNAVAILABLE, true/false/unavailable counts. Score=true_count/7*100;
coverage=(true+false)/7. Không đổi denominator khi thiếu dữ liệu. Group ranking cần 7/7 và hard filter.
Overall cần cả STRONG và ACCUMULATION đủ coverage, score=max hai nhóm, tie-break symbol tăng dần.
Distribution thiếu coverage có warning, không coi như không có rủi ro. Top mặc định 70; all-results giữ
mọi universe member, gồm thiếu history/không đạt thanh khoản. Hard filter AvgVol20>=100.000 shares như prototype.

Assessment/narrative, market regime/risk/strategy và adjusted signal giữ prototype khi inputs đủ.
Adjusted signal dùng rounded score như prototype, không sửa raw group score/ranking. Thiếu group coverage
hoặc hard filter fail không phát nhận xét như dữ liệu đầy đủ. Equity prices VND, index points;
price_summary pct_change_* giữ legacy ratio, units explicit.

## Readiness và deadline

| Slot | Trigger VN | Mode | Readiness / deadline |
|---|---|---|---|
| AM | 11:30 | PROVISIONAL_DAILY | Observations cùng phiên từ 11:30 đến trước 13:00; publish trước 12:55 |
| PM | 15:00 | COMPLETED_DAILY | Completed gate từ 16:00; publish trước 17:30 |

Run readiness khác criterion coverage: VNINDEX đúng phiên/mode, >=50 bars cho market context;
Có histories hợp lệ cho các mã xử lý được; ít nhất một group có eligible symbols.
Theo xác nhận mới ngày 18/10/2026, không chặn công bố theo tỷ lệ độ phủ toàn sàn.
Mã thiếu/lỗi dữ liệu vẫn giữ UNAVAILABLE và bị loại khỏi xếp hạng; độ phủ thực tế lưu trong summary. Thời gian chờ hữu hạn.
Thiếu input trước deadline giữ WAITING_DATA; hết deadline FAILED. Zero eligible là failure explicit.
Source default yêu cầu LIVE_VALIDATED universe + histories. Fixtures chỉ chạy khi cho phép rõ và bị chặn
khỏi API mặc định. Ingest giữ short histories (minimum 1) để screening đánh giá từng criterion;
quant/model giữ MIN_HISTORY riêng (default 120), không dùng guard 120 cho cả screening.

Một row mỗi target_session/slot, lease/fencing, pin manifest mỗi attempt; published READY bất biến,
retry trả cùng run. Transaction publish results + run + ALL/slot pointers; failures giữ latest.
Pointer không bị historical run ghi đè run mới. Stale API tách latest attempt thất bại/chờ và publication age>36h;
age không có nghĩa tự khẳng định bỏ lỡ một trading slot ngày nghỉ. Calendar/scheduler là thành phần kế tiếp.

Các ngưỡng beta do agent chọn theo quyền được giao; scientific/live screening acceptance vẫn cần evidence.

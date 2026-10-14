# 01 — Biên bản và quyết định

Ngày: 15/10/2026. Người tham gia: chủ sản phẩm và agent. Phạm vi: backend SaaS độc lập từ prototype.

## Quyết định đã chốt

| ID | Trạng thái | Quyết định |
|---|---|---|
| D-01 | CONFIRMED | Chỉ triển khai BE; FE do bên khác làm và khớp API sau |
| D-02 | CONFIRMED | Ba ngày có BE end-to-end deploy VPS cho nhóm khách đầu tiên |
| D-03 | CONFIRMED | Hai tính năng chính: screening và phân tích chuyên sâu một mã theo yêu cầu |
| D-04 | CONFIRMED | Screening toàn HOSE/HNX/UPCoM, bắt đầu cuối phiên sáng và cuối phiên ngày |
| D-05 | CONFIRMED | Screening sáng dùng nến ngày tạm thời; bản đầu chỉ dùng khung nến ngày |
| D-06 | CONFIRMED | Thông tin đầu ra bám sát prototype, hạn chế giản lược |
| D-07 | CONFIRMED | LightGBM train hằng tuần |
| D-08 | CONFIRMED | Bỏ backtest và quant_visuals khỏi sản phẩm; tắt meta-label/tracking outcomes; bỏ tin tức |
| D-09 | CONFIRMED | Google Auth cho user; JWT cho cả user/admin; admin username/password riêng được seed |
| D-10 | CONFIRMED | Quản lý gói, quota, giới hạn jobs và billing qua admin config; tích hợp SePay |
| D-11 | CONFIRMED | Vnstock v5, tài khoản trả phí; chú ý hạn mức |
| D-12 | CONFIRMED | Docker trên VPS, có khả năng scalability |
| D-13 | CONFIRMED | Hơn 1.000 user sau một tháng; peak 30–50% user gửi yêu cầu, cùng mã hoặc nhiều mã |
| D-14 | CONFIRMED | Thời gian hoàn tất có thể thoải mái; chưa chốt SLA |

## Đề xuất thiết kế

| ID | Trạng thái | Phương án |
|---|---|---|
| P-01 | PROPOSED | FastAPI + PostgreSQL + RabbitMQ, modular monolith, processes tách riêng |
| P-02 | PROPOSED | Screening trigger 11:30/15:00, chờ input đạt readiness mới publish |
| P-03 | PROPOSED | Training thứ Bảy 02:00; universe cổ phiếu hợp lệ toàn sàn, version riêng |
| P-04 | PROPOSED | Phân tích sâu/training dùng completed daily; screening sáng dùng provisional daily |
| P-05 | PROPOSED | Universe normalization ổn định; bỏ cộng/trừ điểm từ nhãn screening |
| P-06 | PROPOSED | JWT access ngắn hạn, refresh token opaque rotate/revoke |
| P-07 | PROPOSED | Model cảnh báo tuổi 14 ngày; sau 28 ngày LightGBM unavailable |
| P-08 | PROPOSED | Giữ sizing tham chiếu 500 triệu khi thiếu thông tin vốn, ghi rõ minh họa |
| P-09 | PROPOSED | Gói trả trước từng kỳ; gia hạn qua đơn mới, chưa tự động debit |
| P-10 | PROPOSED | Snapshot cross-sectional bất biến theo phiên/data/model/config; request đơn và batch dùng cùng reference |
| P-11 | PROPOSED | Screening mỗi nhóm score = true_count / 7 × 100; ranking chính chỉ nhận nhóm coverage 7/7 |
| P-12 | PROPOSED | Screening sáng dùng raw volume với completed-history baseline, chưa ngoại suy volume cả ngày |
| P-13 | PROPOSED | Quota reserve khi admission, consume khi success hoặc user cancel sau compute_started_at; lỗi kỹ thuật/dữ liệu release |
| P-14 | PROPOSED | Khởi chạy backfill/bootstrap training ngày 1; report đầy đủ với dữ liệu/model thật là gate ngày 2 và trước mở beta |

## Vấn đề mở

| ID | Nội dung | Cần giải quyết |
|---|---|---|
| O-01 | VPS, ngân sách, domain và retention | Chủ sản phẩm trước deploy |
| O-02 | Giá/quota/job limits beta | Chủ sản phẩm trước mở khách; có thể seed giá trị tạm đã được duyệt |
| O-03 | Ngưỡng readiness screening, score/coverage và provisional volume criteria | Review P-11/P-12 trước nghiệm thu screening; mục 04 có mặc định đề xuất |
| O-04 | Universe/history/validation và ngưỡng promotion model | Scientific review trước publish model |
| O-05 | Gia hạn/nâng gói, hoàn tiền, đối soát | Product review trước bật từng policy |
| O-06 | Report field inventory hoàn chỉnh và sizing | Đối chiếu code trước khóa report v1 |
| O-07 | Năng lực tải và provider thực tế | Benchmark/contract tests trên hạ tầng thật |
| O-08 | Cancel sau compute có tính lượt; job qua kỳ subscription; report degraded | Product review P-13 và bảng settlement mục 05 trước mở gói cho khách |
| O-09 | Eligibility/coverage snapshot, zero-IQR policy và thời hạn chờ dữ liệu | Technical/scientific review mục 04 trước nghiệm thu analysis một mã |

Các bổ sung trên không chuyển đề xuất thành quyết định CONFIRMED. Có thể triển khai schema, adapters, quant extraction, jobs và auth trong khi hoàn thiện policy; không bật policy thu lượt chưa chốt cho khách.

Người dùng đồng ý dùng tài khoản Vnstock trả phí; không coi tài liệu này là xác nhận pháp lý về quyền phân phối dữ liệu. Không yêu cầu onboarding/install package chỉ vì AGENTS.md có bootstrap, khi công việc hiện tại là soạn thiết kế.

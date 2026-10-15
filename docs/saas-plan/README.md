# Backend SaaS phân tích chứng khoán Việt Nam

Ngày lập: 15/10/2026. Trạng thái: thiết kế phục vụ triển khai beta, chưa phải bằng chứng hệ thống đã hoạt động.

Bộ tài liệu này được lập từ cuộc trao đổi với người dùng và prototype trong thư mục `../prototype/`. Đây là kế hoạch độc lập; không kế thừa milestones, runtime hoặc quyết định từ tài liệu backend bên ngoài thư mục này. Không sửa các snapshot Python gốc.

Tài liệu đủ để bắt đầu implementation. Các mục PROPOSED là thiết kế để triển khai/review; các quyết định sản phẩm còn OPEN phải được chốt trước gate liên quan, không chặn toàn bộ backend. Bổ sung ngày 15/10/2026: snapshot cross-sectional, screening score/coverage, job/quota settlement và dependency của kế hoạch ba ngày.

## Cách đọc

| Tài liệu | Nội dung |
|---|---|
| [01 — Biên bản và quyết định](01-decisions.md) | Phạm vi đã chốt, đề xuất và vấn đề mở |
| [02 — Yêu cầu và nghiệm thu](02-requirements.md) | Chức năng, tải, bảo mật và acceptance |
| [03 — Kiến trúc và dữ liệu](03-architecture.md) | Modules, persistence, jobs và scaling |
| [04 — Hợp đồng đầu ra và pipeline](04-pipelines-and-reports.md) | Bảo toàn prototype, screening, quant và training |
| [05 — API, auth và billing](05-api-auth-billing.md) | JWT, endpoints, quota và SePay |
| [06 — Triển khai và vận hành](06-delivery-and-operations.md) | Mốc ba ngày, Docker, monitoring và kiểm thử |
| [07 — Implementation log](07-implementation-log.md) | Quyết định triển khai và evidence thực tế |
| [08 — Report mapping](08-report-field-mapping.md) | Nested outputs và compatibility contract |
| [09 — Screening policy](09-screening-policy.md) | 21 criteria, availability, scoring và publication |
| [10 — Billing policy](10-billing-policy.md) | Prepaid plans, SePay matching, renewal và đối soát |
| [11 — Admin operations](11-admin-operations.md) | Customer lock, quota refund, audit và operational views |
| [12 — Scheduled operations](12-scheduled-operations.md) | Versioned configs/calendar, durable daily/weekly tasks và recovery |
| [13 — Local infrastructure evidence](13-local-infrastructure-evidence.md) | Real PostgreSQL/RabbitMQ races, JSONB integrity và dump/restore |
| [14 — Observability](14-observability.md) | Private health/metrics, process heartbeat, logs và alert templates |

## Quy ước

- `CONFIRMED`: người dùng đã chốt trong cuộc trao đổi.
- `PROPOSED`: phương án kỹ thuật/mặc định do agent đề xuất, chưa được chốt thành policy sản phẩm.
- `OPEN`: thiếu quyết định hoặc bằng chứng; không được tự giả định đã hoàn tất.
- Múi giờ lịch và hiển thị: `Asia/Ho_Chi_Minh`. Thời điểm lưu trữ: UTC; ngày phiên giữ riêng.
- Ba ngày là mục tiêu bàn giao backend beta end-to-end cho nhóm khách đầu tiên; khả năng peak 500 yêu cầu phải có evidence riêng.

## Nguồn

Prototype: [crawl_data.py](../prototype/crawl_data.py), [quant.py](../prototype/quant.py), [quant_visuals.py](../prototype/quant_visuals.py), [backtest.py](../prototype/backtest.py).

Tài liệu provider được đọc trong cuộc trao đổi: [Vnstock v5 migration](https://www.vnstocks.com/docs/vnstock/chuyen-doi/tu-vnstock-4x), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [SePay webhook](https://developer.sepay.vn/vi/sepay-webhooks/tich-hop-webhook). Cần kiểm tra lại phiên bản và payload khi triển khai; tài liệu không thay thế contract tests với tài khoản thật.

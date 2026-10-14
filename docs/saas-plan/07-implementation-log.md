# 07 — Implementation log

## 15/10/2026 — Quyền quyết định và thứ tự thực hiện

Chủ sản phẩm giao agent quyết định để triển khai toàn bộ SaaS, yêu cầu Git và commit mỗi luồng/thành phần
hoàn chỉnh. CI/CD làm sau khi hệ thống ổn định. Docker chỉ kiểm tra tích hợp khi cần; dọn tài nguyên test
theo đúng project, không prune tài nguyên ngoài dự án.

Agent chọn FastAPI/PostgreSQL/RabbitMQ modular monolith theo P-01, RSA JWT và refresh rotation theo P-06.
Đây là quyết định kỹ thuật trong quyền được giao, không phải bằng chứng runtime production đã đạt.
SQLite chỉ là backend local để phát triển không cần Docker; nghiệm thu concurrency phải trên PostgreSQL.

Ưu tiên: identity → jobs/quota/outbox → data contracts/ingestion → quant/report parity → snapshots/model
→ screening → billing/admin → scheduler/deploy/recovery/load evidence. Backfill/bootstrap chạy sớm khi adapter
live được kiểm chứng. Các ngưỡng khoa học chỉ publish khi có validation evidence.

## Thành phần identity

Đã triển khai schema/migration, admin seed Argon2id, RSA access JWT, refresh hash/rotation/reuse,
DB session revocation, durable login throttles, Origin check và audit. README có lệnh install/run/test thực tế.
Secrets giữ ngoài Git; chỉ kiểm tra tên biến và có/không có giá trị, không in giá trị.
Các phần Google login, customer jobs, billing, provider và scientific pipelines chưa được nghiệm thu.

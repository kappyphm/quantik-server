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

## Thành phần Google identity

Đã thêm browser-bound state/nonce/PKCE, verify Google OIDC, nhận diện bằng sub, one-time exchange,
user refresh/logout và role/audience isolation. Migration 0002 lưu OAuth flow dùng chung giữa replicas.
Provider credentials có trong .env; chưa chứng minh Google consent/redirect hoạt động live.
Admin local đã seed thành công sau khi người dùng cập nhật mật khẩu đủ độ dài.

## Thành phần jobs/quota

Agent chọn P-13 cho beta-1: cancel sau compute consume, errors release, pin entitlement/bucket/policy tại admission.
Job/outbox/reservation atomic; unique account/idempotency; lease/fencing; completed report/settlement atomic.
API user hỗ trợ admission, status/history/cancel/reports/usage và ownership. Test local kiểm tra redelivery claim,
cancel-completion ordering, stale worker, pinned retries, original bucket release và incomplete reports.
Đây chưa phải evidence PostgreSQL concurrent races; RabbitMQ dispatcher/worker chưa có.

Minimum report gate hiện yêu cầu đủ 16 nhóm theo inventory tài liệu, với 10 nhóm required AVAILABLE;
ARIMA/GARCH/HMM/context/distribution/technical vẫn có thể UNAVAILABLE kèm reasons.
Gate nested fields/numerical parity chưa đạt; sẽ siết theo output inventory, không publish report giả ra khách.

## Evidence runtime local

API uvicorn local khởi động thành công; health/live, health/ready, login admin và admin/me trả HTTP 200
với cấu hình thật trong .env; logout hoàn tất. Hai kiểm tra identity và một kiểm tra jobs core đã đạt.
Không build image, tạo container hay volume Docker. Chưa có live Google consent, SePay/Vnstock hoặc VPS evidence.

## Thành phần market-data

Đã cài và pin Vnstock 5.0.1 theo kho chính thức; không dùng bootstrap v4/vnai hoặc tải skill markdown.
Thử login thật gặp VNSTOCK_AUTH_DEVICE_LIMIT; người dùng yêu cầu chờ slot vì người phụ trách đang nghỉ.
Không retry login đến khi có cập nhật. Đây chỉ chặn provider live/backfill/model evidence, không dừng coding.

Adapter và ingest CLI có unit/time/schema validation, minimum history, cap detection, immutable revisions,
versioned universe/manifest và provider rolling-window gate/cooldown dùng chung qua DB.
Agent chọn 95% universe history-ready + VNINDEX đúng phiên làm ngưỡng manifest beta; exclusions explicit.
V5 Market docs xác nhận equity nghìn VND, index points; adjustment giữ UNVERIFIED đến khi live contract đạt.
History revisions hiện lưu payload nguyên chuỗi để bắt đầu pipeline; trước tải lớn cần đo dung lượng và cân nhắc
chuyển bars sang storage chuyên dụng, giữ nguyên revision/manifest contracts.
Test local đã đạt units, provisional/completed readiness, bad bars, caps, correction, coverage,
mixed-version rejection và shared budget across sessions. Chưa có provider live hay multi-worker PG evidence.

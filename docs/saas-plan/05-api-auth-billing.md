# 05 — API, JWT, gói và SePay

## API conventions

PROPOSED `/api/v1`, OpenAPI là contract giao FE. UUID IDs, UTC ISO timestamps, cursor pagination. Errors có code/message/request_id/details an toàn. Validation 422, unauthenticated 401, forbidden 403, not-found 404 (không lộ owner), idempotency conflict 409, rate limited 429 và retry metadata.

Mutation hỗ trợ Idempotency-Key khi có effect tài chính/job. GET không tạo side effects. Có request correlation, không log tokens/credentials/raw payment payload. CORS allowlist theo domain FE khi tích hợp.

| Nhóm | Endpoints đề xuất |
|---|---|
| User auth | GET /auth/google/start; GET /auth/google/callback; POST /auth/refresh; POST /auth/logout |
| Profile | GET /me; GET /me/entitlements; GET /me/usage |
| Market | GET /instruments; GET /screenings/latest; GET /screenings/{run_id}/results |
| Analysis | POST /analyses; GET /analyses; GET /analyses/{id}; POST /analyses/{id}/cancel; GET /reports/{id} |
| Billing | GET /plans; POST /orders; GET /orders/{id}; GET /payments; GET /subscription |
| Webhook | POST /webhooks/sepay |
| Admin auth | POST /admin/auth/login; POST /admin/auth/refresh; POST /admin/auth/logout |
| Admin | /admin/users, /plans, /subscriptions, /payments, /reconciliation, /jobs, /configs, /models, /data-health, /audit |
| Health | /health/live; /health/ready; private metrics |

Admin mutation actions có endpoint/permission riêng; không generic patch cho mọi DB columns. Exact schemas được khóa sau field inventory.

## JWT

CONFIRMED BE phát JWT riêng cho user/admin. PROPOSED asymmetric signing, aud riêng user/admin, iss cố định, claims sub/role/iat/exp/jti/session_id; algorithm allowlist và kid rotation. Roles không lấy từ body client. Access 15 phút mặc định PROPOSED.

Google authorization-code flow: state/nonce và PKCE khi phù hợp, verify issuer/audience/expiry/signature, identity theo Google sub. Callback không đặt JWT trong URL; FE sau này nhận qua secure exchange. Không dùng Google access token làm API JWT.

Refresh opaque random token, lưu hash/session trong DB; rotation atomic, reuse detection revoke family. PROPOSED HttpOnly Secure cookie scoped endpoint, CSRF/origin protection cho cookie-based mutations. Access token qua Authorization Bearer. Role/audience separation áp dụng cả refresh cookie paths. Logout/password change/lock revoke sessions; access checking session/account state hoặc TTL ngắn phải được chốt, không hứa revoke tức thời với JWT purely stateless.

Admin seed explicit command, lấy secret/env, Argon2id, unique username, idempotent không reset existing password. Không default password/log password. Rate limit login theo account/IP và audit failures; admin không có public registration.

## Gói và quota

Plan version gồm giá/currency/duration, features, analysis quota, active-job limit, validity và upgrade/refund policies. Admin config validate; new order pin version/price, không rewrite purchased order. Entitlement pin tại admission.

Quota ledger append-only: reserve → consume/release/refund theo terminal reason. Unique reservation per job, admission lock/atomic update tránh quota races. Client retry không tạo reservation mới. Fairness theo account, global capacity/backpressure và per-account active jobs.

### Settlement beta đề xuất

Đây là P-13/O-08, cần chốt trước mở gói cho khách. Một reservation có một settlement consume hoặc release; refund sau consume là bút toán bù riêng có reason/evidence, không sửa lịch sử. Settlement có unique reservation_id và conditional transition RESERVED → CONSUMED hoặc RELEASED để không thể ghi cả consume lẫn release. Refund/adjustment có idempotency key riêng và kiểm tra số lượt có thể bù còn lại, không hoàn quá số đã consume.

| Tình huống | Job/effect | Quota |
|---|---|---|
| Admission hợp lệ | QUEUED; pin entitlement, bucket và quota-policy version | Reserve 1 lượt |
| Invalid request hoặc không đủ quyền/quota | Reject trước insert job/reservation | Không trừ |
| SUCCEEDED, report đạt contract | Report + terminal settlement commit nguyên tử | Consume 1 lượt |
| Provider/system error, dữ liệu không đủ hoặc snapshot quá deadline | FAILED khi hết retry/deadline | Release |
| User cancel trước compute_started_at | CANCELLED | Release |
| User cancel sau compute_started_at | CANCELLED, fence completion; không bảo đảm có report | Consume 1 lượt, phải công bố rõ trước khách gửi job |
| Retry nội bộ, redelivery, duplicate request/cancel | Giữ job/reservation cũ | Không reserve/settle thêm |
| Refund do admin sau consume | Giữ terminal history, thêm adjustment có actor/reason/evidence | Bút toán bù, không release lại reservation |

Report có section UNAVAILABLE không tự động là failure: nếu vẫn đáp ứng required sections/coverage trong report policy đã pin thì có thể SUCCEEDED và consume; thiếu required sections thì FAILED và release. Policy phải phân biệt kết quả hợp lệ AVOID/WATCH với lỗi kỹ thuật: quyết định không mua không phải lý do tự động hoàn quota. Required sections/minimum coverage là một phần field inventory và gate report v1, không để worker tự quyết định.

Job đã admission được chạy hết dù subscription hết hạn hoặc đổi gói trong lúc chờ; giữ entitlement/bucket đã pin. Release quay về bucket gốc; nếu bucket đã hết hạn thì chỉ sửa số dư lịch sử, không cấp lượt khả dụng cho kỳ mới. Refund/extension sang kỳ mới cần adjustment explicit. Policy này không bỏ qua account lock/security revocation; trường hợp đó dừng job với reason riêng và release nếu chưa có settlement.

`active_jobs` gồm QUEUED/WAITING_DATA/RUNNING/RETRY_WAIT; giảm đúng một lần khi terminal. Dependency deadline, execution timeout và max attempts phải hữu hạn, được validate trong config trước mở beta. Cancel/completion race và reconciler theo [lifecycle mục 03](03-architecture.md). Response status/history trả quota effect và reason; tài liệu API phải nêu cancel sau compute có thể tốn lượt.

Nghiệm thu: cancel trước/sau compute, cancel-complete race, timeout/expired lease, job qua kỳ quota, degraded report, admin refund lặp và terminal job thiếu settlement sau crash. Mỗi case kiểm tra cả ledger, số dư bucket và active-job count.

PROPOSED prepaid renewal; policies cụ thể OPEN. Không auto-debit mặc định. Adjustment/extension có actor/reason/effective dates và audit.

## SePay

Order CREATED/PENDING/PAID/EXPIRED/CANCELLED; tiền VND dạng integer, mã thanh toán riêng. API trả amount, expires_at, reference và QR/payment instructions, không FE quyết định paid.

Webhook auth theo cơ chế SePay đã cấu hình và contract test. Persist event → deduplicate provider transaction → verify account/inbound/amount/reference → match order → commit payment/subscription/entitlement atomically. Duplicate trả acknowledgement phù hợp. Event auth fail không được ghi paid.

Late payment, missing reference, over/underpayment, multi-transfer, payment order conflict đi vào reconciliation; không tự cấp gói nhầm. Replay/retry idempotent. Refund là financial workflow có statuses, không chỉ đổi subscription flag; manual action có evidence/audit. Raw events lưu hạn chế, che sensitive fields và retention config.

Không dựa browser return URL để cấp quyền. Mất webhook dùng reconciliation theo provider capability được xác minh; không tự giả định endpoint/API limit. Sandbox, duplicate/fault tests và live payment nhỏ là các evidence riêng.

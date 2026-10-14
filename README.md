# Quantik server

Backend SaaS phân tích chứng khoán Việt Nam. Thiết kế tại [docs/saas-plan](docs/saas-plan/README.md).

## Chạy local (PowerShell, từ repository root)

```powershell
& "$HOME\.venv\Scripts\Activate.ps1"
python -m pip install -e './backend[dev]'
# Tạo .env từ .env.example nếu chưa có; không ghi đè secrets hiện có.
quantik generate-keys
quantik migrate
# Đặt QUANTIK_ADMIN_PASSWORD trong .env, tối thiểu 14 ký tự.
quantik seed-admin
python -m uvicorn service.api:create_app --factory --host 127.0.0.1 --port 8000
```

OpenAPI: http://localhost:8000/docs. SQLite dùng local; production yêu cầu PostgreSQL và HTTPS.
Migration chạy explicit trước API, không tự chạy khi startup. Seed lặp không reset mật khẩu.
Private RSA key nằm trong runtime (Git ignore); cần giới hạn ACL khi đưa lên VPS.

## Admin auth

`POST /api/v1/admin/auth/login` nhận username/password, trả bearer access JWT và refresh cookie HttpOnly.
`GET /api/v1/admin/me` nhận Authorization Bearer. Refresh/logout yêu cầu header Origin khớp
QUANTIK_PUBLIC_ORIGIN. Refresh cookie scope /api/v1/admin/auth, SameSite Strict, Secure ở production.
Logout/refresh reuse/account disabled vô hiệu hóa access token qua kiểm tra session trong DB.
Login throttles theo username và peer IP, chặn 15 phút sau 5 lần sai. Không tin X-Forwarded-For
trực tiếp; cấu hình trusted proxy trước deploy. Refresh family có expiry cố định 7 ngày, access 15 phút.

## Kiểm tra

```powershell
python -m pytest backend/tests/test_admin_auth.py backend/tests/test_google_auth.py backend/tests/test_jobs.py -q
```

Test này dùng DB và RSA keys tạm, kiểm tra seed, login, JWT claims/algorithm, CSRF Origin,
refresh rotation/reuse, logout và rate limiting. Chưa thay bằng chứng PostgreSQL concurrency hoặc Google live.
Không có tài nguyên Docker được tạo bởi test này.

## Google login

Trong Google Cloud OAuth client, thêm redirect URI đúng với QUANTIK_PUBLIC_ORIGIN:
`http://localhost:8000/api/v1/auth/google/callback` cho local; dùng HTTPS domain thật khi deploy.
Tham khảo [Google OIDC server flow](https://developers.google.com/identity/openid-connect/openid-connect).

1. Browser gọi GET /api/v1/auth/google/start, giữ cookie và mở authorization_url trả về.
2. Google callback kiểm tra browser-bound state, PKCE và ID token signature/issuer/audience/expiry/nonce.
3. Callback trả exchange_code trong JSON. FE gửi POST /api/v1/auth/exchange với code, cookie và Origin.
4. BE trả user access JWT và refresh cookie. GET /api/v1/me xác định account theo Google sub.

Exchange dùng một lần, hết hạn sau 60 giây; không đưa bearer token vào URL. FE chưa được triển khai;
callback JSON là API contract hiện tại, cần tích hợp màn hình callback của FE sau. Google flow kiểm tra
bằng token ký thật với provider transport/JWKS fixture; đăng nhập Google live cần thao tác consent của người dùng.

## Analysis jobs / quota

POST /api/v1/analyses yêu cầu user bearer JWT, Idempotency-Key và body symbol/target_session;
sizing tùy chọn (account_size_vnd, max_position_risk_ratio). GET /analyses hỗ trợ cursor;
GET /analyses/{id}, POST /analyses/{id}/cancel, GET /reports/{id} chỉ cho owner.
GET /me/usage trả quota buckets. Admission reserve một lượt, commit job/outbox cùng transaction.

Policy beta-1: success consume; lỗi kỹ thuật/data release; cancel trước compute release,
cancel sau compute consume. Job qua kỳ gói giữ bucket gốc; release không tăng quota kỳ mới.
Response có quota_effect, reason và compute_started_at. Cancellation fence token, completion muộn bị từ chối.
Report commit yêu cầu đầy đủ 16 nhóm sections và minimum availability; nested field parity sẽ khóa khi
quant assembly hoàn tất. Chính sách hiện tại chưa được bật cho khách trả phí.

Jobs core đã kiểm tra request lặp/conflict, ownership, active limit, cancel, retry lease, version pin,
incomplete report rejection, settlement và kỳ quota gốc. Chưa có numerical worker hoặc RabbitMQ dispatcher;
jobs tạo local sẽ nằm trong queue cho đến khi thành phần đó hoàn thành. Quota chưa tự cấp trước billing/admin grants.
PostgreSQL concurrency/load vẫn cần evidence riêng trước mở beta.

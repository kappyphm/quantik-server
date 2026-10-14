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

## Market data / Vnstock v5

```powershell
# Gói v5 nằm ở kho riêng; PyPI chỉ có v4. Không cài vnai/onboarding scripts.
python -m pip install --no-deps --index-url https://vnstocks.com/api/packages vnstock==5.0.1
python -m pip install -e './backend[market,quant,dev]' --extra-index-url https://vnstocks.com/api/packages
quantik migrate
# Chạy khi slot thiết bị Vnstock đã được cấp. target-session phải là phiên đã hoàn tất.
quantik ingest-market --target-session 2026-10-14
# Chỉ fetch thử một mã + VNINDEX; không publish manifest toàn sàn nếu thiếu coverage.
quantik ingest-market --target-session 2026-10-14 --symbol FPT
python -m pytest backend/tests/test_market_data.py -q
```

Adapter pin v5.0.1, equity nguồn VCI và index nguồn KBS; theo
[contract Market v5](https://www.vnstocks.com/docs/vnstock/thi-truong-market), equity giá nghìn VND
được chuyển sang VND, index giữ points. Không suy đơn vị từ độ lớn giá. Daily completed của phiên hiện tại
chỉ nhận từ 16:00 giờ Việt Nam; stale/duplicate/invalid/capped histories bị từ chối.

Mỗi lịch sử/universe/manifest lưu revision bất biến với checksum; correction tạo revision mới.
Manifest ready yêu cầu VNINDEX đúng phiên và ít nhất 95% universe có history hợp lệ (mặc định beta-1).
Ingestion lưu từng history sau mỗi mã để tiếp tục sau lỗi; không publish partial dưới tên hoàn chỉnh.
Corporate-action adjustment chưa có evidence live nên metadata cảnh báo rõ, chưa dùng để publish model.
DB provider gate áp dụng rolling windows minute/hour/day/31-day từ limits tài khoản đã verify;
cooldown chia sẻ giữa workers. PostgreSQL contention/provider live cần kiểm chứng riêng.

Provider v5 lưu credential mã hóa theo cơ chế native ngoài repo; không ghi AI skills/instructions vào workspace.
Lần thử login thật trả VNSTOCK_AUTH_DEVICE_LIMIT. Theo người dùng, integration live đang chờ cấp slot,
không tự retry login trong lúc chờ. Test normalization/manifest/limiter dùng fixtures, không thay live evidence.

## Quant extraction / numerical baseline

```powershell
python backend/tools/extract_quant.py
python -m pytest backend/tests/test_quant_parity.py -q
# Smoke đầy đủ ARIMA/GARCH/HMM/Monte Carlo; input synthetic, không phải report thị trường.
python backend/tools/smoke_quant.py
```

22 classes tách từ prototype vào service.quant.engines; prototype gốc giữ nguyên.
Provider/file mapping, training-on-request, request-universe scoring và screener bonuses được loại khỏi
engines runtime. LightGBM features giữ lại để phục vụ training/inference riêng. AST inventory tại
docs/saas-plan/quant-field-inventory.json là công cụ đối chiếu literal keys, chưa thay report field mapping cuối cùng.

Config frozen và ContextVar cô lập tham số giữa requests. Baseline lấy evaluated_at/index inputs explicit,
tính đủ statistical/risk components; agreement, costs, stops, sizing và final score/action sẽ được ghép sau
snapshot/model đã pin, tránh tính stops hai lần. Không dùng output baseline để giả một completed report.
QUANTIK_NUMERICAL_THREADS mặc định 1 (1–8); backend đặt BLAS/OMP/NumExpr budgets trước scientific imports.

Compatibility ghi riêng: SciPy 1.18 string-name kstest fast path không nhận loc/scale; dùng explicit
norm.cdf callable với cùng fitted normal CDF. Numerical parity test đối chiếu 22 class AST (sau loại hàm
đã chủ động bỏ và đổi callable này), kiểm tra distribution/statistics/Monte Carlo/cost/stops và config restoration.
Full baseline smoke synthetic 252 bars đã chạy khoảng 18.44 giây, ARIMA/GARCH/HMM/simulation không có lỗi.
Chưa có scientific validation trên dữ liệu live, bootstrap model hoặc customer report hoàn chỉnh.

## Weekly model lifecycle

```powershell
quantik train-model --manifest-id <ready-completed-manifest-id>
quantik publish-model --model-id <candidate-id>
python -m pytest backend/tests/test_model_training.py -q
```

Training chạy riêng với immutable completed manifest và sector membership rõ. Mature labels theo horizon,
holdout 40 sessions, purge + embargo mỗi cái bằng horizon, tối thiểu 400 train rows/5 symbols và
150 validation rows. Beta policy yêu cầu ít nhất 20 ngày rank IC hợp lệ, rank IC ≥0.02 và RMSE absolute/alpha
không tệ hơn zero-return baseline. Tham số model giữ n_estimators=180 và công thức feature từ prototype.
Incumbent được so trên cùng holdout khi có; nếu training cutoff incumbent overlap holdout thì metrics đó
chỉ diagnostic, không được dùng làm evidence out-of-sample. Refit trên mature rows sau khi khóa metrics.

Artifacts dùng LightGBM text (không pickle), config/checksum/manifest/cutoff/evidence lưu DB shared.
Publication kiểm tra checksum, validation và live-data evidence, cập nhật pointer atomic; candidate lỗi
giữ model đang publish. Fixture mặc định có provenance FIXTURE; provider fetch ghi LIVE_UNVERIFIED.
Chưa có đường tự đổi UNVERIFIED thành VERIFIED, nên chưa publish model từ dữ liệu chưa kiểm định.
Test temporal training/inference/revisions và fixture publication rejection đã đạt; lịch weekly chưa được nối scheduler.

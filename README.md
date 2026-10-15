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
incomplete report rejection, settlement và kỳ quota gốc. Worker/dispatcher có lệnh chạy bên dưới;
job chưa đủ validated data/model sẽ chờ dependency. Quota chưa tự cấp trước billing/admin grants.
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

## Market snapshots / report assembly

```powershell
quantik build-snapshot --manifest-id <ready-completed-manifest-id>
python -m pytest backend/tests/test_snapshots.py -q
```

Snapshot builder cache baseline theo history/index/config/engine/evaluation-day, có lease/fencing cho cả
component và snapshot. Tính sector context, LightGBM inference/rank và factor reference trên eligible universe;
không lấy tập mã user đang request. Eligibility cần history đúng phiên, sector và baseline hợp lệ; publish
yêu cầu ít nhất 5 mã và 80% universe (beta-1), exclusions rõ. Model ≥14 ngày warning, ≥28 ngày unavailable.
Snapshot payload checksum, atomic pointer và manifest revision counter ngăn bản cũ ghi đè correction mới.

Customer finalization dùng reference đã pin, tính agreement/cost trước stops và sizing, score không có screener
bonus. Zero-IQR/minimum sample xử lý z=0 với lý do rõ. Sizing reference 500 triệu hoặc personalized;
LightGBM unavailable luôn chặn executable BUY_NOW. Report có 16 sections, giữ nested outputs/series và
commentary gốc; mapping tại [08-report-field-mapping.md](docs/saas-plan/08-report-field-mapping.md).
OpenAPI expose ReportResult. Raw MC paths được bỏ, quantile trajectories và summaries giữ nguyên.

Test fixture đã đạt single/batch score/action invariance, baseline cache reuse, full report envelope/commentary,
correction publication order và failed build giữ latest. Baseline statistical model fitting đã smoke riêng;
test snapshot thay thế phần fitting bằng fixture để kiểm tra orchestration. Chưa có
live full-universe benchmark hoặc model/data scientific gate đạt.

## Worker / dispatcher / dependency runtime

```powershell
python -m pip install -e './backend[broker]'
quantik migrate
# Các process riêng, dùng chung DB và QUANTIK_AMQP_URL:
quantik dispatcher
quantik worker
quantik dependencies
quantik reconciler
# Local development không cần broker, chạy một batch rồi dừng:
quantik worker --transport local --once
quantik reconciler --once
# Chỉ sau restore/mất dữ liệu broker; không chạy lặp với backlog đang hoạt động:
quantik recover-delivery
```

RabbitMQ durable queues/exchanges, persistent messages, publisher confirms/mandatory routing, prefetch 1
và dead-letter queue. Dispatcher đánh dấu outbox sau confirm; crash/retry có thể gửi lại, job claim/inbox
chặn duplicate effects. Consumer ack sau report/settlement và inbox commit; native compute chạy ở bounded
executor để AMQP heartbeats không bị chặn. Một compute slot mỗi worker process, không nhả CPU slot khi
user cancel cho tới khi attempt thực sự dừng. Reconciler recover expired leases và thiếu terminal settlement.

Worker pin versions/evaluated_at trước compute; retries giữ snapshot/model/config. Snapshot member/reference
được đọc qua JSON projection và kiểm checksum riêng (PostgreSQL dùng JSONB), không decode toàn universe
vào RAM mỗi job. Dispatcher/control runtime không import NumPy/Pandas/LightGBM khi idle. Attempt heartbeat
20 giây, lease 120 giây, execution timeout beta mặc định 900 giây, dependency deadline 24 giờ.

WAITING_DATA tạo shared dependency request theo target/config, không theo account/symbol. Source/model
thay đổi rearm request; dirty flag giữ update đến trong lúc builder đang chạy. Không tự gọi provider từ
customer worker. Job chỉ wake khi inputs thật sự đủ policy; account bị khóa chặn compute/completion và
release lượt. Request hết active waiters được đóng để tránh retry nền mãi.

Production từ chối guest broker, fixture/degraded flags và local transport. Mặc định development cũng
yêu cầu LIVE_VALIDATED data + published model; flags nới chỉ dành cho fixture validation có chủ đích.
Settings validation không in raw input/secrets. Không tự chuyển sang local transport khi RabbitMQ lỗi.

Focused fixture E2E đã đạt API admission → confirmed outbox → worker → report → quota; confirm loss/redelivery
không nhân report/lượt; strict inputs wait không đánh dấu compute; shared dependency, cancel và account
revocation sau compute xử lý đúng. Đây chưa phải bằng chứng broker/PostgreSQL thật, load hoặc live scientific gate.

## Screening

```powershell
quantik migrate
# Chạy khi provider đã được cấp slot/xác minh; AM chỉ lấy observations trong lunch window.
quantik ingest-market --target-session 2026-10-15 --input-mode PROVISIONAL_DAILY
quantik screen-market --target-session 2026-10-15 --slot AM
# PM trigger 15:00, chờ completed data từ 16:00:
quantik ingest-market --target-session 2026-10-15
quantik screen-market --target-session 2026-10-15 --slot PM
python -m pytest backend/tests/test_screening.py -q
```

21 tiêu chí/narrative/VNINDEX context giữ từ prototype, thêm tri-state availability và requirements theo
từng criterion. Mọi universe member có all-results, kể cả short history/liquidity rejection; group rankings,
overall Top 70 và coverage explicit. AM raw volume với prior-completed baseline, không projection.
Policy/window/readiness/deadline tại [09-screening-policy.md](docs/saas-plan/09-screening-policy.md).

API user: GET /instruments; GET /screenings/latest?slot=ALL|AM|PM;
GET /screenings/{run_id}/results?group=ALL|OVERALL|STRONG|ACCUMULATION|DISTRIBUTION&limit=50&cursor=...
ALL cursor theo symbol; ranking cursor theo rank. OpenAPI có ScreeningPage/ScreeningItem/ScreeningGroup/CriterionRecord.
Latest trả published run + latest attempt + stale reasons; failed run không thay published pointer.
Universe và data fixture không được phục vụ qua API mặc định; source validation vẫn chờ live credentials.

Focused checks đạt boolean parity completed inputs với prototype, missing/short/index availability,
provisional denominator, stable ranks, all-results, PM/AM publication, deadline giữ latest và authenticated
API pagination. Chưa có calendar/scheduler automation hoặc live full-universe acceptance.

## Thanh toán prepaid

Migration 0009 thêm plan versions, orders, bank payments, subscriptions và nối quota hiện có.
Admin tạo draft plan qua POST /api/v1/admin/plans, rồi bật availability. Customer tạo đơn qua
POST /api/v1/orders với Idempotency-Key, nhận số tiền/nội dung/QR; GET /orders/{id} xem trạng thái.
POST /api/v1/webhooks/sepay yêu cầu `Authorization: Apikey ...`, khớp giao dịch và cấp gói/quota nguyên tử.
Renewal xếp kỳ tiếp theo; không auto-debit. Có API đối soát cho admin và kiểm tra entitlement cho user.

Điền SEPAY_BANK_CODE, SEPAY_ACCOUNT_NUMBER, SEPAY_ACCOUNT_NAME, SEPAY_WEBHOOK_API_KEY trong .env;
SEPAY_TRANSFER_PREFIX nếu ngân hàng yêu cầu TKP/VA. QUANTIK_PAYMENTS_ENABLED mặc định false,
chỉ bật sau khi kiểm tra tài khoản, QR/nội dung và webhook. Policy và giới hạn xác minh tại
[10-billing-policy.md](docs/saas-plan/10-billing-policy.md).

Focused check: `python -m pytest backend/tests/test_billing.py -q` (shared venv).
Chưa xác minh SePay sandbox/live, PostgreSQL concurrency hoặc hoàn tiền ngân hàng.

## Quản trị vận hành

Migration 0010 thêm hoàn lượt có Idempotency-Key/reason/evidence, append ledger và audit, không sửa
settlement gốc. POST /api/v1/admin/jobs/{id}/quota-refund hoàn một lượt vào bucket cũ; bucket hết hạn
không được chuyển thành quota kỳ mới. Đây là hoàn lượt phân tích; hoàn tiền ngân hàng chưa triển khai.

POST /api/v1/admin/users/{id}/state khóa/mở customer có reason; khóa revoke sessions ngay, reconciler
fence/release jobs đang chờ/chạy. Có bounded GET views users/jobs/payments/subscriptions/audit/models/data-health
dưới /api/v1/admin. Chi tiết: [11-admin-operations.md](docs/saas-plan/11-admin-operations.md).
Focused checks: test_admin_operations.py và test_jobs.py đã đạt; chưa có PG race/load acceptance.

## Lịch tự động và cấu hình có phiên bản

Migration0011. Chạy `quantik seed-operations` sau migrate/seed-admin để khởi tạo policy và lịch giao dịch2026
đã đối chiếu thông báo HOSE/HNX. `quantik scheduler` lập lịch bền vững;
`quantik schedule-worker --kind SCREEN` (hoặc SNAPSHOT/TRAIN) thực thi tương ứng. Các lệnh hỗ trợ `--once`.
Provider INGEST cần cả config ingestion_enabled và QUANTIK_SCHEDULED_PROVIDER_ENABLED; mặc định đều tắt
trong lúc chờ Vnstock. Không tự đăng nhập lại provider chỉ vì scheduler chạy.

Admin có /api/v1/admin/configs/{kind} để tạo version/activate/rollback bằng expected_version + reason và
/api/v1/admin/scheduled-tasks để xem task/retry/reason. Có unique slots, global lanes, leases/heartbeat,
deadline, restart recovery và source/model gates. Screening freshness dùng calendar khi có coverage.
Lịch2027 không được suy đoán; cần import bản có nguồn. Xem [12-scheduled-operations.md](docs/saas-plan/12-scheduled-operations.md)
cho đầy đủ commands, timings, config và giới hạn kiểm chứng. Chưa có supervision/acceptance trên VPS.

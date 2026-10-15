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

## Thành phần pure quant baseline

Tách 22 classes bằng AST/source slices, không execute/import prototype và không sửa snapshot gốc.
Giữ numerical methods; bỏ SectorEngine provider/watchlist loaders, LightGBM fit_predict theo request,
và AdaptiveScorer request-batch/absolute scoring có screener bonuses. Những phương pháp scoring/training mới
phải được triển khai riêng theo market snapshot/published model contracts.

Config frozen + ContextVar, explicit evaluated_at cho data audit, process numerical thread budget mặc định 1.
Inventory ghi normalized-source SHA và literal keys theo engine/method (bao gồm internal keys); chưa đủ để
khóa nested report schema. Full baseline runner giữ statistical components trước agreement/cost/plan.

Smoke phát hiện SciPy 1.18 string-name kstest tối ưu thành ndtr không nhận loc/scale. Extraction đổi đúng
một lời gọi sang stats.norm.cdf callable, giữ cùng CDF/params, ghi compatibility change riêng.
Parity AST của 22 classes sau transformations và numerical parity critical methods đã đạt; config restoration đạt.
Full baseline synthetic 252 bars: 25 output groups, 18.44 giây, component_errors rỗng;
ARIMA/GARCH/HMM/simulation outputs đầy đủ và không có error. Đây không thay live scientific/model evidence.

## Thành phần model lifecycle

Train panel dùng features gốc, toàn eligible universe có sector/index/same-session; mature labels explicit.
Temporal holdout 40 sessions, purge/embargo bằng horizon; min 400 train rows, 5 symbols, 150 validation rows.
Agent chọn temporal-beta-1: ≥20 valid rank days, rank IC≥0.02, absolute/alpha RMSE≤zero-return baseline.
Đây là ngưỡng beta cần evidence thực nghiệm trước mở khách, không tuyên bố model sinh lợi.
So incumbent cùng holdout; overlap training cutoff được ghi diagnostic-only. Refit chỉ sau khóa metrics.

ModelVersion/PublishedModel lưu text boosters, config, manifest/checksum/cutoff/evidence. Publication atomic,
checksum gate, validation gate, live-data gate, không ghi đè model cutoff mới bằng model cũ.
Model fixture không publish; provenance phân biệt FIXTURE/LIVE_UNVERIFIED/LIVE_VALIDATED.
Test synthetic 6 symbols/400 bars đạt temporal separation, deterministic inference/rank ordering,
stale-symbol exclusion, artifact idempotency và fixture promotion rejection. Không có bootstrap live model.

## Thành phần market snapshot và report assembly

Component cache và snapshot builder có unique dependency keys, lease/token/fencing; input versions pin theo
manifest/model/config/engine/policy/evaluation-day. Baseline eligibility trước global inference/rank/sector factors.
Agent chọn minimum 5 eligible members + 80% universe; zero-IQR hoặc n<3 đóng góp z=0, reason/version explicit.
Model warn 14 ngày, disable 28 ngày; build model unavailable không giả prediction/rank.

Monotonic manifest revision allocator và atomic published snapshot pointer giữ correction mới khi builder cũ retry.
Failed run giữ latest. Snapshot cache/read có checksum. Per-request score đọc immutable reference,
không dùng request universe hoặc screening labels. Sizing personalized không thay reference normalization.

Final graph agreement → costs → stops → sizing → score → action. LightGBM unavailable chặn BUY_NOW/executable.
Commentary được extract từ prototype; mappings giữ nested outputs/series, raw MC paths thay bằng quantile
trajectories, meta-label/tracking/Kelly disabled rõ. ReportResult exposed OpenAPI; mapping tại mục 08.

Test fixtures đạt snapshot cache reuse, single/batch invariance, report 16 sections/commentary,
old-builder/correction ordering và failure không đổi pointer. Migrations đến 0006 đã được tạo;
PostgreSQL concurrency, live full reports và scientific bootstrap vẫn chưa nghiệm thu.

## Thành phần execution/delivery runtime

Migration 0007: inbox, job_attempts, evaluated_at pin, dependency requests/waiters và dirty latch;
PostgreSQL snapshot JSONB để projection member/reference. Snapshot policy beta-2 có per-member/reference checksum.
Worker dùng immutable versions và evaluation clock; heartbeat 20s, lease 120s, execution timeout 900s.
Account lock kiểm tra lại ở begin_compute/completion; failed security revocation release reservation.
Attempt stopped_at chỉ ghi khi execution thực sự kết thúc; account slot/cancel không mở thêm compute slot.

RabbitMQ adapter pin Pika 1.3.2, publisher confirm + mandatory + durable/persistent, dead letters và prefetch 1.
Dispatcher batch round-robin theo account; outbox chỉ mark sau confirm. Consumer bounded executor giữ AMQP I/O
thread alive, ack sau effects/inbox; Job claim/fencing bảo vệ at-least-once deliveries. Local transport explicit
cho dev, cấm production. recover-delivery explicit sau restore, không re-publish backlog theo timer.

Shared dependency keyed target/config, không theo account/symbol; ready-source/model signals rearm, dirty flag
không mất update trong lúc build. Reconciler chỉ wake inputs pass policy và đóng requests không còn active jobs.
Live data/model strict mặc định; production cấm fixture/degraded flags. Vnstock login vẫn chờ theo người dùng.

Fixture E2E bổ sung vào snapshot check đã đạt: API→outbox→worker→16-section report→quota, lost confirm rồi
redelivery chỉ một report/consume; strict-data wait chưa compute; wake, shared dependencies, cancel release,
account disabled sau compute chặn completion/release đúng. CLI local worker/reconciler --once chạy thành công;
control runtime xác nhận không nạp NumPy/Pandas/LightGBM. Chưa chạy broker/container, PG concurrency hay VPS.

## Thành phần screening

Extract pure Feature/Screening/Assessment/MarketAnalyzer + regime adjustment từ crawl_data.py; không provider,
news hay exporter. 21 criteria giữ threshold/công thức khi inputs đủ, tri-state + minimum bars theo mục 09.
RS không ffill ngày index thiếu; volume AM raw/prior completed20; projected volume disabled. Fixed denominator,
coverage/group/overall eligibility, stable symbol tie-break và all-results cho rejected/short symbols.

Run unique target/slot, lease/fencing, readiness>=95% histories + index50bars; AM cutoff/lunch, PM completed16:00,
deadlines 12:55/17:30. Atomic publication giữ latest khi failed; ALL/AM/PM pointers riêng, published run immutable.
API user/instruments/latest/results có cursor và typed schemas. Default source gate chặn fixtures;
universe provenance cũng được đưa vào model live-publication evidence.

Test đạt completed boolean parity, missing/partial availability, raw morning volume, tie/rank ordering,
PM+AM publication/wait/deadline, giữ rejected/all-results và API auth/pagination. Ingestion nhận short
histories để screening không áp min120 lên tất cả tiêu chí; quant/model giữ min riêng120. Không gọi lại Vnstock,
không tạo Docker. Calendar/scheduler, billing/admin và deployment tiếp tục ở các thành phần sau.

## Thành phần prepaid billing

Migration 0009: immutable plan versions, idempotent pinned orders/QR, normalized bank payments,
subscriptions và quota buckets. API customer/admin/Webhook API-key; dedup SePay ID + bank reference hash.
Exact amount/account/inbound/reference/window validation, atomic grant, queued renewal, explicit audited
manual reconciliation và order cancellation. Source contract/policy tại mục 10; chưa live/sandbox bank gate.

Focused check đã phát hiện initial SQLite SAVEPOINT có thể giữ event sau outer rollback; thay bằng native
INSERT ON CONFLICT để webhook/payment/subscription/quota cùng commit/rollback. Check JWT/ownership,
duplicates, mismatches, manual resolution, renewable periods, actual job admission và rollback đã đạt.
Recheck sau account/order lock bảo vệ cancellation/disabled state races; PG concurrency vẫn cần acceptance.
Bank config đã lưu .env, payments mặc định disabled, đang chờ làm rõ TPBank VA prefix. Không gọi Vnstock/Docker.

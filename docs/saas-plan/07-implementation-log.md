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

## Thành phần admin operations

Migration 0010 thêm quota_refunds, one-per-reservation + actor/idempotency key. Explicit admin refund
append compensating ledger, giữ original settlement và credit bucket gốc, không chuyển lượt hết hạn sang kỳ
mới. Customer job response có refund ID. Account disable audit before/after và revoke sessions; reconciler
fence/release pending/running jobs với ACCOUNT_DISABLED, tránh account→job lock inversion ở action disable.

Bounded admin users/jobs/payments/subscriptions/audit/models/data-health views không đọc artifacts/histories.
Focused admin API check và existing job lifecycle đã đạt; có refund replay/conflict, rollback, expired bucket,
security stop/stale token, re-enable không revive session, read routes và audit. PG concurrency chưa xác minh.
General config rollback, bank refunds, scheduler và deployment vẫn còn. Policy chi tiết tại mục 11.

## Thành phần scheduled operations

Migration0011 thêm immutable/CAS operational configs và calendar, durable unique tasks/cursor, global lanes.
Admin config activation/rollback/audit và task history API. Reviewed calendar2026 có248 phiên theo HOSE/HNX
và New Year update; không ngoại suy2027. Scheduler giữ cursor/catch-up, daily slots/deadlines, Sunday training
theo last actual session, lease/heartbeat/fencing. Publication/candidate/ingestion effects có operation guard.

Provider auto-ingestion yêu cầu cả operation config và env gate; cả hai vẫn tắt. Không login lại Vnstock.
Ingestion resume tránh refetch các mã đã xong và giữ retry-after; manifest chỉ đọc projected headers giảm RAM.
Calendar-aware latest freshness xét expected slot thay vì báo stale chỉ vì cuối tuần/lễ. Heavy imports lazy.

Focused scheduler/resume/readiness/admin config/screening checks đạt; local migrate/seed/scheduler --once
và provider-disabled worker --once chạy thành công. Lịch đã materialize19 tasks,16 quá hạn được ghi nhận rõ
ở lần bootstrap, không có provider calls. Chưa xác minh PG races, scheduled live run hoặc deployment.
Chi tiết tại mục12; observability/deployment/backup/load và live integration gates vẫn còn.

## PostgreSQL/RabbitMQ thật và restore local

Thêm opt-in runner dùng existing image IDs, hai container tmpfs1CPU/512MiB riêng, không pull/build/volumes.
Repo quantik-backend cũ có Docker resources khác nên giữ nguyên. Runner cleanup theo ownership label cả khi
test fail; readiness chạy Rabbit CLI đúng service user, tránh cookie root/eacces ở startup.

5 checks trên PostgreSQL/RabbitMQ thật đạt: concurrent admission/idempotency/cancel-complete/payment/refund,
config CAS/scheduler lanes, Rabbit confirm/duplicate/ack/DLX/app restart, JSONB numeric roundtrip, và fixture
API→outbox→real Rabbit→AnalysisWorker→16-section report/quota. pg_dump/restore DB riêng khớp toàn bộ bảng
cả hai schema. Baseline/model vẫn fixture/degraded có chủ đích; không thay live/scientific/VPS/load acceptance.

Check thật phát hiện SNAPSHOT_CHECKSUM_MISMATCH sau JSONB lưu số theo representation khác. Recipebeta3
dùng streamed semantic numeric hash cho snapshot dependency/root/member/reference, không làm tròn metrics;
giữ legacy hash cho beta2 reader và không đổi history/model identities. One-ULP/type/collision checks đạt.
Upgrade builder/worker coordinated sau drain, không mixed-recipe rolling deployment. Chi tiết/evidence tại13.
Final inspection không còn check containers, image/volume inventory giữ nguyên. Provider vẫn không gọi lại.

## Private observability và process heartbeat

Migration0012, admin operations-health và riêng Bearer scrape token (blank disabled); .env local đã có
token ngẫu nhiên, không ghi vào Git/log. HTTP histogram/counter giới hạn route templates/status classes;
structured request/runtime logs không chứa path/query/body/headers/raw exception. Heartbeat20s, alive90s,
retention24h, RUNNING/ERROR/STOPPED; control processes vẫn idle không import numerical libraries.

Aggregates: queue/outbox age, leases, quota counter/reservation/refund consistency, recent1000 attempt
runtime/wait percentiles, provider windows/cooldown, calendar/expected screening, published model/data,
payment reconciliation, disk và passive Rabbit counts. Collector cache10s; broker probe chờ tối đa4s,
chỉ một probe thread/connection khi broker RPC bị treo. Không dùng DB gauges SUM across API replicas.

Ba observability checks đạt, cùng admin/refund critical check; local migrate/scheduler/reconciler --once
đạt. API thật ready0012, metrics unauthorized401/authorized200 (63 samples), admin login/health200,
quota anomalies0 và broker_up=false đúng trạng thái broker chưa bật. Không chạy/build Docker ở bước này.
Prometheus scrape/rule templates đã có nhưng chưa nghiệm thu bằng promtool hoặc notification receiver.
Host/container metrics, exporter và alert delivery còn thuộc deployment gate; xem14.

## Backup bundles và restore target riêng

Thêm CLI backup/verify-backup/restore-backup: SQLite online backup + integrity/FK check, PostgreSQL custom dump
với exported snapshot, private bundle manifest/SHA256/revision. Restore từ chối current/existing target;
PG import một transaction, không clean/drop database; credentials qua env, không URL args/manifest.
Hỗ trợ native tools hoặc explicit local DB container sau xác minh published port.

SQLite check và PostgreSQL17 check thật đạt2 tests/7.06s (runner14.16s); so nội dung tất cả model tables,
source changes sau backup không đổi restored image. Runner --backup-only chỉ một tmpfs container1CPU/512MiB,
không Rabbit/no volumes/no build/pull, đã dọn owned container và credential file; volume inventory giữ nguyên.
CLI thật trên DB local0012 tạo/verify/restore bundle và SQL export khớp124 statements; dọn restore target,
giữ một bundle local có mục đích. Chưa có offsite/backup schedule/native Windows tools/VPS disaster drill.
Runbook gồm keys/ACL exclusions, job redelivery và post-backup payment reconciliation; xem15.

## Admin SePay/Vnstock credentials

Theo yêu cầu bổ sung, migration0013 và GET/PATCH admin/integrations/{sepay|vnstock}. Typed PATCH với
expected_revision/reason, atomic CAS chống ghi đè (concurrent operators một200/một409), audit metadata
không secret values. Payload mã hóa Fernet; master key riêng runtime/secret mount; local đã tạo với ACL
chỉ tài khoản vận hành/SYSTEM. GET chỉ có key-configured flags và các bank fields cần quản trị.

SePay hot-read trên plans/create-order/webhook; orders cũ pin account/content, config mới không sửa đơn.
Enable chỉ khi đủ config/VA; xóa webhook secret đang bật phải đồng thời disable. Vnstock ingest đọc key
từ DB khi bắt đầu run, không tự login lúc save. Clear secret không fallback key env cũ; stored record ưu tiên
env và không sửa .env. Wrong/missing master key hoặc tampered ciphertext fail503, không âm thầm fallback.

Focused integration/auth/redaction/encryption/CAS/ingest-consumer/payment-settlement tests đạt; billing,
scheduled ingestion/resume và SQLite backup critical checks đạt. Có sửa tên field trong test theo đúng
order contract account_number; không đổi public order schema. Không gọi provider/bank thật, không Docker.
Đã backup local trước migration; live API ready0013, admin GET cả hai provider200, anonymous401,
encryption_ready=true/sourceENV/revision0; chưa tự import key env vào DB hoặc bật payments/provider.
Tài liệu/API fields/master-key recovery tại16. Hai thay đổi supervisor dang dở trước đó giữ riêng, không
thuộc component này; không tiếp tục triển khai supervisor theo yêu cầu dừng trước đó.

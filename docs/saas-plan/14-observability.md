# Observability local và vận hành beta

## Bề mặt truy cập

Migration `0012` thêm process heartbeat. API và runtime cần migrate trước khi khởi động.
`GET /api/v1/admin/operations/health` yêu cầu admin access JWT; customer bị từ chối.
`GET /internal/metrics` dùng riêng `Authorization: Bearer <QUANTIK_METRICS_TOKEN>`;
admin JWT không thay scrape token. Để trống token thì endpoint trả `503 METRICS_DISABLED`.
Đặt token trong `.env`/secret mount, không URL/query hay source. Token local đã được tạo trong `.env`.
`QUANTIK_RELEASE_ID` là nhãn release cố định, mặc định `development`.

Giữ metrics trên private network; reverse proxy public phải chặn `/internal/`.
`/health/live` chỉ chứng minh API còn xử lý HTTP. `/health/ready` kiểm DB/schema, không hứa
broker/provider/model đã sẵn sàng. Operations health trả JSON ngay cả khi broker down;
đọc từng trường, không coi HTTP200 là toàn bộ SaaS healthy. DB/query lỗi trả503.
Collector cache10 giây, một collection đồng thời mỗi API process; scrape timeout nên >=10 giây.

## Số liệu và giới hạn

| Nhóm | Đã có |
|---|---|
| HTTP | Counter theo method/route template/status class; cumulative latency histogram, process CPU seconds |
| Jobs | Status counts, oldest queued/retry/waiting age, expired execution leases, outbox pending/oldest |
| Execution | p95/p99 actual attempt runtime; admission-to-attempt-start wait; sample count |
| Quota | Terminal RESERVED leaks; counter/reservation/active-job/refund discrepancies |
| Broker | Passive queue readiness, ready messages, consumer count, dead letters |
| Pipelines | Dependency/snapshot/screening/task/model status counts, latest manifest age |
| Model/calendar | Published model presence/age/live validation, calendar coverage, expected screening slot missed |
| Billing | Payment status counts, oldest reconciliation age |
| Processes | Recent heartbeat counts by fixed role and RUNNING/ERROR state; zero for missing roles |
| Provider/disk | Calls over60/3600/86400 seconds, cooldown remaining, working-directory disk free/total |

Execution/wait percentiles dùng tối đa1000 attempts đã dừng gần nhất, nearest-rank; không phải
benchmark tải hoặc cửa sổ5 phút. Retry wait tính từ admission gốc, bao gồm các lần chờ trước.
Queue age dùng admission time, không phải AMQP enqueue age. Model age là ngày lịch từ cutoff.
Manifest presence không chứng minh nguồn LIVE_VALIDATED. Screening expected slot chỉ đánh giá khi
calendar có coverage; thiếu calendar thì số liệu slot bị bỏ qua, không xuất false=healthy.

DB và broker gauges là số liệu dùng chung; nhiều API replica lặp cùng giá trị. Dùng `max`/deduplicate,
không cộng chúng. HTTP/CPU counters là từng process; restart reset counter, dùng `rate`.
Không đặt account/symbol/job IDs vào labels. Không có process IDs trong metric labels.
Rabbit probe chỉ đọc queue đã tồn tại, không tạo topology và không gọi provider; socket2s/stack3s.
Collector chờ probe tối đa4s; chỉ có một daemon probe thread/connection mỗi API process. Nếu RPC treo,
scrape báo BROKER_PROBE_TIMEOUT và không tạo thêm probe cho đến khi thread cũ kết thúc.
Unacked messages cần RabbitMQ Prometheus plugin ở bước deployment. Host/container RAM, DB pool/query
latency và snapshot coverage/runtime chi tiết còn cần exporter/instrumentation tiếp theo.

## Heartbeat và logs

API, dispatcher, worker, dependencies, reconciler, scheduler và các schedule-worker ghi heartbeat20s;
alive khi last_seen <=90s và chưa STOPPED. Một thread riêng duy trì pulse khi worker đang chờ broker
hoặc compute. Do đó heartbeat còn sống không chứng minh job đang tiến triển; xem leases/backlog/runtime.
`--once` cũng ghi start/stop. Diagnostic heartbeat rows quá24h được dọn khi pulse; không xóa audit/job history.
Không khởi chạy provider khi chạy monitor; hai ingestion gates vẫn giữ nguyên.

Structured JSON events có UTC time, request UUID, route template, status, duration; runtime loop có
role/count/error class. Không ghi body, query, raw path, Authorization hoặc exception message.
Lỗi runtime giống nhau chỉ log tối đa một lần30s khi lỗi liên tiếp. Uvicorn chạy `--no-access-log`
để access logger ngoài ứng dụng không ghi OAuth callback query. Production log rotation/retention
thuộc process supervisor/log collector, chưa cấu hình trên VPS.

## Alert templates

`deploy/monitoring/prometheus.example.yml` là mẫu scrape; cần chỉnh target và secret-file mount.
`quantik.rules.yml` có ngưỡng beta đề xuất cho scrape/broker/worker, dead letters, outbox/queue,
quota, process errors, disk và HTTP5xx. Chưa cấu hình notification receiver, không tự gửi tin nhắn.
Những ngưỡng này không thay acceptance/SLA. Provider bị pause có chủ đích nên không có alert live-data
mặc định trong mẫu. Bật live thì cần thêm model/data freshness alerts theo calendar và provider policy.

Mẫu chưa chạy Prometheus/promtool ở local; không tuyên bố alert delivery đã nghiệm thu.
Khi provision monitoring, chạy `promtool check config` và `promtool check rules` trước khi bật.
Theo tài liệu chính thức: [scrape configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[alert rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).

## Kiểm chứng

Focused tests kiểm admin/customer permissions, scrape token riêng/disabled, route-template redaction,
cumulative histogram, quota discrepancy và compensating refunds, stale heartbeat exclusion, ERROR/STOPPED,
collection failure503 sau cache expiry. Local migration, scheduler/reconciler `--once` và API scrape thật
đã kiểm; runtime idle vẫn không import NumPy/Pandas/LightGBM. Không cần Docker build/container mới.

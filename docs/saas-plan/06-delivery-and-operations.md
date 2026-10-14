# 06 — Kế hoạch ba ngày và vận hành

## Mục tiêu

CONFIRMED: backend end-to-end cho nhóm khách đầu tiên trong ba ngày triển khai, không làm FE. Ngày tương đối tính từ khi bắt đầu implementation; không khẳng định deadline tuyệt đối hoặc production readiness. External credentials/VPS/domain thiếu có thể chặn live E2E, vẫn tiếp tục local contracts/runtime.

## Delivery gates

Plan dưới đây là thứ tự triển khai đề xuất P-14, giữ mục tiêu ba ngày. Có thể bắt đầu DB/jobs/adapters/quant/auth ngay; các OPEN chỉ chặn gate tương ứng. Chủ sản phẩm cung cấp credentials/quyền hạ tầng; agent triển khai phần kỹ thuật và lưu evidence. Thiếu credentials vẫn làm fixtures/local runtime, nhưng không đánh dấu live gate đạt.

| Ngày | Việc | Gate |
|---|---|---|
| 1 | Inventory output, schema/contracts, adapter v5, DB, quant extraction, queue minimum; khởi chạy backfill/bootstrap training và snapshot builder | API → worker → report đúng schema với fixed fixtures; screening mẫu có evidence; ghi rõ sections unavailable và trạng thái data/model thật |
| 2 | Hoàn tất validation bootstrap model + completed snapshot; regression report; JWT Google/admin, entitlement/quota, orders/SePay, admin config/history | Report đầy đủ trên data/model thật; single/batch reference nhất quán; auth/ownership/quota/webhook duplicate tests đạt; live integrations khi có secrets |
| 3 | Screening scheduler và weekly training schedule; Docker/HTTPS, metrics, retries/recovery; restore và load benchmark | HTTPS live E2E, seed admin, backup/restore, scientific regression và evidence tải; đóng mọi gate mở beta bên dưới |

Backfill → completed manifest → validation/publish bootstrap model → daily inference/reference snapshot → full analysis là dependency bắt buộc. Một số baseline computations chạy sớm được, nhưng không thay model thật bằng fixture để nghiệm thu full report. Tách contract/fixture gate ngày 1 khỏi scientific/live gate ngày 2–3. Nếu data/bootstrap/validation chưa đạt thì cập nhật tiến độ và xử lý dependency, không tự giảm scientific components hay ngưỡng promotion để vừa lịch.

### Các gate trước mở beta

| Gate | Nội dung cần chốt/kiểm tra | Trách nhiệm |
|---|---|---|
| Analysis contract | Field mapping, required sections/degraded policy; snapshot eligibility/coverage/normalization; costs trước stops; deadline chờ inputs | Agent chuẩn bị implementation/evidence; technical/scientific review O-04/O-06/O-09 |
| Screening | Score/coverage/ranking, provisional volume, lịch/cutoff và run readiness/deadline; latest không đổi khi failed | Agent kiểm thử; technical/product review O-03 |
| Gói/quota | Giá/quota/active limits, cancellation charge, bucket qua kỳ và refund rules | Chủ sản phẩm chốt O-02/O-05/O-08; agent kiểm tra races/ledger |
| Tích hợp và vận hành | Credentials Google/SePay/Vnstock, VPS/domain; live payment/login/data; backup/restore và resource budgets | Chủ sản phẩm cung cấp quyền/ngân sách; agent triển khai, kiểm chứng O-01/O-07 |

Evidence tải phải có tiêu chí đạt trước khi chạy: API admission/error bounds, không mất job, drain-time budget, provider limits và resource budget trên VPS được chọn. Chưa có SLA cố định thì ghi ngưỡng beta đã thống nhất cùng kết quả, không coi chỉ báo p95/p99 là tự động pass. OPEN không liên quan không chặn code; OPEN ảnh hưởng gate chưa chốt thì gate đó chưa đạt.

Ưu tiên correctness/output completeness hơn extra admin conveniences. Nếu thiếu gate cần báo thiếu và giới hạn beta, không đổi tên incomplete thành done. Không giảm scientific components âm thầm để đạt lịch.

## Docker deployment

Services đề xuất: reverse-proxy, api, scheduler, dispatcher/reconciler, data-worker, screening-worker, analysis-worker, training-worker, postgres, rabbitmq, monitoring và migration one-shot. Có thể gộp một số control processes beta nếu không phá scheduling/locks; lifecycle phải explicit.

API/worker dùng immutable image, non-root, secrets runtime, healthchecks, graceful shutdown, restart policy và resource limits. DB/broker chỉ private network. Migration chạy trước release, không mỗi API startup. Secrets Google/SePay/Vnstock không source/image/log; provider auth storage v5 phải kiểm tra khi dựng container.

Scale thêm replicas/machines; connection pools/concurrency không vượt DB budgets. Mỗi numerical process explicit OMP/BLAS/LightGBM threads. Không publish shared artifacts chỉ vào local worker disk. Backup DB/artifacts ngoài VPS, restore vào môi trường riêng.

OPEN: hardware/domain/cost/retention/RPO/RTO. Chưa có cơ sở chốt cấu hình VPS. Compose trên một VPS không cung cấp HA.

## Monitoring

- API rate, errors, latency và auth/admin login failures.
- Queue depth/oldest age, execution/wait p95/p99, retries, expired leases và failed dependencies.
- Provider calls/limits/backoff, symbols stale/missing, manifests và screening publish status.
- Training outcome, published model age và component unavailable/coverage.
- Snapshot build age/runtime/failures, target-session coverage và jobs chờ snapshot quá deadline.
- Orders/payment mismatches, pending reconciliation, quota reservation leaks.
- CPU/RAM/disk, DB pool/query health và broker readiness.

Alerts dựa age/error/staleness thresholds cấu hình được; logs structured có request/job/run IDs, không secrets/raw exception payloads. Health live khác ready: process hoạt động không chứng minh đủ dependencies.

## Test matrix

| Nhóm | Cases |
|---|---|
| Numerical parity | Fixed inputs compare original/new fields, tolerances, deterministic seed; method changes tách riêng |
| Cross-sectional snapshots | Single/batch invariance; rank không theo request universe; stale symbol/correction/pinned retries; atomic publish và model-age policy |
| Screening scoring | TRUE/FALSE/UNAVAILABLE counts, denominator cố định, group/overall eligibility, stable tie-break; provisional volume baseline không chứa nến hiện tại |
| Data | Units/timezones, caps, duplicate sessions, unfinished candle, missing features, correction |
| Auth | Wrong iss/aud/alg/expiry, role escalation, ownership, refresh reuse, admin seed |
| Usage/billing | Quota races, idempotency conflicts, cancel trước/sau compute và completion race, release về kỳ gốc, degraded report, refund/reconciler lặp; duplicated/out-of-order webhook, late/mismatched payments |
| Jobs | Crash after publish/before ack/after compute, expired lease, fencing, retry/cancel |
| Load | 500 same-symbol, diverse-symbol, mixed warm/cold; fairness, queue drain và resources |
| Operations | Clean Docker boot, migration, provider outage, backup/restore và redeploy |

Prototype backtest không là customer feature; numerical regression và temporal model validation vẫn bắt buộc. Test fixture không thay provider/live payment evidence. Load report ghi hardware, universe/config, dependency warmth, arrival pattern, worker count, provider mode và kết quả; không tuyên bố support 1.000 concurrent compute chỉ từ user count.

## Bàn giao BE

OpenAPI, report schema/field mapping, examples/errors, Google/JWT integration guide, payment/status flow, admin permissions, deployment/runbook, migration/backup steps và evidence report. FE tích hợp sau; chưa nghiệm thu E2E UI.

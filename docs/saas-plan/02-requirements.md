# 02 — Yêu cầu và tiêu chí nghiệm thu

## Chức năng

| ID | Yêu cầu | Nghiệm thu |
|---|---|---|
| FR-01 | User login Google, BE JWT | Xác minh token/code Google server-side; user không truy cập admin |
| FR-02 | Admin login local seed | Password hash; seed lặp không reset; brute-force bị giới hạn |
| FR-03 | Screening toàn sàn hai lần/ngày | Một run mỗi slot; latest chỉ đổi sau publish thành công |
| FR-04 | Screening provisional sáng | Input mode và timestamp rõ; thiếu criterion không biến thành false |
| FR-05 | Phân tích một mã theo yêu cầu | Mã ngoài candidates vẫn phân tích được nếu đủ dữ liệu |
| FR-06 | Báo cáo sâu đầy đủ | Đối chiếu từng field/nhận xét prototype; omissions có lý do |
| FR-07 | Lịch sử/status/cancel | Chỉ owner truy cập; cancel không làm process cũ ghi completion |
| FR-08 | Gói/quota | Admission nguyên tử; HTTP retry không trừ thêm; system failure xử lý refund policy |
| FR-09 | SePay | Webhook duplicate/out-of-order không cấp quyền trùng; có đối soát |
| FR-10 | Admin config/audit | Validate, version, rollback; ghi actor/reason/before-after |
| FR-11 | Training tuần | Labels mature; temporal validation; promotion và fallback có evidence |
| FR-12 | Provider limit | Tất cả workers tuân thủ hạn mức chung; không retry storm |
| FR-13 | Cross-sectional reference nhất quán | Cùng input versions/sizing: single và batch có rank/strength/score/action giống nhau; không dùng request universe để rank; snapshot chưa ready có deadline và reason |
| FR-14 | Screening score/coverage explicit | Theo policy mục 04: denominator cố định; missing giữ UNAVAILABLE; group/overall eligibility rõ; incomplete symbols vẫn có trong all-results |
| FR-15 | Terminal quota settlement | Theo policy mục 05: cancel/completion race có một settlement; retries không trừ thêm; release đúng bucket gốc; terminal không giữ active-job slot |

## Phi chức năng

| ID | Yêu cầu | Nghiệm thu |
|---|---|---|
| NFR-01 | Peak 500 requests | Burst cùng mã, nhiều mã, warm/cold; không mất job; báo p95/p99 và drain time |
| NFR-02 | Mở rộng workers nhiều VPS | Không phụ thuộc process-local locks/cache; worker mới nhận job đúng |
| NFR-03 | Durable execution | Restart/crash giữa publish, compute, commit không ghi report/quota trùng |
| NFR-04 | Security | Ownership/RLS, JWT validation, CSRF cho refresh cookie, logs không chứa secrets |
| NFR-05 | Reproducibility | Reports có input/model/config/engine versions, units và seed policy |
| NFR-06 | Recovery | Backup ngoài VPS, restore thử, kiểm tra schema/jobs/reports |
| NFR-07 | Observability | Queue age, runtime/errors, provider limit, data freshness, billing mismatch |

## E2E cho nhóm khách đầu tiên

1. User Google login → nhận token → đọc quyền/quota.
2. Tạo đơn → xác nhận thanh toán → subscription/entitlement được cấp đúng.
3. Đọc screening latest → tạo analysis → poll status → đọc report → đọc lại history.
4. Admin login → cấu hình gói → xem job/payment/data health → audit hiện đúng.
5. Restart worker và gửi request/webhook lặp; kết quả/quota vẫn chính xác.

Google/SePay fixture tests không thay thế kiểm tra live credentials. Load test local không thay evidence trên VPS. Mỗi acceptance lưu command, input mode, môi trường, version, kết quả và giới hạn.

FR-13/14/15 bổ sung acceptance cho các thiết kế PROPOSED P-10 đến P-13, không tự chuyển policy thành CONFIRMED. Required report sections/degraded policy, screening readiness và quota charging phải khóa tại gate tương ứng trong [kế hoạch triển khai](06-delivery-and-operations.md).

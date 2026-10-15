# Admin cấu hình SePay và Vnstock

Migration `0013` thêm `integration_configs`. Tất cả endpoints dưới đây yêu cầu **admin access JWT**;
customer hoặc chưa đăng nhập bị từ chối. Không có endpoint đọc lại API key đã lưu.

| Endpoint | Chức năng |
|---|---|
| `GET /api/v1/admin/integrations/vnstock` | Trạng thái key, revision, nguồn cấu hình và provider gate |
| `PATCH /api/v1/admin/integrations/vnstock` | Đặt/thay/xóa key Vnstock |
| `GET /api/v1/admin/integrations/sepay` | Cấu hình nhận tiền, trạng thái webhook key và trường còn thiếu |
| `PATCH /api/v1/admin/integrations/sepay` | Sửa cấu hình nhận tiền, webhook key và bật/tắt thanh toán |

## Giao thức cập nhật

GET trước để lấy `revision`. PATCH gửi `expected_revision` bằng revision đó, `reason` từ10–500 ký tự
và các trường cần sửa. Bỏ qua trường thì giữ nguyên. Thay đổi thành công tăng revision; client dùng
revision cũ nhận409 `INTEGRATION_REVISION_CONFLICT`, phải đọc lại trước khi sửa tiếp.
Không phải API tạo phiên bản lịch sử/rollback secrets: audit chỉ lưu revision, changed-field names, lý do
và actor; không lưu old/new key. Muốn đổi về key cũ phải nhập lại key đó.

Vnstock PATCH:

```json
{
  "expected_revision": 0,
  "reason": "Cập nhật key tài khoản Vnstock",
  "api_key": "YOUR_VNSTOCK_API_KEY"
}
```

`api_key` tối thiểu8 ký tự; dùng `null` để xóa, không dùng chuỗi rỗng. Response chỉ có
`api_key_configured: true/false`; không trả một phần hoặc toàn bộ key. Lưu key không gọi Vnstock,
không thử login, không mở slot và không thay đổi scheduler/provider gates.

SePay PATCH có các trường:

| Trường | Ý nghĩa |
|---|---|
| `webhook_api_key` | Secret Authorization Apikey của webhook; tối thiểu16 ký tự; null để xóa |
| `bank_code` | Mã ngân hàng, chuẩn hóa uppercase/TPB/970423 về TPBANK |
| `account_number` | Số tài khoản nhận tiền dạng chuỗi, giữ số0 đầu |
| `account_name` | Tên chủ tài khoản |
| `transfer_prefix` | VA/TKP prefix cho nội dung chuyển khoản; chuỗi rỗng để bỏ |
| `requires_transfer_prefix` | Có bắt buộc VA/TKP prefix không |
| `payments_enabled` | Bật/tắt tạo đơn/nhận webhook thanh toán |

```json
{
  "expected_revision": 0,
  "reason": "Cập nhật cấu hình ngân hàng nhận tiền",
  "bank_code": "TPBANK",
  "account_number": "0000000000",
  "account_name": "YOUR_ACCOUNT_NAME",
  "transfer_prefix": "YOUR_VA_PREFIX",
  "requires_transfer_prefix": true,
  "payments_enabled": false
}
```

Response trả bank/account/prefix cho admin sửa, cùng `webhook_api_key_configured`, `configuration_complete`
và `missing_fields`. Webhook key không bao giờ được trả. Chỉ secrets nhận null; trường khác nhận kiểu
đúng, chuỗi không có control characters hoặc khoảng trắng thừa. Trường ngoài schema bị từ chối.
Bật payments yêu cầu đủ bank/account/name/webhook key và prefix nếu bắt buộc; thiếu nhận422,
toàn bộ cập nhật rollback. Muốn xóa webhook key đang dùng phải đồng thời tắt payments.
Configuration_complete chỉ chứng minh đủ trường, không chứng minh tài khoản ngân hàng/provider đã xác minh.

## Hiệu lực runtime

- Khi chưa có record admin, đọc giá trị `.env` như trước (`source=ENVIRONMENT`, revision0).
- Lần PATCH đầu tiên chụp đủ cấu hình provider hiện tại rồi áp dụng phần thay đổi, lưu mã hóa trong DB.
  Từ đó DB là nguồn ưu tiên (`source=ADMIN_DATABASE`); sửa `.env` không ghi đè record này.
- Xóa secret lưu rõ trạng thái trống; không tự quay lại secret cũ trong `.env`. Không sửa file `.env` qua API.
- SePay đọc cấu hình ở mỗi request plans/create-order/webhook; thay đổi áp dụng cho request tiếp theo,
  không cần restart API. Request đang xử lý giữ cấu hình đã đọc. Các đơn đã tạo pin bank/amount/content,
  đổi bank config không sửa lại đơn cũ. Cần giữ kết nối nhận tiền cũ để xử lý các đơn còn hạn.
- Vnstock đọc key từ DB khi bắt đầu mỗi lần ingest; lần ingest đang chạy giữ key ban đầu.
  Việc ingest vẫn cần thao tác CLI hoặc scheduler đã bật; hai provider gates không bị API credentials thay đổi.
- Không tự gửi request xác minh Google/SePay/Vnstock khi GET/PATCH. Google credentials ngoài phạm vi API này.

## Mã hóa và khôi phục

Payload credentials/bank config mã hóa bằng authenticated Fernet, bind provider name và revision trong
envelope. Khóa mã hóa nằm riêng tại `QUANTIK_INTEGRATION_ENCRYPTION_KEY_PATH`, mặc định
`runtime/integration-encryption.key`, không nằm trong DB/source/response. Khóa local đã tạo và giới hạn
ACL cho tài khoản vận hành/SYSTEM. API và ingest workers phải dùng cùng file/secret mount.

Khởi tạo môi trường mới bằng `quantik generate-integration-key` sau khi activate shared venv;
command không ghi đè file có sẵn. Không tạo key mới khi restore DB cũ: phải khôi phục đúng key đi kèm.
Backup key riêng ở nơi an toàn; DB backup chỉ có ciphertext. Mất/sai key hoặc ciphertext bị sửa trả503,
không fallback âm thầm sang `.env`. Chưa có master-key rotation/migration tool trong API này.
Các key đã có trong `.env` trước đây vẫn ở đó; API không tự xóa chúng.

Nguồn thư viện: [cryptography Fernet](https://cryptography.io/en/latest/fernet/).

## Kiểm chứng

Focused checks với DB migrate và JWT thật: admin/user boundaries, env defaults, secret redaction,
encrypted storage/audit, stale revision409, validation422 không echo input, missing/wrong key/ciphertext503,
clear không fallback, mocked ingest nhận đúng rotated key, SePay hot updates/old-key rejection,
old-order bank pinning và webhook settlement/quota với tài khoản đã đổi, enable/disable gates.
Billing và scheduled ingestion/resume critical checks cũng đạt. Không gọi provider/ngân hàng thật;
không chạy/build Docker cho phần này. Live device-slot/VA/webhook acceptance vẫn chưa đạt.

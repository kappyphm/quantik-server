# Tài khoản demo cho máy trải nghiệm (booth triển lãm)

Tài liệu này mô tả tính năng tài khoản demo của Quantik: một tài khoản
khách dùng chung, không giới hạn tính năng client, đăng nhập bằng mã do
quản trị cấp, dùng cho các máy trải nghiệm đặt tại booth triển lãm.

Bản triển khai trên VPS: release `20261028-demo-01`, API ready, schema
`0018`, backend image `20742d56`, frontend image `882598d6` (kiểm chứng
ngày 2026-10-28).

## 1. Cách hoạt động

- Tài khoản demo là một tài khoản khách bình thường, gắn cờ `is_demo`
  (bảng `accounts`, migration `0018`), tên đăng nhập nội bộ `demo`,
  không đăng nhập qua Google, không có mật khẩu.
- Quản trị tạo mã demo từ trang `/admin/demo`. Chỉ lưu SHA-256 của mã
  trong bảng `demo_codes`; mã gốc chỉ hiện một lần lúc tạo, không bao
  giờ trả lại trong API danh sách.
- Mã demo dùng lại được cho nhiều máy, cho tới khi bị thu hồi hoặc hết
  hạn (nếu quản trị đặt hạn). Máy booth nhập mã tại trang đăng nhập
  (mục "Dùng mã demo cho máy trải nghiệm") hoặc quản trị đăng nhập
  demo ngay trên máy đó từ trang `/admin/demo`.
- Đổi mã lấy phiên: `POST /api/v1/auth/demo/exchange` với JSON
  `{"code": "..."}`. Phiên demo là phiên khách bình thường (cookie
  `quantik_user_refresh`), tách biệt với phiên quản trị, nên quản trị
  vẫn giữ phiên admin khi đăng nhập demo trên cùng một trình duyệt.

## 2. Giới hạn nào được bỏ, giới hạn nào còn

Được bỏ qua với tài khoản demo (bypass thật trong code, không phải gói
to giả vờ):

- Kiểm tra gói khi xem screening (`screening_access.require_access`).
- Kiểm tra quyền gói khi tạo yêu cầu phân tích
  (`POST /api/v1/analyses`).
- Giới hạn số yêu cầu mỗi ngày và số job chạy cùng lúc trong
  `jobs.admit`. Job demo vẫn tạo reservation trên quota bucket để
  worker settlement hoạt động bình thường.

Vẫn còn, cố tình giữ lại:

- Tài khoản demo **không tạo được đơn thanh toán thật**:
  `POST /api/v1/orders` trả `403 DEMO_ORDER_NOT_ALLOWED`.
- Tài khoản demo dùng chung: mọi máy booth thấy chung lịch sử phân
  tích, chung hồ sơ demo. Đây là đánh đổi của acc dùng chung, khắc
  phục bằng nút Reset (mục 4).

Để quota bucket và subscription có bản ghi thật, tài khoản demo vẫn có
một gói demo (quota 999.999, active limit 100). Gói này không phải là
cơ chế giới hạn chính; các bypass ở trên mới là cơ chế.

## 3. API quản trị

Tất cả đều yêu cầu phiên quản trị (`admin`):

| Phương thức | Đường dẫn | Tác dụng |
|---|---|---|
| `POST` | `/api/v1/admin/demo/codes` | Tạo mã. Body: `{"expires_in_days": null}` (để trống là không hết hạn) hoặc số ngày 1–365. Trả về mã gốc đúng một lần. |
| `GET` | `/api/v1/admin/demo/codes` | Liệt kê mã (không kèm mã gốc): id, người tạo, thời điểm tạo/hết hạn, trạng thái thu hồi, số lượt dùng, lần dùng gần nhất. |
| `POST` | `/api/v1/admin/demo/codes/{id}/revoke` | Thu hồi mã. Máy đang dùng mã vẫn giữ phiên hiện tại cho tới khi hết hạn phiên hoặc bị reset; máy mới không vào được nữa. |
| `POST` | `/api/v1/admin/demo/reset` | Reset tài khoản demo (xem mục 4). |

Sự kiện audit ghi lại: `demo.code_created`, `demo.code_exchanged`,
`demo.code_revoked`, `demo.reset`.

## 4. Vận hành booth: tạo mã, thu hồi, reset

Quy trình chuẩn cho một đợt triển lãm:

1. Quản trị đăng nhập `/admin/login` (tài khoản quản trị, không phải
   tài khoản demo), mở `/admin/demo`.
2. Bấm "Tạo mã", chọn hạn nếu cần, **sao chép mã ngay** vì mã chỉ
   hiện một lần.
3. Trên mỗi máy booth, mở trang đăng nhập, nhập mã demo vào mục mã
   demo, hoặc quản trị mở `/admin/demo` trên máy đó và bấm "Đăng nhập
   demo".
4. Hết đợt triển lãm (hoặc trước khi khách mới dùng): bấm "Reset tài
   khoản demo". Reset sẽ:
   - Thu hồi toàn bộ phiên demo đang chạy (máy booth bị đăng xuất).
   - Hủy job demo chưa xong và xóa yêu cầu phân tích/báo cáo của tài
     khoản demo.
   - Khôi phục hồ sơ demo về tên mặc định.
   - **Không** xóa mã demo và **không** chạm vào đơn hàng (demo vốn
     không tạo được đơn).
5. Nếu mã bị lộ ra ngoài booth: thu hồi mã đó ngay và tạo mã mới.

## 5. Triển khai

Backend có migration `0018` (cột `accounts.is_demo`, bảng
`demo_codes`). Quy tắc bắt buộc đã rút ra từ hai sự cố deploy:

- **Mỗi migration mới phải tăng `SCHEMA_REVISION` trong
  `service/db.py`** (lần đầu deploy demo quên tăng, API báo
  `DATABASE_SCHEMA` và unhealthy; đã rollback về image cũ + schema
  0017 rồi sửa bằng PR #3 mới deploy lại được).
- **Timestamp của subscription là Postgres INTEGER (32-bit, tối đa
  2147483647).** Gói demo ban đầu đặt 100 năm nên insert tràn số và
  tạo mã trả 500 `NumericValueOutOfRange` (test SQLite không bắt được
  vì SQLite dùng số 64-bit). Gói demo hiện tại là 10 năm, và code chặn
  cứng `valid_until` dưới `INT32_MAX - 1 ngày` (sửa bằng PR #4).

Quy trình deploy backend (áp dụng cho mọi lần sau):

1. Chạy `sh /opt/quantik/backup.sh` và xác nhận có file `.dump` kèm
   `.sha256` đã verify. Backup lỗi thì dừng, không deploy.
2. Build image app-update từ base image đang chạy
   (`Dockerfile.app-update`, tag base hiện tại thành
   `quantik-backend-base:test` trước vì build-arg dạng `sha256:` sẽ
   bị Docker hiểu là cần pull).
3. Cập nhật `BACKEND_IMAGE` trong `.env.production`, chạy
   `docker compose run --rm migrate`, xác nhận `alembic_version`
   đúng revision mới.
4. Recreate các service backend (`api worker dispatcher dependencies
   reconciler scheduler ingest screen snapshot train`), không chạm
   postgres/rabbitmq/web.
5. Xác nhận `/health/ready` trả `ready` với đúng schema, và
   `/dashboard` 200.

Deploy frontend chỉ recreate `web`, không chạm backend hay database.

## 6. Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| Tạo mã trả 500 | Lỗi server khi tạo tài khoản/gói demo; xem log `docker logs quantik-api-1` | Kiểm tra traceback; hai lỗi đã biết ở mục 5 đều đã sửa |
| Đổi mã demo trả 401 | Mã đã bị thu hồi, đã hết hạn, hoặc nhập sai | Tạo mã mới, kiểm tra trạng thái mã ở `/admin/demo` |
| Đăng nhập admin qua API trả 403 | Thiếu header `Origin` khi gọi qua proxy frontend | Gọi từ trình duyệt, hoặc kèm `Origin: https://quantik.io.vn` |
| Demo tạo đơn trả 403 `DEMO_ORDER_NOT_ALLOWED` | Đúng thiết kế, không phải lỗi | Demo không tạo đơn thanh toán thật |
| API báo `DATABASE_SCHEMA`, unhealthy | `SCHEMA_REVISION` trong code lệch với `alembic_version` trong DB | Đồng bộ hai giá trị rồi recreate `api`; không tự ý sửa `alembic_version` nếu chưa rõ migration nào đang thiếu |
| Máy booth thấy dữ liệu của khách trước | Tài khoản demo dùng chung | Bấm "Reset tài khoản demo" ở `/admin/demo` |

## 7. Bảo mật cần nhớ

- Mã demo không phải mật khẩu của một tài khoản cá nhân, nhưng ai có
  mã cũng dùng được tài khoản demo không giới hạn. Chỉ đưa mã cho máy
  booth, thu hồi ngay khi mã lộ hoặc hết đợt triển lãm.
- Không bao giờ ghi mã demo, mật khẩu quản trị, hay token vào tài
  liệu, chat, hay file log. Tài liệu này cố tình không chứa bất kỳ
  giá trị bí mật nào.
- Tài khoản quản trị hiện tại (2026-10-28): username `quantik`,
  đăng nhập tại `/admin/login`. Mật khẩu quản trị chỉ người giữ mới
  biết; quên mật khẩu thì reset hash trong DB, không có cách đọc lại
  mật khẩu cũ.

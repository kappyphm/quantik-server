# Báo cáo deploy thử production và bảo mật VPS

Ngày kiểm tra: 2026-10-16, giờ Việt Nam. Domain: https://quantik.io.vn. Release: `20261016-02`.

## Kết quả triển khai

Bản thử đã chạy công khai qua HTTPS. Image được build bằng Docker Desktop trên máy Windows local, push lên repository Docker Hub **private** `kappyphm/quantik`, rồi VPS pull về. VPS chỉ nhận các file vận hành, không clone source ứng dụng từ Git. Không build ứng dụng trên VPS.

| Thành phần | Phiên bản / định danh đã deploy |
| --- | --- |
| Backend | `backend-20261016-02`, digest `sha256:b5e63314483ccf352c0b7b4f593de5c1d10dc61a1df495f353d00b30528b41c4` |
| Frontend | `frontend-20261016-02`, digest `sha256:3b924ab217714378be2809892f1ed36a2cbd2d18cdf0738be3b4cc4856172c0f` |
| PostgreSQL | 17, digest `sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826` |
| RabbitMQ | 4.2, digest `sha256:c8d1117e143aa39f99133299abdfaa2f54b2dce8fce1b7ac57e3c7693f8cfeca` |
| Schema | Alembic `0015`, API readiness đạt |
| VPS | Ubuntu 26.04 LTS, kernel `7.0.0-30-generic`, 2 vCPU, RAM 3,8 GiB, swap 4 GiB |
| Docker VPS | Engine 29.8.2, Compose 5.5.1 |

Chín container đang chạy: web, api, worker, dispatcher, dependencies, reconciler, scheduler, postgres, rabbitmq. API/web/PostgreSQL/RabbitMQ đạt healthcheck. Sáu tiến trình backend được collector ghi nhận RUNNING; API và web có restart count bằng 0 ở lần kiểm tra cuối. RabbitMQ có một consumer, không có ready/dead messages. RAM container khi idle tổng khoảng 713 MiB; máy còn khoảng 2,5 GiB RAM khả dụng và 28 GiB đĩa trống. Đây là số đo idle, chưa phải benchmark tải.

## Những gì đã kiểm chứng

- Build Next.js production và TypeScript hoàn tất. Frontend image chạy trên local trả 200; backend image import được API và các thư viện NumPy/SciPy/LightGBM/ARCH/HMM. Kiểm tra dependency bằng pip check đạt trước khi loại pip khỏi runtime.
- Sáu test proxy/auth frontend và một test tích hợp admin-auth backend đạt. Lần chạy backend đầu thiếu PYTHONPATH; chạy lại với backend/src đúng đã đạt.
- **30 kiểm tra HTTPS trên domain đạt**, gồm trang chủ/static JS, health/live, health/ready, plans, chặn admin API khi chưa đăng nhập, chặn lộ file nhạy cảm, login/refresh/logout và SSR admin gate.
- Cookie refresh và gate có Secure, HttpOnly, SameSite Strict. Mutation sai Origin bị từ chối. Access token bị vô hiệu sau logout. Smoke script không in password/token.
- Trình duyệt mở được trang chủ và điều hướng tới trang đăng nhập Google; không ghi nhận console error trong luồng đã xem. Chưa kiểm tra mọi màn hình hoặc mọi kích thước màn hình.
- Google start trả authorization URL với redirect URI đúng `https://quantik.io.vn/api/v1/auth/google/callback`. Chưa thực hiện consent hoặc callback Google thật.
- Một backup PostgreSQL định dạng custom đã được kiểm tra checksum/table of contents, restore vào database tạm riêng, đối chiếu schema và số tài khoản, rồi xóa database tạm. Không restore đè DB thật.

Hai kỳ vọng ban đầu của smoke script được sửa theo contract thực tế: admin ẩn nội dung và render login ngay tại `/admin` thay vì redirect; thuộc tính SameSite của cookie không phân biệt hoa/thường. Lần chạy cuối không bỏ qua kiểm tra bảo mật nào trong danh sách trên.

## Bảo vệ đã áp dụng cho bản deploy

| Hạng mục | Trạng thái kiểm chứng |
| --- | --- |
| HTTPS | Chứng chỉ Let's Encrypt hợp lệ, hết hạn 2027-01-01; handshake TLS 1.3 đạt, cấu hình chỉ cho TLS 1.2/1.3 |
| HTTP | Chuyển hướng sang HTTPS domain đúng |
| Firewall | UFW active, mặc định deny incoming; 22/80/443 được mở |
| Kết nối ngoài | Probe từ local: 22/80/443 mở; 3000/8000/5432/5672/15672/2375/2376 đóng hoặc bị lọc |
| App upstream | Port 3000 và 8000 chỉ bind 127.0.0.1; PostgreSQL/RabbitMQ không publish port ra host |
| Container ứng dụng | Non-root, root filesystem read-only, drop ALL capabilities, no-new-privileges; không mount Docker socket |
| Database | App role quantik không có superuser/createdb/createrole; mật khẩu quản trị DB riêng không truyền vào app |
| Secrets | Env root:root 0600; key directory UID 10001 mode 0700; key files mode 0400; không đưa env/key vào image |
| Registry | Repository private; credential dùng khi pull nằm tạm dưới /run và đã được xóa |
| HTTP headers | HSTS, nosniff, SAMEORIGIN, Referrer-Policy, Permissions-Policy; ẩn X-Powered-By và phiên bản Nginx |
| Đường nội bộ | /.env, /.git/config, /docs, /openapi.json, /internal/metrics trả 404 |
| Rate limit | Nginx giới hạn login và Google start theo IP; backend giữ throttle riêng |
| Log | Docker log rotation 10 MB x 3/service; access log riêng dùng URI không có query string để tránh ghi OAuth code |
| Backup | Timer local hằng ngày khoảng 02:00 Việt Nam; backup đầu và restore probe đã đạt |

Password admin khởi tạo được lưu ở `/root/quantik-initial-admin.txt` trên VPS, quyền 0600; username `admin`. Password không được ghi trong báo cáo. Password bootstrap đã được bỏ khỏi env ứng dụng sau seed. Env development local được giữ nguyên.

## Phát hiện bảo mật còn mở

| Ưu tiên | Phát hiện và bằng chứng | Hướng xử lý |
| --- | --- | --- |
| Cao | `sshd -T` cho thấy PermitRootLogin yes và PasswordAuthentication yes; SSH mở public. Fail2ban sshd ghi nhận tổng 39.778 lần thất bại, 3.394 lượt ban tại thời điểm kiểm tra. | Chuyển sang user quản trị riêng + sudo, kiểm tra key login ở phiên SSH thứ hai rồi tắt password login và root password login. Không tự thay đổi chính sách truy cập trong lượt chỉ yêu cầu kiểm tra/báo cáo này. |
| Cao | Sau apt update, có **75 package chờ cập nhật**, 52 dòng có security pocket; gồm OpenSSH/OpenSSL và kernel mới hơn. unattended-upgrades đang active nhưng chưa đồng nghĩa các bản này đã được áp dụng. | Lập lượt vá hệ điều hành, kiểm tra SSH và container sau nâng cấp; reboot có kiểm soát nếu kernel yêu cầu. Lượt này chưa nâng cấp OS/reboot. |
| Cao | Backup và key hiện vẫn trên cùng VPS; chưa có bản sao offsite hay kiểm chứng mất toàn máy. | Thiết lập backup mã hóa ra nơi độc lập, key backup riêng, retention và RPO/RTO; local timer không thay thế offsite. |
| Trung bình | Scanner vẫn nhận diện gói OS có cảnh báo High trong image, xem bảng bên dưới. | Theo dõi bản vá Debian/base image, đánh giá khả năng khai thác của từng CVE; rebuild/rescan định kỳ. Không kết luận đã hết rủi ro chỉ vì container non-root. |
| Trung bình | Tổng giới hạn RAM các heavy worker có thể vượt RAM VPS nếu bật đồng thời. Chưa có stress/load test hoặc kiểm chứng job scientific live. | Giữ một analysis worker và profile pipeline/training tắt trong trial; đo tải và giới hạn lịch/concurrency trước khi bật. |
| Thấp | npm audit có một cảnh báo Low của KaTeX hiện tại: GHSA-238p-pmpm-9mq7; bản vá từ 0.18.2. | Nâng KaTeX trong lượt riêng có kiểm tra render công thức; không dùng npm audit fix --force trên bản thử. |
| Thấp | Chưa áp dụng CSP; SSH còn X11Forwarding/AllowTcpForwarding bật. | Thiết kế CSP tương thích Next.js/TradingView và cân nhắc tắt forwarding không cần thiết sau khi xác nhận luồng quản trị. |

Fail2ban, UFW, Docker, Nginx, unattended-upgrades và certbot timer đều được bật. Docker daemon dùng Unix socket, không thấy listener TCP quản trị. Chưa thử renewal chứng chỉ thật; timer đang bật không thay bằng chứng renew thành công.

### Kết quả quét image

Docker Scout 1.17.0 quét **High/Critical** trên image local cuối cùng. Có 0 Critical trong phạm vi quét, backend còn 9 High/4 package, frontend còn 8 High/3 package. Các phát hiện có phần trùng giữa hai image, không cộng thành 17 CVE độc lập. Đây là nhận diện theo metadata package và advisory feed, không phải chứng minh exploit.

| Gói Debian | Advisory High còn lại theo Scout |
| --- | --- |
| util-linux | CVE-2026-53613, CVE-2026-78409, CVE-2026-78410, CVE-2026-78408, CVE-2026-53615, CVE-2026-76642 |
| perl | CVE-2026-82560 |
| zlib | CVE-2026-85091 |
| openssl, backend | CVE-2026-84782 |

Scout ghi fixed version là `not fixed` cho các dòng trên ở lần quét. Trước làm sạch, backend có 14 High, frontend 26 High. Đã loại pip/npm/corepack/yarn runtime và cập nhật perl-base frontend. Các cảnh báo msgpack/urllib3/setuptools trước đó nằm trong BOM vendored của pip; các cảnh báo npm trước đó nằm dưới thư mục npm, không phải dependency API trực tiếp. SARIF gốc và sau xử lý được lưu cạnh báo cáo. Chưa quét PostgreSQL/RabbitMQ bằng Scout, chưa chạy pentest hoặc điều tra xâm nhập host; không thể kết luận máy chưa từng bị xâm nhập từ các kiểm tra này.

## Giới hạn chức năng của bản thử

Production mode không bật fixture/degraded analysis. Thanh toán và scheduled provider đều false. Provider calls trong collector bằng 0; không thử login lại Vnstock hoặc chiếm slot thiết bị. Frontend vẫn có màn hình/dữ liệu minh họa theo code hiện tại và chưa nối toàn bộ analysis/report/screening API.

Chưa có manifest live và published model. Profile pipelines/training chưa chạy. Collector có 16 scheduled task FAILED với TASK_DEADLINE, 4 PENDING và 10 screening run FAILED từ việc scheduler đánh dấu các lịch quá hạn khi bootstrap. Đây không phải kết quả screening đã được nghiệm thu. Log lần tick đầu còn có counter `created=-20`; đọc code cho thấy counter cộng rowcount của PostgreSQL insert, cần sửa cách đếm và kiểm tra riêng. DB vẫn có task thật; không dùng counter này làm bằng chứng admission/scheduling đạt.

Bản deploy đạt mục tiêu thử hạ tầng, auth và domain. Việc mở khách trả phí cần đóng các gate live Google/Vnstock/SePay, dữ liệu/model, frontend integration, load/resource budget, OS/SSH hardening và offsite recovery.

## Tài liệu tham chiếu

- [Hướng dẫn vận hành và build](README.md), [smoke domain](smoke_domain.py), [restore probe](verify_restore.py).
- [Docker security](https://docs.docker.com/engine/security/) và [port publishing](https://docs.docker.com/engine/network/port-publishing/).
- [Next.js standalone output](https://nextjs.org/docs/app/api-reference/config/next-config-js/output).
- [Ubuntu OpenSSH](https://ubuntu.com/server/docs/how-to/security/openssh-server/).
- [KaTeX advisory chính thức](https://github.com/advisories/GHSA-238p-pmpm-9mq7).

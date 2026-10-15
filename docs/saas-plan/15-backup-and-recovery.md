# Backup và khôi phục DB

## Phạm vi

Đã có `quantik backup`, `quantik verify-backup` và `quantik restore-backup` cho SQLite/PostgreSQL.
Chạy từ repository root, shared venv đã activate. Backup online gồm schema và dữ liệu ứng dụng,
kể cả reports, model text artifacts và historical/snapshot JSON đang lưu trong DB.
Không kèm `.env`, RSA keys, integration encryption key, Vnstock auth state hoặc cluster-global roles/tablespaces.
Các secrets/keys cần bản lưu riêng có mã hóa; chưa có offsite destination được cấu hình.
Từ migration0013, credentials admin nằm trong DB dưới dạng ciphertext: khi restore phải khôi phục đúng
integration encryption key ở secret mount riêng. Tạo key mới không giải mã được dữ liệu cũ; xem[16](16-admin-integrations.md).

Mỗi bundle có manifest format1, dialect, UTC timestamp, Alembic revision, byte count và SHA256.
Không lưu connection URL/password trong manifest. Output nằm dưới `runtime/backups`, được gitignore.
Directory/file modes700/600 trên POSIX; Windows dùng ACL kế thừa, cần giữ thư mục chỉ cho tài khoản vận hành.
DB backup chứa dữ liệu khách hàng và password/session hashes nên được xử lý như dữ liệu riêng tư.
SHA256 phát hiện hỏng file, không xác minh nguồn gửi. Chỉ restore bundle từ nguồn vận hành tin cậy.

SQLite dùng online backup API thay vì copy file đang mở; kiểm `quick_check` và foreign keys trên bản sao.
PostgreSQL dùng custom-format pg_dump với exported REPEATABLE READ snapshot; revision và dump cùng snapshot.
Không backup riêng các schema chọn lọc: database phải dành riêng cho Quantik.
Native PostgreSQL client utilities cần có trên PATH và tương thích server.
Client native đã được implement nhưng local Windows hiện không có pg_dump/pg_restore; nhánh container
đã kiểm thật bằng tools đi kèm PostgreSQL17 image có sẵn.

## Tạo và kiểm tra bundle

```powershell
& "$HOME\.venv\Scripts\Activate.ps1"
quantik backup
# Có thể đổi nơi lưu, ví dụ ổ backup riêng:
quantik backup --directory runtime/backups
quantik verify-backup --backup runtime/backups/BACKUP_DIRECTORY
```

Thay `BACKUP_DIRECTORY` bằng tên bundle vừa tạo. `verify-backup` chỉ kiểm manifest/size/checksum,
không thay restore drill. Tạo bundle bằng folder `.partial`, flush database/manifest xuống file trước khi đổi tên completed;
failed run chỉ dọn đúng các file do chính lần chạy đó tạo. Không tự xóa backups cũ.

Nếu PostgreSQL nằm trong container local và không có native tools:

```powershell
quantik backup --postgres-container YOUR_DB_CONTAINER
```

Đây là tên container PostgreSQL thực tế do người vận hành chọn. Tool không tạo/stop/build container.
Chỉ chấp nhận DB URL localhost/127.0.0.1 và xác minh published5432 port khớp trước khi chạy utilities.
Password chuyển qua child environment và `docker exec --env NAME`, không nằm trong process arguments.
Remote PostgreSQL dùng native tools. SSL cert paths ở nhánh container phải tồn tại trong container.
Tool không tự mount/copy credentials để chữa đường dẫn.

## Restore drill và chuyển môi trường

1. Xác minh checksum và giữ nguyên DB đang phục vụ. Chọn target riêng, chưa có dữ liệu.
2. Đặt `QUANTIK_RESTORE_DATABASE_URL` trong environment/`.env` riêng tư, không command arguments/chat.
   SQLite target phải là file chưa tồn tại; PostgreSQL target phải là database đã tạo trống, có tên khác DB hiện tại.
3. Chạy:

```powershell
quantik restore-backup --backup runtime/backups/BACKUP_DIRECTORY
# Nếu dùng tools trong PostgreSQL container local:
quantik restore-backup --backup runtime/backups/BACKUP_DIRECTORY --postgres-container YOUR_DB_CONTAINER
```

4. Đối chiếu revision, dữ liệu cốt lõi/ledger/report/model và chạy readiness trong môi trường cách ly.
   Không bật webhook/customer traffic/provider trong restore drill. Bundle không tự đổi configured DB URL.
5. Khi khôi phục sau sự cố thật: dừng app/worker cũ, kiểm secrets/RSA keys, đổi cấu hình sang DB mới,
   migrate nếu cần và khởi động control processes. Chỉ sau khi đã quyết định chuyển sang restored DB,
   chạy `quantik reconciler --once` rồi `quantik recover-delivery --once` trước dispatcher/workers nếu
   broker đã mất queue. Reconcile lại sau khi execution leases cũ hết hạn; không tự giả định job đang RUNNING đã xong.
6. Kiểm quota/ledger, jobs và payment reconciliation trước mở traffic. Restore quay ngược DB có thể làm
   mất webhook sau thời điểm backup: đối chiếu lịch sử giao dịch SePay rồi xử lý qua luồng admin có audit.

Restore không có `--clean`/drop-target và không ghi đè DB hiện tại. PostgreSQL kiểm target không có user
relations/functions, restore trong một transaction và exit-on-error; lỗi restore để nguyên target để điều tra,
không tự drop database. Owner/ACL không phục hồi từ dump; cần cấp lại quyền qua cấu hình deployment.
Restore verification kiểm Alembic revision sau import; full-content comparison được thực hiện trong kiểm thử,
không chạy quét toàn DB trên mọi lần restore. Nếu lỗi verification sau commit, vẫn giữ nguyên DB hiện tại,
điều tra target riêng trước khi sử dụng.

Mặc định kỹ thuật đề xuất: backup trước migration/release và hằng ngày02:00 giờ Việt Nam; copy mã hóa ra
khỏi máy chạy ứng dụng, thường xuyên restore drill. Chưa bật scheduler backup/offsite/retention tự động;
RPO/RTO chưa nghiệm thu. Không coi một bundle local là disaster recovery đầy đủ.

## Bằng chứng

- SQLite focused check: dữ liệu tại thời điểm backup giữ nguyên khi source tiếp tục thay đổi; restore vào
  file mới; existing/current DB bị từ chối; file bị sửa checksum không tạo target; không sót `.partial`.
- PostgreSQL17 thật: exported-snapshot dump/restore qua chính CLI utilities trong một container tmpfs1CPU/512MiB;
  nội dung tất cả model tables khớp, existing target và current DB đều bị từ chối. Hai focused checks đạt trong7.06s;
  tổng runner14.16s. Không pull/build image, không tạo volume, container/temporary credentials đã dọn.
- CLI trên DB local đang chạy schema0012: tạo bundle, verify checksum, restore cách ly và so SHA256 của
  SQL export; khớp124 statements. Restore-check directory đã dọn; giữ một bundle local có mục đích.

Runner lặp lại có chủ đích:

```powershell
python backend/tools/run_infrastructure_checks.py --postgres-image EXISTING_IMAGE_ID --backup-only
```

Giới hạn: chưa kiểm restore từ offsite, mất toàn máy/VPS hoặc native Windows PostgreSQL tools.
Nguồn chính thức: [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html),
[Python SQLite online backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).

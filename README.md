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
python -m pytest backend/tests/test_admin_auth.py -q
```

Test này dùng DB và RSA keys tạm, kiểm tra seed, login, JWT claims/algorithm, CSRF Origin,
refresh rotation/reuse, logout và rate limiting. Chưa thay bằng chứng PostgreSQL concurrency hoặc Google live.
Không có tài nguyên Docker được tạo bởi test này.

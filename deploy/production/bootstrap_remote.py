"""Create fresh production secrets on the VPS; input JSON arrives through SSH stdin.

Refuses to overwrite an existing deployment environment. Never prints secret values.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def main():
    incoming = json.load(sys.stdin)
    root = Path('/opt/quantik')
    root.mkdir(mode=0o700, exist_ok=True)
    env_path = root / '.env.production'
    if env_path.exists():
        raise SystemExit('Production environment exists; refusing to overwrite')
    key_dir = root / 'secrets'
    key_dir.mkdir(mode=0o700, exist_ok=True)
    password_db, password_broker, password_admin = (secrets.token_hex(24) for _ in range(3))
    release = incoming['release']
    values = {
        'BACKEND_IMAGE': incoming['backend_image'],
        'FRONTEND_IMAGE': incoming['frontend_image'],
        'POSTGRES_PASSWORD': password_db,
        'POSTGRES_ADMIN_PASSWORD': secrets.token_hex(24),
        'RABBITMQ_PASSWORD': password_broker,
        'QUANTIK_ENVIRONMENT': 'production',
        'QUANTIK_DATABASE_URL': f'postgresql+psycopg://quantik:{password_db}@postgres:5432/quantik',
        'QUANTIK_AMQP_URL': f'amqp://quantik:{password_broker}@rabbitmq:5672/%2F',
        'QUANTIK_PUBLIC_ORIGIN': 'https://quantik.io.vn',
        'QUANTIK_JWT_PRIVATE_KEY_PATH': '/run/secrets/jwt-private.pem',
        'QUANTIK_JWT_PUBLIC_KEY_PATH': '/run/secrets/jwt-public.pem',
        'QUANTIK_INTEGRATION_ENCRYPTION_KEY_PATH': '/run/secrets/integration-encryption.key',
        'QUANTIK_ADMIN_USERNAME': 'admin',
        'QUANTIK_ADMIN_PASSWORD': password_admin,
        'QUANTIK_NUMERICAL_THREADS': '1',
        'QUANTIK_ALLOW_FIXTURE_ANALYSIS': 'false',
        'QUANTIK_ALLOW_DEGRADED_ANALYSIS': 'false',
        'QUANTIK_PAYMENTS_ENABLED': 'false',
        'QUANTIK_SCHEDULED_PROVIDER_ENABLED': 'false',
        'QUANTIK_METRICS_TOKEN': secrets.token_hex(32),
        'QUANTIK_RELEASE_ID': release,
    }
    allowed = ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'VNSTOCK_API_KEY',
        'SEPAY_WEBHOOK_API_KEY', 'SEPAY_BANK_CODE', 'SEPAY_ACCOUNT_NUMBER',
        'SEPAY_ACCOUNT_NAME', 'SEPAY_TRANSFER_PREFIX', 'SEPAY_REQUIRES_TRANSFER_PREFIX')
    for name in allowed:
        value = incoming.get('integrations', {}).get(name, '')
        if not isinstance(value, str) or '\n' in value or '\r' in value:
            raise SystemExit('Invalid integration configuration')
        values[name] = value
    # Single quoted dotenv values preserve dollar signs; escape apostrophes/backslashes.
    def quote(value):
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
    with env_path.open('x', encoding='utf-8') as stream:
        os.chmod(env_path, 0o600)
        stream.write(''.join(f'{name}={quote(value)}\n' for name, value in values.items()))
    admin_path = Path('/root/quantik-initial-admin.txt')
    with admin_path.open('x') as stream:
        os.chmod(admin_path, 0o600)
        stream.write('username=admin\npassword=' + password_admin + '\n')
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
        '-out', str(key_dir / 'jwt-private.pem')], check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(key_dir / 'jwt-private.pem'), '-pubout',
        '-out', str(key_dir / 'jwt-public.pem')], check=True, capture_output=True)
    (key_dir / 'integration-encryption.key').write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b'\n')
    for path in key_dir.iterdir():
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o400)
    os.chown(key_dir, 10001, 10001)
    print('Production environment and new keys created; credentials are not printed.')


if __name__ == '__main__':
    main()

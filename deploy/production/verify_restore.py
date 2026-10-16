"""Restore a backup into an isolated, disposable database; never target the live DB."""
from pathlib import Path
import subprocess
from uuid import uuid4

prefix = ['docker','compose','--env-file','.env.production','exec','-T','postgres']
root = Path('/opt/quantik')
backup = max((root/'backups').glob('quantik-*.dump'), key=lambda p:p.stat().st_mtime)
target = 'quantik_restore_probe_' + uuid4().hex[:10]


def run(args, **kwargs):
    return subprocess.run(prefix+args, cwd=root, check=True, **kwargs)


run(['createdb','-U','postgres','--owner=quantik',target])
try:
    with backup.open('rb') as stream:
        run(['pg_restore','-U','postgres','--dbname='+target,'--exit-on-error'],stdin=stream,capture_output=True)
    query = 'SELECT version_num FROM alembic_version; SELECT count(*) FROM accounts;'
    restored = run(['psql','-U','postgres','-d',target,'-At','-c',query],capture_output=True,text=True).stdout.strip()
    source = run(['psql','-U','postgres','-d','quantik','-At','-c',query],capture_output=True,text=True).stdout.strip()
    if restored != source:
        raise SystemExit('Restore check failed: schema/account count mismatch')
    print('PASS backup restore into separate database: schema and account count match')
finally:
    run(['dropdb','-U','postgres',target])

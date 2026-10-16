#!/bin/sh
# Local recovery copy only; configure encrypted offsite replication separately.
set -eu
umask 077
cd /opt/quantik
install -d -m 700 backups
destination="backups/quantik-$(date -u +%Y%m%dT%H%M%SZ).dump"
set -C
docker compose --env-file .env.production exec -T postgres pg_dump -U postgres -d quantik -Fc > "$destination"
docker compose --env-file .env.production exec -T postgres pg_restore --list < "$destination" > /dev/null
sha256sum "$destination" > "$destination.sha256"
echo "Database backup verified: $destination"

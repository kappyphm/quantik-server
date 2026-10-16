#!/bin/sh
# Run only after private images are pulled and .env.production is created.
set -eu
cd /opt/quantik
compose() { docker compose --env-file .env.production "$@"; }
compose config --quiet
compose up -d --wait postgres rabbitmq
if compose exec -T postgres psql -U postgres -d quantik -tAc "SELECT to_regclass('public.alembic_version') IS NOT NULL" | grep -qx t; then
    sh /opt/quantik/backup.sh
fi
compose run --rm migrate
if grep -q '^QUANTIK_ADMIN_PASSWORD=' .env.production; then
    compose run --rm migrate quantik seed-admin
fi
compose run --rm migrate quantik seed-operations
# Remove bootstrap password from application environment after seeding.
sed -i '/^QUANTIK_ADMIN_PASSWORD=/d' .env.production
compose up -d --wait --wait-timeout 180
nginx -t
echo 'Services started. Activate nginx.conf only after smoke checks pass.'

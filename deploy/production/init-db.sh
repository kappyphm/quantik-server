#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 --set=app_password="$QUANTIK_DB_PASSWORD" <<'SQL'
CREATE ROLE quantik LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE quantik OWNER TO quantik;
GRANT ALL ON SCHEMA public TO quantik;
SQL

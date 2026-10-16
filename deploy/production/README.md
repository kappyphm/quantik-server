# Quantik production deployment

The 2026-10-16 trial is deployed to https://quantik.io.vn. See [the deployment and security report](DEPLOYMENT-2026-10-16.md) for evidence and remaining risks. Production mode is enabled; market/model acceptance is incomplete and payments/provider scheduling remain disabled.

## Build on the local Windows machine

Run from the workspace containing both repositories. Start Docker Desktop first. These commands build Linux amd64 images; no production credentials are needed during the build.

```powershell
docker build --platform linux/amd64 -t kappyphm/quantik:backend-RELEASE -f quantik-server/Dockerfile quantik-server
docker build --platform linux/amd64 -t kappyphm/quantik:frontend-RELEASE -f quantik-client/Dockerfile quantik-client
docker push kappyphm/quantik:backend-RELEASE
docker push kappyphm/quantik:frontend-RELEASE
```

Replace RELEASE with a new unique release identifier. Confirm the Docker Hub repository remains private before pushing. Both applications share one private repository with distinct tags. Frontend dependencies are locked by package-lock.json. requirements.resolved.txt inventories the Python packages used for the trial build; it is an inventory, not a hash-verified dependency lock. Deployed images are pinned by digest.

The backend image installs vnstock 5.0.1 from the provider's package index. Do not bake provider credentials into build arguments, environment layers, or images. The build does not run Vnstock onboarding or authenticate a provider device. Runtime images exclude pip/npm and run as non-root users.

## VPS files and initial setup

Only deployment files and pulled images are required on the VPS; no application Git checkout is needed.

```text
/opt/quantik/
  compose.yml
  .env.production             # root:root 0600; never commit/copy into an image
  secrets/                    # UID 10001, mode 0700
    jwt-private.pem           # UID 10001, mode 0400
    jwt-public.pem
    integration-encryption.key
  init-db.sh
  deploy.sh
  backup.sh
  nginx.conf
  backups/                    # root:root 0700
```

Compose reads variables with `--env-file .env.production`. The environment file is not sourced as shell code. Application containers receive an explicit variable allowlist, excluding the PostgreSQL superuser password and image/Compose credentials. Use fresh independent random database and broker passwords. The app connects as the non-superuser `quantik` database role. `init-db.sh` runs only against a newly initialized PostgreSQL volume; changing initialization environment values later does not rotate existing database passwords.

bootstrap_remote.py is a one-time VPS bootstrap helper. It reads a JSON payload on stdin, copies only explicitly allowed integration variables, generates fresh passwords/RSA/encryption keys, and refuses to overwrite an existing .env.production. It stores the initial admin credentials at `/root/quantik-initial-admin.txt` with mode 0600. The bootstrap password is removed from .env.production after seeding; normal app services never receive it. Do not re-run bootstrap or regenerate encryption keys on an existing installation. Keep an encrypted key backup separate from database backups.

Google OAuth must allow `https://quantik.io.vn/api/v1/auth/google/callback`. Frontend origin variables and backend public origin must match the public HTTPS origin. `.env` on the local development machine is unchanged.

## Pull and release

Authenticate using an appropriately scoped Docker Hub credential; prefer a read-only PAT for future VPS pulls. The trial used pull_images.py with an existing credential passed over SSH stdin and a temporary Docker configuration under `/run`, removed after pulling. It did not leave a registry credential on the VPS. Never print tokens or place them in command arguments.

Pin BACKEND_IMAGE and FRONTEND_IMAGE in .env.production to the exact pulled digest and set QUANTIK_RELEASE_ID. POSTGRES_IMAGE and RABBITMQ_IMAGE may also be pinned to their verified digests. Do not silently upgrade database/broker major versions.

```sh
cd /opt/quantik
sh deploy.sh
```

The script validates Compose, starts PostgreSQL/RabbitMQ, backs up an existing schema, explicitly migrates, optionally seeds the initial admin, seeds reviewed operation defaults, and waits for application readiness. API startup does not run migrations. Install nginx.conf only after local upstream smoke checks pass, run `nginx -t`, then reload Nginx. The preserved `/opt/quantik/nginx.maintenance.conf` can restore a 503 maintenance response while recovering a failed release.

Default services: postgres, rabbitmq, api, web, worker, dispatcher, dependencies, reconciler, scheduler. The pipelines and training profiles are intentionally off during the trial. Starting them requires provider/device verification, live data/model acceptance, and a measured RAM/concurrency budget. A single analysis worker is configured. Docker memory limits are ceilings, not reservations; simultaneously enabling all heavy processes can exceed VPS capacity.

## Verification and recovery

`python3 /opt/quantik/smoke_domain.py` checks the public domain, security boundaries, and admin login/refresh/logout using credentials read locally on the VPS. It prints no passwords/tokens and logs out its test session. It creates authentication/audit records; avoid running it repeatedly because Nginx limits login requests. Google start is checked, but Google consent and callback completion are not automated.

`sh backup.sh` makes a custom-format PostgreSQL dump, validates its table of contents, and writes a checksum. verify_restore.py restores the latest dump into a uniquely named disposable database, compares schema/account count, then removes that database. It never restores over the live database. The current backup timer runs daily around 02:00 Asia/Ho_Chi_Minh. Offsite replication, encrypted offsite key backup, retention limits, and RPO/RTO acceptance still require configuration.

For a rollback, first determine whether migrations are compatible with the prior image. Retagging/pinning an older image does not undo a schema migration. Recover incompatible schema changes from a verified backup into a separate target before switching traffic. Keep .env.production and the integration/RSA keys intact.

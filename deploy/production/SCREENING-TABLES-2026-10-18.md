# Screening top-five tables production release

Release `20261018-screening-tables-01` is live at https://quantik.io.vn/screener.
Frontend source: `7584b94`. Backend screening source: `b011676`.

## Images and rollout

- API image: `sha256:7608f657c3923b5617cc9c22e292c7b7d9939873f2f3e1b50d6b1ded43461714`.
- Web image: `sha256:ceb8d4021a61ad7c783731f722c0947b8f9fb0d176ad354ef4ab3b9165379689`.
- API base: existing production research image
  `sha256:4ab9a3ae708258afadeaec3f672cddeb95d59e7cc3319d5651a6005e2196c52e`.
  Its api.py was compared to the release: only the screening contract, result
  projection and detail route changed. The thin cached overlay copies exactly
  api.py, screening_contracts.py and screening_views.py, without installing any
  dependencies or replacing Quant/provider/worker code. The frontend image was
  built locally, transferred as a compressed Docker archive and loaded on VPS.
  No source or image was uploaded to a public container registry.
- Verified pre-release PostgreSQL backup:
  `backups/quantik-20261018T102532Z.dump`, with checksum and readable TOC.
- Only API/web were recreated. Existing workers/scheduler/provider tasks and
  their running images were preserved. No migration; schema remains `0017`.
- Environment/Compose recovery copies and image metadata are under
  `/opt/quantik/releases/20261018-screening-tables-01/`, with restricted access.

The initial attempt passed both container healthchecks but rolled back because
its script mistakenly tested `/api/v1/health/ready` (404). After correcting the
public endpoint to `/health/ready`, the final API/web rollout passed readiness.
The previous API/web images remain available for rollback; no schema changes
need reversal.

## Live verification

Authenticated production API/browser checks used the existing LIVE_SCREENING_VALIDATED
publication for 16/10/2026 and an existing enabled account with screening rights.
Only a temporary, audited authentication session was created; no account,
subscription, payment, screening publication or analysis job was created/changed.
The session was revoked after verification, including browser refresh rotations.

- Both groups return five rich rows in persisted rank order; legacy pagination
  parameters do not reveal additional rows.
- Selected-symbol detail returns assessment, three group scores and VNINDEX
  context. A symbol outside both top-five groups returns 404.
- Anonymous latest access returns 401.
- The actual public frontend displays two five-row tables with no pagination;
  clicking a symbol opens the centered detail modal.
- Desktop 1440px and mobile 390px passed: actual publication data, no page/modal
  horizontal overflow, no browser JavaScript errors.
- Quant quota counters (created, consumed, reserved) were unchanged.
- Public health readiness is 200, with API/web healthy on the pinned images.

Local validation before release: three backend screening tests, six proxy/auth
checks, ten focused desktop/mobile browser cases, TypeScript/build, and the real
Next-proxy/FastAPI/SQLite 80-symbol fixture harness at both viewports passed.
No stock ranking/scoring policy or Quant data/model gate changed in this release.

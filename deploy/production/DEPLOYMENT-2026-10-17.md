# Quant request, signup profile and Free release

Release: `20261017-quant-onboarding-03`, deployed through the existing backup,
migration and readiness procedure on 2026-10-17. This is an incremental delivery;
live Quant/model acceptance remains open.

## Source and images

- Backend: `013ab96` onboarding, `91ba831` complete history window and application
  image update, `bef7cb0` public Free offer, `fa41636` corroborated candle repair,
  `2b5aa90` daily reference reuse on resumed ingestion.
- Frontend: `c92dc39` Quant integration, `75aa604` profile/Free views,
  `46ca7a0` public Free offer.
- Backend image: `sha256:8b4a16df57c84abc2ecfec5491e5b289ffd11fb601dc2c6e4f18c4f1b167250e`.
- Frontend image: `sha256:d610f1b2a4aee374d0c90aa417e741170690edb7b456bec3270e755db77e8acd`.
- Backend uses the pinned existing runtime base
  `kappyphm/quantik@sha256:b5e63314483ccf352c0b7b4f593de5c1d10dc61a1df495f353d00b30528b41c4`.
  The image build checked every declared runtime dependency before replacing
  application code. A fresh dependency build was cancelled; it is not deployed.

Build sources are Git archives, excluding local environment files and keys.
Artifacts/logs are under `/opt/quantik/releases/20261017-quant-onboarding/`.
Pre-release environment/Compose copies and verified PostgreSQL dumps are on the
VPS. Migration 0016 completed. The first verified dump is
`backups/quantik-20261017T143231Z.dump`.

## Observed production checks

- Public HTTPS, static assets, readiness and security boundaries passed.
- Admin login, SSR gate, refresh rotation, logout/revocation and Google callback
  URI passed the existing production smoke script.
- A temporary verification account was provisioned with the real signup grant
  function. A fresh browser used a real refresh session and public HTTPS, with
  no intercepted/mocked APIs: onboarding PATCH returned200, profile fields/age
  persisted and entitlements returned Free, 3/day, 1 active request. The temporary
  account, entitlement and sessions were removed; audit events were retained with
  their actor cleared. This does not prove a human Google consent/first-signup
  round trip; that remains a separate verification item.
- Anonymous browser `/plans` showed Free, 30 days and3/day, a login link and no
  checkout button. No order POST was issued.
- Focused local checks passed for Google/profile, Free daily limits/reset/expiry,
  paid upgrade, billing and source repair. Ten desktop/mobile scenarios passed
  for onboarding and Quant request UI.

## Analysis activation and pending acceptance

The provider environment gate and audited OPERATIONS ingestion switch are now
enabled. Ingest/screen consumers and guarded train/snapshot consumers are deployed.
Observed idle train/snapshot memory was about61MiB each; this is not a peak
training/analysis concurrency acceptance test. The provider authenticates as
Golden; its universe returned1519 instruments with sector membership.

Backfill `b2ad4791-9558-457f-9ff4-fceee99c069d` targets completed session
2026-10-16. Attempt1 encountered a transient source rejection. Attempt2 stopped
on an invalid VNINDEX historical candle (2021-08-23, VCI open1329.43 > high1326.07).
KBS has a valid record that day but invalid candles on other dates. The deployed
repair policy replaces only invalid candles from a valid second-source record,
requires120 valid common anchors and no close divergence over0.5%, and records
both source-record checksums. It never calculates a replacement price.

Further source rejection was isolated to re-fetching the exchange universe.
Resumed ingestion now reuses a checksum-valid, screening-certified universe
collected that Vietnam day. Fresh references include a reference date, so an
unchanged membership still produces a new daily observation. It does not change
the universe's Quant verification/provenance or reuse yesterday's reference.

Attempt4 reused the stored daily universe (1519 members, matching checksum and
screening evidence), then hit source rejection on VNINDEX. A bounded diagnostic
isolated the failure to its primary VCI history request, before correction.
At that observation the task remained WAITING with its scheduled retry; history
and manifest counts were still zero. The deployed correction path has focused
test evidence but has not yet completed a live stored index revision.

Explicit `limit=20000` returned1299 VNINDEX rows for the tested window, compared
with1000 without it. FPT VCI/VND comparison covered1299 sessions with maximum
price divergence about0.024%; this is a sample observation, not full-market
adjustment certification. Data/model verification gates remain enabled.

Pending: finish backfill, qualify the Quant dataset, validate/publish a model,
build the market snapshot, execute a real customer request to a persisted report,
and verify daily automated processing. Do not treat container health, source
samples or the UI checks as proof of those results.

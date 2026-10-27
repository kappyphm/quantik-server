# Free five-request offer

The owner changed the requirement on 2026-10-18: every existing customer gets
30 days Free with five requests per Vietnam calendar day; future signups receive
the same five-request Free terms automatically.

The new immutable Free plan is 04dfe4bc-755c-4dc2-9891-b3c0e13c8002. The original
three-request plan stays in history. Normal seed-operations provisions the new
plan, and signup grants also ensure it exists. No schema change is required.

Operator CLI `quantik grant-free-all` executes the explicitly requested one-time
offer transaction. Each customer gets an auditable grant without an order or
payment. Earlier Free periods end at the replacement start; today's admitted
requests remain counted. Paid terms are retained and the gift follows their
existing paid periods. Disabled accounts receive the offer without being
enabled; admin identities do not use customer subscriptions.

The offer completion marker is transactional and serialized on the new plan,
so rerunning the command cannot renew existing gifts or give a second offer to
future signups. New Google login still grants only on new-account insertion.

Focused tests cover five admissions then daily denial, next-day reset, expiry,
paid upgrade, the all-customer offer, repeat execution, later signup, disabled
accounts, preserved old plan terms and no fabricated payment records.

## Production verification

- Backend source `036bb66`, image
  `sha256:8d81ddccc46ed221bedd874bc84a092a4ecd3c1445b1d825d65be489573955fa`.
  API updated without interrupting the active snapshot build; no schema change.
- Normal operator command returned created=4, existing=0. A second invocation
  returned created=0, existing=4. Production has four customer accounts and four
  offer grants, each exactly 2,592,000 seconds, daily quota five.
- Public `/api/v1/plans` returned 200 with Free duration_days=30 and
  daily_request_limit=5.
- Frontend source `935291b`, image
  `sha256:c87e359573570440c78561631cc9fdd682567bb40364494c75682d1f4af3b4c8`.
  Docker/Next production build passed. A real headless browser loaded public
  `/plans` after deployment and rendered Free / 30 NGÀY / Yêu cầu mỗi ngày / 5.
- Onboarding, Google auth and billing tests passed (four tests); revised
  all-customer idempotence/late-signup tests passed again after the completion
  marker was added (two onboarding tests).
- No Quant request was submitted under the owner's account: no matching active
  Google session was present. No request/quota was fabricated or consumed by
  the entitlement and public-page checks. Snapshot and real Quant-report
  verification remain part of the active implementation goal.

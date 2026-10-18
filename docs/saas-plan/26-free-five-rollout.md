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

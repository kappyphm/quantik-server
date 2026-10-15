# 10 — Prepaid billing implementation

Beta defaults implemented under the user's delegated product decisions. This is code evidence,
not evidence of live bank connectivity. Live acceptance gates in 06 remain applicable.

## Plans and orders

Admin creates immutable plan versions: VND integer price, duration 1–366 days, positive analysis quota,
active-job limit 1–10, implemented screening/analysis features. Versions start unavailable; only availability
can change. New prices/quotas require new versions. Existing orders pin version, price, receiving account,
account name and transfer content, unaffected by plan withdrawal or subsequent environment changes.

POST /orders requires Idempotency-Key; same account/key/plan returns the original, different plan conflicts.
Maximum five unexpired pending orders/account. Reference is QTK + 16 random hex digits; expiry 30 minutes.
Response includes amount/reference/content and QR URL. GET derives EXPIRED from deadline without writes.
Paid orders cannot be cancelled; pending cancellation is idempotent and prevents automatic later grants.

Environment: SEPAY_BANK_CODE, SEPAY_ACCOUNT_NUMBER, SEPAY_ACCOUNT_NAME, SEPAY_WEBHOOK_API_KEY;
SEPAY_TRANSFER_PREFIX for required TKP/VA content. User's `TP` was normalized to TPBank via official bank list.
Account existence/ownership and required VA prefix remain unverified. QUANTIK_PAYMENTS_ENABLED defaults
false until bank, QR/content and webhook setup are checked. No real payment has been sent or validated.

## Webhook and entitlements

API-key authentication (`Authorization: Apikey ...`) uses constant-time comparison before parsing a maximum
16 KiB JSON body. Positive SePay ID and integral positive VND are required; mock ID 0 is rejected.
Only normalized payment facts, hashes and extracted references are stored; no raw descriptions, balances,
content or raw bank references. No request credentials/payload logging is introduced.

Deduplication: SePay ID and optional hash(bank/account/bank reference). Changed replays are audited, never
applied again. Native INSERT ON CONFLICT avoids SQLite's initial-savepoint independent commit issue.
Automatic grant requires one complete reference, matching bank/account, inbound transfer, exact amount,
PENDING order, transaction within the order window and receipt before expiry. Unknown/multiple references,
late payment, wrong amount/account/direction, future timestamp or another paid-order transfer requires
reconciliation. Split/under/overpayments are not summed into a purchase.

Payment, PAID order, subscription, quota bucket and audit commit atomically before {success:true} response.
Account/order locks plus unique subscription order/payment/bucket prevent duplicate grants. Recheck state
after locking for cancellation/payment races; disabled account cannot receive a grant. Renewal queues a
full next period after the latest purchased period. No auto-debit, immediate upgrades or proration.
Job admission uses the currently valid bucket and pins it across period boundaries.

Manual apply requires admin, order ID and 10–500 character reason. Late/missing-reference payment can be
resolved, but matching bank/account, direction and exact amount remain mandatory. Cannot reassign an
already associated payment, grant cancelled orders, bypass account locks or double-apply. Same payment/
order retry is idempotent. Actor/reason audited. Refunds, provider API reconciliation and retention cleanup
remain separate work; receipt of a webhook does not imply bank refunds are implemented.

## API and evidence

Public GET /plans. Customer JWT: POST/GET /orders, GET /orders/{id}, POST /orders/{id}/cancel,
GET /payments, GET /subscription, GET /me/entitlements. Bounded cursor lists; ownership mismatch returns404.
Admin JWT: POST/GET /admin/plans, POST /admin/plans/{id}/availability, GET /admin/reconciliation,
POST /admin/reconciliation/{payment_id}/apply. Validated request schemas appear in OpenAPI.
Browser return URLs never authorize paid status.

Focused temporary-SQLite check: real JWT roles, draft/withdrawn plans, order replay/ownership, unauthorized/
mock rejection, duplicate provider/bank references, second transfer, quota and real job admission, renewal,
amount/account/direction/reference mismatches, manual late resolution, cancellation, and rollback after grant
before commit. PostgreSQL concurrency, SePay sandbox and small live payment acceptance remain unverified.

Official contracts checked:
[payload/acknowledgement](https://developer.sepay.vn/vi/sepay-webhooks/tich-hop-webhook),
[authentication](https://developer.sepay.vn/vi/sepay-webhooks/xac-thuc),
[QR/VA rules](https://developer.sepay.vn/vi/tien-ich-khac/tao-qr-code),
[bank names](https://vietqr.app/banks.json).

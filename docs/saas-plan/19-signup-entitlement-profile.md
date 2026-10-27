# Signup entitlement and profile

New Google accounts receive Free once: 30 days, 3 analysis requests per Vietnam
day, 1 active request. The grant is atomic with account creation and has a unique
signup key. Existing accounts are not retroactively enrolled. Repeat login does
not renew the grant. No purchase order, bank payment or zero-value transfer is
created. Free is system-managed and cannot be opened for purchase by an admin.

Paid purchases start immediately after any paid periods, ignoring the remaining
Free period. Applying a paid purchase closes Free; admissions already used that
day still count. Daily rollover and expiry use the existing subscription budget.

New accounts complete name, date of birth and phone number before analysis
admission. Age is derived from birth date in Vietnam time, never stored twice.
Phone numbers normalize to international form; ownership is not OTP-verified.
Verified Google email stays read-only. Subsequent Google login preserves a
customer-entered name. Customers may update their own profile; admins can read
these fields through the existing protected users list. Audit events contain no
birth date or phone payload.

Migration 0016 adds nullable profile fields, a required-profile flag for new
signups, the immutable Free plan, and payment-less subscription origins. Existing
paid periods remain valid. Downgrade requires an explicit subscription migration.

Focused checks: Google callback/exchange and repeat-login handling, profile
validation, once-only grant, daily limit/reset/expiry, paid upgrade and existing
billing tests. Production release evidence is recorded separately; isolated
tests do not prove real Google signup or live Quant execution.

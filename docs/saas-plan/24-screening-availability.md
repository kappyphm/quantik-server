# Screening availability and prototype alignment

On 2026-10-18 the user superseded the earlier 95% coverage requirement and
requested real results from the available data, matching the prototype.
The whole-market 95% gate was introduced in the new pipeline; the prototype
instead skips individual symbols with fewer than 20 bars or average volume
below 100,000 shares (docs/prototype/crawl_data.py, STEP 4).

Default manifest publication now records actual availability without a global
ratio threshold. It still requires VNINDEX and at least one equity history.
Explicit callers can request a minimum ratio; that optional bound remains
enforced. Screening requires valid index inputs and at least one eligible
ranking group. Missing/invalid stocks remain UNAVAILABLE and cannot rank.
No candles, volume, eligibility flags or coverage values are fabricated.
The actual ratio, unavailable reasons and exclusions remain in admin records.

Admin backfill first constructs an immutable manifest from compatible saved
histories and attempts screening. It fetches data when saved inputs cannot
produce a result. This allows the 830 already stored equities to be processed
without repeatedly fetching the missing symbols before publication.
Only admins may request a backfill; customers automatically read the latest
publication. Per-symbol formulas, liquidity filters and source/checksum/OHLC
checks are preserved. Existing READY publications remain immutable.

The change does not promote screening data to LIVE_VALIDATED or assert verified
corporate-action adjustments. Model/Quant checks remain independent.

Focused verification covers partial-coverage publication, exclusion of missing
stocks, unchanged ranking/criteria behavior, index-only rejection, explicit
ratio enforcement, cached manifest reuse and admin-only backfill execution.

## Production verification — 2026-10-18

- Deployed backend image `sha256:de556d1b32afab73cdbecf8be6824e373eb20f4ac02d722a5be843a1dd4e3ca6`, source `e98d9a6`; existing frontend retained.
- Requested the completed 2026-10-16 session through the authenticated admin
  backfill API. Task `b2ad4791-9558-457f-9ff4-fceee99c069d` succeeded on attempt 1
  using saved histories, without another provider download.
- Run `c71fb53a-2cc0-4b59-aa33-d1f4e473e5c0` is READY and published under ALL/PM.
  Actual coverage is 830/1519 equities (54.6412%), with screening-only validation.
- Public HTTPS customer endpoints returned 200 using an existing eligible user
  session; no account, subscription, payment or quota was changed. Latest returns
  target session 2026-10-16 and `stale=false`. Both result groups reference the
  same run and return five real stocks with prices and scores.
- OVERALL: IJC, MST, CTG, CTI, CTR. DISTRIBUTION: SCS, AAA, ACV, BCM, BSR.
  This verifies publication and customer API responses, not a new browser login
  or independent Quant/model readiness.

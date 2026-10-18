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

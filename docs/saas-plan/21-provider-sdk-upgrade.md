# Provider SDK 5.0.3 compatibility

The production backfill stopped with `VNSTOCK_AUTH_PROTOCOL` on the pinned
5.0.1 runtime. An isolated 5.0.3 container on the same VPS authenticated the
configured Golden account and fetched the real VCI universe (1,519 eligible
members), FPT from VCI/VND and VNINDEX from VCI/KBS through 2026-10-16.
Each history contained 1,298 daily bars for 2021-08-03 through 2026-10-16.

Pin 5.0.3 in dependencies and normalized metadata, with a new screening
contract policy. Older screening evidence is not silently recertified.
OHLC validation still rejects the one invalid VCI index candle and the two
invalid KBS candles; corroborated replacement remains required. This upgrade
does not certify corporate-action adjustment or authorize Quant model training.

Official release notes:
https://vnstocks.com/docs/tai-lieu/lich-su-phien-ban (2026-10-15, 5.0.3).
The Linux wheel used in the isolated probe matched its official-index SHA256:
`5ef3c863ae129e6328f0b5c3349e4603fd997798ac198b29904fb17039ca9890`.

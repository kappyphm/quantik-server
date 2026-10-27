# Customer feedback release

Production API source `a51f55b`, frontend application sources `7d5e550` and
`62abd6d`, test-output clarification `a274b39`.

- API image: `sha256:36fbf61489101c75de4fa983e25e33dff18d4d89257e4eacb87fa467b41c477f`.
- Web image: `sha256:6ecc3e9907e62f86ad0a0d4b03d5e37d72ec4ff5c54682be86c8b8436ea2a5db`.
- Backup before API/web deployment: `backups/quantik-20261017T180101Z.dump`.

Only API and web were recreated. Existing scheduled workers retained the
compatible provider/revision-selection image so active ingestion was not interrupted.
There is no schema change; public readiness remains revision 0017.

Verified locally: focused backend test checks reduced public fields, hidden
operational metadata, rejected internal groups/limits above five, rank cap and
no cursor bypass. Twenty-six desktop/mobile Playwright checks passed, plus
two full Next proxy/FastAPI browser cases with isolated fixture data. Local
production build and remote production builds passed. Fixture checks do not
prove real published-market results.

Verified on public production, without API mocks: authenticated existing user
sees the restored banner and correct subscription-required state (403), no
slot chooser/internal processing details, and no Quant session selector.
Both 1440px and 390px had no horizontal overflow. The existing account had no
subscription; its entitlements/profile were not changed for verification.
The temporary verification session was audited, logged out (204) and revoked.
No analysis request, quota consumption or payment was created.

The real TradingView iframe loaded and timeframe controls worked, but visual
inspection showed that HOSE:FPT is restricted to TradingView's own site.
This is not acceptance of embedded price candles. The final web image adds a
direct TradingView link preserving HOSE:FPT/1W and corrects the chart header
badge. These were verified again on production at 1440px and 390px.
See the restriction and official source in 22-customer-screening-presentation.md.

Five-stock production result lists remain unverified because no publication
has met the unchanged 95% source coverage threshold. No fixtures or reduced
coverage policy were introduced to manufacture a result for this release.

## Subsequent user clarifications

The customer-facing scan launch/rescan controls were reverted. Source
`00b95cd` automatically loads the latest publication; `74d3f4e` restores a
results-first layout with no launch banner or slot selector. Eight targeted
desktop/mobile checks passed for automatic polling, consistent run replacement,
access denial and waiting/error states. The deployed assets were additionally
checked in both viewports with explicitly mocked API responses: automatic
latest loads/refreshes, no scan controls, no slot selector and no scan mutations.

The user then chose temporary mock candles rather than waiting for licensed
chart integration. Source `7213010` restores labelled sample candles/volume,
daily/weekly/monthly controls and a direct real-data link. It removes the
blocked embedded widget and inactive indicator controls. Backend Quant inputs,
entitlements and admission gates are unchanged. Final web image:
`sha256:ee2a91f3770451cb27081029b29bb38ccc4ac546efca22d880c5ec59eba84262`.

Public production browser checks at 1440px/390px verified the explicit mock
label, 48 daily/12 monthly sample candles, timeframe changes, correct external
symbol/timeframe link, no iframe, no JS errors and no horizontal overflow.
Only web was recreated. Real-data chart direction is in 23-chart-delivery.md.

The focused admin-backfill test passed after pinning its API clock to the
existing deterministic test clock. It verifies denial for anonymous/customer
callers, authenticated admin execution, audited idempotency and publication
ordering. This clock change is confined to tests, with no production auth or
scheduling exception.

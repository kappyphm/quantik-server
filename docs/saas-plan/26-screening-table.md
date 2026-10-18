# 26 — Two top-five screening tables and symbol details

Latest confirmed customer request, 18/10/2026: show only five best stocks and five
distribution warnings, retaining the full information table layout and wide
modal. This supersedes the earlier full-universe pagination request. The page
contains two stacked tables: "5 mã tốt nhất" and "5 mã cần thận trọng". It has
no customer filters, sorting controls, page-size chooser or pagination.

## API and ranking

- `GET /api/v1/screenings/latest`: unchanged publication metadata/stale indicator.
- `GET /api/v1/screenings/{run_id}/results?group=OVERALL|DISTRIBUTION` returns
  `run_id`, `group`, `items`, with at most five symbols per group. Default:
  OVERALL. Query stored overall/distribution ranks 1–5, ordered by rank then
  symbol. Extra legacy pagination/filter parameters cannot reveal further ranks.
  Fewer than five eligible results is valid. Groups rank independently and may
  include the same symbol.
- `GET /api/v1/screenings/{run_id}/symbols/{symbol}` returns modal data only for
  a symbol in either top-five group. Other symbols/runs return 404; non-READY
  runs return 409. Both routes enforce active screening entitlement and the
  live-source gate. Neither consumes Quant quota.

Best uses the existing overall ranking: max of STRONG/ACCUMULATION scores with
symbol tie-break. Warnings use the existing descending DISTRIBUTION ranking.
The engine's scoring/ranking policy and its eligibility rules are unchanged.
Distribution risk does not claim to predict future price returns.

## Presentation and consistency

Rows retain symbol/exchange, closing price in VND, 1D/5D/20D changes as ratios,
raw volume and average 20-session volume in shares, volume ratio, close/high20
ratio and all three group scores with met/total/unavailable counts. Incomplete
group scores remain null. Both tables preserve backend rank order. Local
horizontal scrolling and sticky symbol/header cells support narrow screens.

The wide, centered modal retains original/adjusted signals, money flow, trend,
strength, investor/broker notes; VNINDEX level, regime, combined score, risk,
5D/20D changes, MA20 position, ATR and market strategies. Legacy market percentage
points are converted to ratios at the API boundary. News is not implemented and
is not fabricated. Internal formulas, criteria, weights, series and provenance
remain private.

Latest and both groups load together and commit atomically to UI state. Failed
refreshes retain the previous publication with a stale notice. An open modal
stays pinned to the selected run/date even when new tables arrive. Access denial
clears results. Escape/close/backdrop dismiss the native dialog and restore focus
when the trigger still exists. Morning raw volume is labelled provisional.

No database migration, provider refetch or engine recalculation is needed.
Frontend/backend must ship together because the response shape replaces the
previous paginated contract. Existing runs and their stored ranks are reused.

## Validation

Focused backend checks cover independent best/distribution selections, fixed
five-row caps even with legacy pagination parameters, rich fields, access to
either group's modal, rejection outside both groups, subscription expiry,
market-unit conversion and unchanged Quant quota. Browser fixtures cover two
five-row tables, defensive UI caps, no pagination controls, both modal paths,
centered/mobile layout, pinned publications and existing access/error states.
The real proxy/FastAPI harness keeps its isolated 80-symbol fixture universe
so it can verify results remain limited to the two top-five tables.

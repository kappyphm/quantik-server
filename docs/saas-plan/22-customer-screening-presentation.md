# Customer screening presentation

Customer feedback on 2026-10-18 changes the presentation contract to a launch
banner followed by at most five leading stocks and five distribution warnings.
Customers no longer choose a screening slot or browse every internal group.

The authenticated customer API returns only symbol, exchange, group score,
closing price in VND, daily change ratio and a short recommendation. It does
not send criteria, weights, formula details, manifests, scheduler failures,
coverage internals or provider validation records. Results are restricted to
OVERALL and DISTRIBUTION, ranks 1–5. A cursor cannot reveal further ranks.
Publication date and a generic stale indicator remain visible so older results
are not presented as current. Admin operational diagnostics remain available.

The launch button reads the latest scheduled publication; it does not enqueue
a global scan or consume a customer's Quant admission. Read-only refreshes
continue while the page is visible. No-ready-data stays a generic waiting state.

Quant automatically selects the backend's latest completed session, while
unresolved submissions retain their pinned session/idempotency key for replay.
The date stays visible in confirmation, history and the report.
Chart workspaces embed TradingView directly, keeping timeframe and indicator
controls and removing sample-price charts and the source chooser.

This presentation change does not relax data quality: the user confirmed that
the 95% screening readiness threshold must stay while additional sources are
qualified. It does not certify Quant adjustment or bypass model validation.

# Customer screening presentation

Customer clarification on 2026-10-18 restores the results-first page with
at most five leading stocks and five distribution warnings. There is no launch banner.
Customers no longer choose a screening slot or browse every internal group.

The authenticated customer API returns only symbol, exchange, group score,
closing price in VND, daily change ratio and a short recommendation. It does
not send criteria, weights, formula details, manifests, scheduler failures,
coverage internals or provider validation records. Results are restricted to
OVERALL and DISTRIBUTION, ranks 1–5. A cursor cannot reveal further ranks.
Publication date and a generic stale indicator remain visible so older results
are not presented as current. Admin operational diagnostics remain available.

The page automatically reads the latest publication when opened. Customers have
no launch or rescan button. Read-only refreshes continue while the page is visible
and do not consume a customer's Quant admission. Only authenticated admins can
request a backfill through the existing admin route. No-ready-data stays a
generic waiting state.

Quant automatically selects the backend's latest completed session, while
unresolved submissions retain their pinned session/idempotency key for replay.
The date stays visible in confirmation, history and the report.
After the restriction below was verified, the user chose explicitly labelled
sample charts temporarily. See 23-chart-delivery.md for the current delivery
and the later real-chart design. The source chooser remains removed.

Production inspection found that the embedded widget rejects HOSE:FPT with
"This symbol is only available on TradingView". An iframe and working controls
do not prove that price candles are available. TradingView's official data FAQ
confirms that this restriction covers every timeframe and personal paid plans
do not unlock widget data: https://www.tradingview.com/widget-docs/faq/data/.
An external chart link preserves the selected symbol/timeframe. Fully embedded
Vietnamese candle charts remain unverified and incomplete under that restriction.

The later user clarification removes the added 95% screening coverage gate and
restores per-symbol processing as in the prototype; see 24-screening-availability.md.
Invalid candles remain excluded. Quant adjustment and model validation remain
separate from screening publication.

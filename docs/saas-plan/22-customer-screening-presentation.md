# Customer screening presentation

The latest customer clarification on 2026-10-18 restores two top-five selections,
presented as full information tables with a wide per-symbol detail modal.
See [26-screening-table.md](26-screening-table.md) for the current contract.
The full-universe pagination request is superseded. Publication date and the
generic stale indicator remain visible. Internal formulas, criteria definitions,
manifests and operational diagnostics remain excluded from customer responses.

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

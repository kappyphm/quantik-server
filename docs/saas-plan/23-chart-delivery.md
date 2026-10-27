# Chart delivery direction

The user chose temporary, explicitly labelled mock candles on 2026-10-18 so
work on real analysis/data can continue. Sample candles are display-only;
Quant requests and reports continue to use backend data and admission gates.
Only sample-supported daily/weekly/monthly intervals are shown. Missing sample
symbols show an empty state. A direct TradingView link offers real market data.
No embedded widget or sample indicator button implies verified market output.

For the later real chart, prefer TradingView Lightweight Charts with Quantik's
own OHLCV API: candles/volume, zoom, crosshair and daily/weekly/monthly views.
The library has no bundled market data or indicators; implement and verify
SMA/EMA/RSI/MACD/Bollinger/ATR against fixed reference vectors before exposing
those controls. Expose intraday only when an actual intraday source exists.

The API must retain symbol/exchange, session dates, source/version, collection
time, VND units and adjustment status. Weekly/monthly aggregation uses first
open, maximum high, minimum low, last close and summed volume over exchange
sessions. Reject invalid/duplicate/nonfinite candles, preserve true gaps and
never reuse one symbol's sample series for another. Compare rendered OHLCV,
indicator values, session boundaries and viewport behavior with reference data.

If a full TradingView toolset is required instead, evaluate Advanced Charts
with an appropriate vendor license and a Quantik datafeed. Both libraries need
our own data; the hosted widget cannot accept a custom feed.
Official comparison: https://www.tradingview.com/charting-library-docs/latest/product-comparison/
Access conditions: https://www.tradingview.com/charting-library-docs/latest/introduction/

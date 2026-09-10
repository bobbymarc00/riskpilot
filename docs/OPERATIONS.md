# Operations status

## Scanner deployment status

The repository does not assert whether a scanner timer is currently enabled,
disabled, or active on a VPS. Check the target host before relying on scheduled
execution or Telegram candidate notifications.

The previous broad USDT allowlist path validated/fetched market data too often per cycle. Binance public REST returned `HTTP 429` throttling and later `HTTP 418`, indicating temporary IP-ban risk. This is a reliability and availability problem, not a trading-execution path: the scanner never had authority to place LIVE orders.

Before enabling or repeatedly running broad `scan` / `analyze all` commands,
verify the following safeguards on the target deployment:

1. Build the universe from the most-liquid 20 Spot USDT pairs by 24-hour quote volume.
2. Use one cached/bulk `exchangeInfo` snapshot rather than per-symbol validation requests.
3. Bound candle/book calls, add exponential backoff, and stop a cycle safely on throttling.
4. Retain the five-minute scheduler cadence and 15-minute closed-candle analysis, but notify Telegram only when a candidate meets every threshold.
5. Prove the request budget under test before enabling `spotguard-monitor.timer`.

Manual single- or small multi-symbol analysis is read-only, but it still consumes Binance public REST quota and should be used sparingly while throttling is present.

## Live Spot workflow

Every live write remains owner-confirmed: a local VPS arm and an immutable Telegram proposal are required. The supported protected sequence is: buy with OTOCO TP/SL; restore an interrupted TP/SL OCO; partial exit (cancel exact OCO → sell rounded percentage → re-arm OCO); or full protected exit (cancel exact OCO → sell protected quantity). No futures, margin, transfer, withdrawal, wallet, or arbitrary-order cancellation path is supported.

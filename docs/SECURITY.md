# RiskPilot security

## Dedicated read-only Agent OS profile

RiskPilot accepts a market read only after an explicit CODEX_HOME and neutral
workspace pass path validation. The analysis profile is dedicated and non-default. Its allowed proof is exactly one configured Binance MCP market-data call with outer `tool_execute`, inner `spot.klines`, and exact symbol/interval/limit. Shell, file, account, history, trade, and transfer events fail closed in the analysis path.

LIVE execution uses a separate dedicated OAuth profile and fixed direct MCP envelopes. It accepts only a narrow Spot allowlist: protected entry OTOCO, SELL OCO restore, cancellation of the exact active protection list, and exact MARKET SELL exit. It never exposes credentials, accepts model-selected tool arguments, or permits Futures, Margin, Convert, wallet, transfer, payment, borrowing, or withdrawal.

Providers may include the current forming kline. RiskPilot requests exactly
three raw candles once, applies the configured two-second close grace against
the post-response observation time, discards forming candles, and requires two
consecutive fresh closed candles.

- Spot-only configured allowlist; public REST is GET-only.
- Agent OS review accepts only the explicit read-only `get_book_ticker` tool from the configured Binance server; candle confirmation separately accepts exactly one `spot.klines` call.
- The model cannot select symbol, side, amount, bracket, product, server, or tool.
- Canonical payload hash, nonce/code, sender, chat, mode, state, and expiry are validated transactionally.
- Single-use claims, execution leases, idempotent fills, and restart recovery prevent replay.
- Financial values use Decimal; state changes use SQLite transactions.
- Invalid long brackets are rejected and malformed persisted brackets are never auto-closed.
- Delivery is claimed only after transport confirmation and valid controls.
- OpenClaw 2026.8.1 wraps generic presentation callbacks as `tgcb1` opaque data; an unclaimed callback is terminalized by OpenClaw before RiskPilot. RiskPilot does not treat the legacy `sg:` callback parser as proof that this installed presentation path works.
- `risk.default_order_size_usdt` is a default hypothetical/order size, never a maximum or implicit approval amount. The legacy `default_quote_amount` key remains compatible. PAPER and LIVE per-entry maxima are independently reported and validated.
- Credentials belong to external OAuth/runtime stores and never repository config.
- Revoke or restrict broad OAuth before funding a Binance account or considering LIVE use.
- Synthetic and fixture inputs are explicit opt-in demo paths; production scanning does not silently fall back to them.
- Error reporting is bounded and sanitized; credentials are never returned.
- Every unsupported write, transfer, withdrawal, wallet, Convert, Margin, Futures, generic/model-selected tool, unknown server/tool, and malformed event/result fails closed. The few supported Spot write schemas are constructed solely from immutable owner-approved proposals.
- Shell scripts quote paths and arguments and pass untrusted text as argv values, not shell code.

LIVE is locally armed only for a limited period. No automatic retry is allowed for writes. No unprotected MARKET BUY or delayed-protection fallback exists. Telegram approval cannot bypass local arm, immutable proposal validation, or Binance response checks. Binance website controls remain the final out-of-band emergency stop.

### Ambiguous-write boundary

RiskPilot does not equate a transport error with a rejected Binance order. Once a write may have reached Binance, an unusable result transitions the proposal to `RECONCILE` and blocks blind retry.

Targeted read-back verification exists for the partial-exit OCO-cancellation step: RiskPilot checks the active order list before permitting the sell to continue. Generic automatic reconciliation for every possible ambiguous financial write is intentionally unavailable in this release and requires operator inspection of exchange state.

See [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md) for the public real-funds evidence path and its explicit limitations.

# RiskPilot security

## Dedicated read-only Agent OS profile

RiskPilot accepts a market read only after an explicit CODEX_HOME and neutral
workspace pass path validation. The normal profile is dedicated and non-default.
An explicitly marked legacy OAuth profile is PAPER-only, uses a neutral workspace
outside the OAuth directory, and applies a one-process `tool_execute` approval
override without changing `config.toml`. It is rejected when LIVE is enabled,
armed, execution-ready, or scheduled for LIVE. Child processes receive a
sanitized environment with an explicit CODEX_HOME and use `-a never exec
--strict-config --ephemeral --skip-git-repo-check --json -s read-only`.
The allowed proof is exactly one completed configured Binance MCP call with
outer `tool_execute`, inner `spot.klines`, and exact symbol/interval/limit.
Shell, file, account, balance, history, trade, and transfer events fail closed.
Binance MCP read-only: Agentic account + market data; this does not claim a
market-only OAuth scope.

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
- Error reporting is bounded and sanitized; raw subprocess environments and credentials are not returned.
- Every write, cancel, transfer, withdrawal, wallet, Convert, Margin, Futures, generic `tool_execute`, unknown server/tool, and malformed event/result fails closed; no Binance write schema is guessed or enabled.
- Shell scripts quote paths and arguments and pass untrusted text as argv values, not shell code.

LIVE is disabled/disarmed. No auto-review is allowed for writes. No unprotected MARKET BUY or delayed-protection fallback exists. Telegram approval cannot bypass Binance native confirmation. Binance website controls remain the final out-of-band emergency stop for any future LIVE implementation.

# Protected-live setup (review before OAuth)

RiskPilot's live path must remain behind its policy layer. This setup only
prepares a separate Codex profile; it does not log in, connect MCP, change the
existing market-data profile, or place an order.

## Required boundary

The intended route is:

```text
Codex CLI / Telegram
  -> RiskPilot proposal and policy checks
  -> explicit owner confirmation
  -> dedicated Binance execution MCP profile
  -> protected Spot entry + native TP/SL
  -> reconciliation
```

The scanner remains read-only and only notifies when a candidate passes the
configured signal, freshness, spread, and Agent OS confirmation checks. It must
not create a LIVE proposal on its own.

## Profile preparation

```bash
./scripts/prepare-execution-profile.sh
```

The script creates `~/.codex-riskpilot-execution/config.toml` from the reviewed
template. It performs no OAuth login. The existing market-data profile is not
modified.

## Scope review before OAuth

Before connecting the execution profile, review the Agent OS/Binance permission
screen. RiskPilot constructs only the protected Spot OTOCO request. If the
provider bundles Spot and Margin in one checkbox, do not route Margin
operations through RiskPilot. Explicitly reject or disable wherever possible:

- withdrawals and deposits;
- transfers and wallet operations;
- Futures and COIN-M Futures;
- Margin and borrowing;
- Convert, payments, and unrelated products;
- arbitrary cancellation or account-management capabilities.

Do not fund or arm LIVE until the permission screen and the RiskPilot
`live status` output have been reviewed. A successful OAuth login alone is not
execution readiness.

## Current repository state

The executor builds one Spot OTOCO request: a LIMIT BUY plus pending SELL
take-profit and stop-loss legs. It is reached only after native Telegram owner
confirmation and a short-lived VPS-local arm. A malformed, failed, or
ambiguous result requires reconciliation and is never retried automatically.

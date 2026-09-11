# Protected-live setup

RiskPilot's LIVE path remains behind its deterministic policy layer. The setup below prepares and authenticates a **separate** execution profile; it does not change the market-data profile and it does not place an order by itself.

## Required boundary

```text
Codex CLI / Telegram
  -> RiskPilot proposal and policy checks
  -> explicit owner confirmation
  -> dedicated Binance execution MCP profile
  -> fixed protected Spot write
  -> response validation / targeted read-back checks
  -> EXECUTED or RECONCILE
```

The scanner has no LIVE-order authority. It is currently disabled during public-REST rate-limit/IP-ban optimization; do not enable it as part of LIVE setup.

## 1. Prepare the dedicated profile

```bash
./scripts/prepare-execution-profile.sh
```

The script creates `~/.codex-riskpilot-execution/config.toml` from the reviewed template and creates its isolated workspace. It performs no OAuth login. The existing market-data profile is not modified.

## 2. Review Binance scopes before OAuth

RiskPilot constructs only these fixed protected Spot request shapes:

- protected LIMIT BUY using OTOCO;
- SELL OCO protection restore;
- cancellation of the exact active protected OCO;
- approved MARKET SELL exit;
- protected partial exit using cancel -> verify -> sell -> re-arm.

If the provider bundles Spot and Margin in one checkbox, do not route Margin operations through RiskPilot. Explicitly reject or disable wherever possible:

- withdrawals and deposits;
- transfers and wallet operations;
- Futures and COIN-M Futures;
- Margin and borrowing;
- Convert, payments, and unrelated products;
- arbitrary account-management capabilities.

## 3. Authenticate the isolated execution profile

After reviewing the permissions, authenticate the MCP server in the dedicated Codex home rather than the default profile:

```bash
CODEX_HOME="$HOME/.codex-riskpilot-execution" codex mcp --help
CODEX_HOME="$HOME/.codex-riskpilot-execution" codex mcp login --help
```

OAuth material is stored in the dedicated runtime credential store under that profile. It is intentionally absent from the repository and must never be committed.

A successful OAuth login alone is **not** execution readiness.

## 4. Verify readiness before funding or arming

Review RiskPilot's LIVE status and configured limits before any real-money use. LIVE still requires the dedicated profile, supported Binance Spot capabilities, local time-limited arm state, an immutable approved proposal, and owner confirmation.

Protected BUY entries use a marketable `LIMIT`, not an unbounded `MARKET` order. At proposal creation RiskPilot snapshots the current ask and fixes the working LIMIT at no more than `live.entry_slippage_cap_pct` above that ask (default `0.20%`, exchange-tick aligned). Before the write, a fresh ask is read again. If it is already above the immutable approved LIMIT, execution fails closed with `SLIPPAGE_CAP_EXCEEDED` and requires a new proposal/requote.

The protected OTOCO request asks for `newOrderRespType=FULL`. If Binance and the MCP transport return an immediately filled working order in the first response, RiskPilot records exchange-reported `executedQty`, weighted `fills[]`, `commission`, and `commissionAsset`. If fill provenance is incomplete or asynchronous, the existing reconciliation path remains authoritative; RiskPilot does not invent a fill or retry the write blindly.

Do not fund or arm LIVE if the permission screen or readiness output is broader than expected.

## Current repository state

The executor contains an authenticated direct Binance Agent OS/MCP transport and fixed Spot request builders for OTOCO entry, OCO restore, exact protection cancellation, and MARKET SELL exits. The request endpoint and delegated tool arguments are derived by RiskPilot rather than supplied by model text.

Malformed, failed, or ambiguous financial-write results are never automatically retried. The service moves an unresolved outcome to `RECONCILE`. Targeted read-back verification is implemented for the protected partial-exit cancellation path; generic automatic reconciliation of every ambiguous Binance write is intentionally unavailable in this release.

For the public real-funds demonstration and the mapping between video evidence, order IDs, implementation files, and tests, see [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md).

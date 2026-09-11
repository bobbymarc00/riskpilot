# Changelog

## Unreleased

- Fixed deterministic LIVE approval/rejection presentation so native LIVE actions can no longer fall through to PAPER wording.
- Full protected exits (`remaining_quantity = 0`) are now labeled as FULL EXIT; partial exits retain PARTIAL EXIT wording.
- LIVE protected entries now use an exchange-aligned marketable BUY LIMIT with a configurable hard slippage cap (`live.entry_slippage_cap_pct`, default `0.20`).
- LIVE OTOCO writes request `newOrderRespType=FULL` so immediate fill/commission evidence can be captured from the initial Binance response when available.
- Fresh ask prices above the immutable cap are rejected before any LIVE write; verified BUY fill evidence above the approved cap is rejected into reconciliation.

## 1.0.3 — 2026-09-10

- Stabilized the Risk Policy v2 offline demo and exchangeInfo test fixtures.
- Closed every SQLite connection created by the ledger, preserving transactions.
- Synchronized package/runtime version metadata to 1.0.3.

## 1.0.2 — 2026-09-10

- Hardened the LIVE Spot execution lifecycle with fail-closed async execution handling.
- Added execution reconciliation, capability discovery, EXIT_ONLY recovery safeguards, and durable execution evidence.
- Prevented fabricated fills, fees, and realized PnL when upstream fill provenance is unavailable.

## 1.0.1 — 2026-09-08

- Added Smart Scanner for broader Binance Spot market discovery and ranking.
- Preserved the configured-symbol execution boundary and human-approved LIVE workflow.
- Added scanner safety, audit, and offline test coverage.

## 1.0.0 — 2026-09-04

- Established RiskPilot public branding while retaining required SpotGuard compatibility identifiers.
- Added PAPER scale-in and aggregate partial-close controls with replay-safe approvals.
- Restored durable typed Telegram callback actions and canonical callback dispatch.
- Tightened verified Agent OS market confirmation and kept the LIVE adapter dormant and fail-closed.

## 0.2.0 — 2026-09-02

- Replaced the incompatible direct OpenClaw MCP connection with an on-demand Codex CLI bridge.
- Added strict structured output and JSONL proof of a Binance `mcp_tool_call`.
- Added read-only sandboxing, fixed prompts, environment filtering, timeouts, and cross-MCP rejection.
- Added `agent-os status`, `market`, `demo`, and `review` CLI commands.
- Locked execution to paper mode while preserving the existing Telegram approval, replay protection, ledger, and timer.
- Kept v0.1.0 configuration backward-compatible with safe Codex defaults.

## 0.1.1 — not released

The attempted no-auth/direct OpenClaw MCP fallback did not complete the Binance MCP handshake and must not be deployed.

## 0.1.0 — 2026-09-02

- Initial deterministic Spot prefilter, Telegram proposal flow, SQLite ledger, paper execution, and systemd timer.

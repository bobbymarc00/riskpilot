# Changelog

## 1.0.3 — 2026-09-10

- Stabilized the Risk Policy v2 offline demo and exchangeInfo test fixtures.
- Closed every SQLite connection created by the ledger, preserving transactions.
- Synchronized package/runtime version metadata to 1.0.3.

## 1.0.2 — 2026-09-09

- Integrated Smart Scanner radar state and configured-symbol candidate handoff while preserving the existing execution boundary.
- Added offline scanner coverage and synchronized scanner safety documentation.

## 1.0.1 — 2026-09-08

- Added Risk Policy v2 equity-percentage sizing and limits with optional legacy absolute backstops.
- Added immutable policy snapshots and execution-time conservative equity revalidation.

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

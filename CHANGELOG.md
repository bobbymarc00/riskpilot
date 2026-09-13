# Changelog

## 1.0.5 — 2026-09-13

- Bound LIVE permission attestations and readiness proofs to the active authorization lifecycle and expiry.
- Added persisted readiness provenance and fail-closed permission diagnostics for LIVE execution.
- Added controlled refresh and durable re-validation of stale permission and decimal-transport evidence.
- Replaced lexical decimal matching with semantic finite-decimal equality for immutable LIVE approval validation.
- Hardened LIVE session accounting with symbol-isolated FIFO inventory and explicit incomplete-PnL failure handling.
- Expanded scalable equity-aware Risk Policy v2 documentation, sizing validation, and regression coverage.
- Isolated and hardened Agent OS AI REVIEW execution, serialization, provider reporting, timeout handling, and owned-process cleanup.
- Routed Telegram AI REVIEW through RiskPilot's native deterministic command boundary.
- Added the versioned `riskpilot-direct-review` OpenClaw extension and installer integration.
- Preserved successful AI REVIEW results when subsequent LIVE readiness or deterministic entry policy blocks proposal creation.
- Improved LIVE readiness, permission, policy-rejection, and AI-review presentation in English and Indonesian.
- Fixed low-price candidate reference formatting.
- Added regression coverage for LIVE authorization lifecycle, readiness wizard behavior, permission diagnostics, LIVE entry integration, Telegram direct dispatch, natural-language trading intents, review timeout cleanup, extension packaging, and symbol-isolated risk accounting.
- Preserved Spot-only execution, immutable proposals, fail-closed reconciliation, deterministic risk controls, and human-approved LIVE execution.


## 1.0.4 — 2026-09-11

- Added marketable LIMIT LIVE entries with a deterministic hard slippage cap.
- Added FULL Binance order-response handling for immediate fill and commission evidence.
- Verified real-funds protected Spot entry and OTOCO lifecycle.
- Verified LIVE partial exits at 20% and 45% of the remaining protected quantity with automatic TP/SL re-arming.
- Verified protected full exit with no orphan Spot orders.
- Fixed LIVE/PAPER approval presentation labels and full-exit presentation.
- Preserved fail-closed decimal transport, readiness, risk, exposure, and human-approval controls.

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

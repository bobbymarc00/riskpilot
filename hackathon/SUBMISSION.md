# Binance Agent OS Mini Hackathon — Track A

## Project

**RiskPilot — Agent OS-powered Spot trading copilot**

RiskPilot combines deterministic Binance Spot scanning, targeted read-only Agent OS confirmation, risk-controlled PAPER execution, and explicit Telegram approval.

## Problem

Trading assistants can blur analysis, authority, and execution. Oversized requests, ambiguous approvals, and retries after uncertain results are unsafe failure modes.

## Solution

RiskPilot uses a deterministic public-market prefilter, invokes Agent OS only for a qualifying candidate, matches exact candle evidence, applies deterministic risk policy, and binds Telegram approval to an immutable proposal. Current execution is PAPER simulation only.

## Agent OS integration

- Codex CLI invokes the official Binance Agent OS MCP endpoint for a narrow read-only `spot.klines` confirmation.
- RiskPilot verifies the MCP server, tool, arguments, status, schema, candle time, and exact OHLC values.
- The model cannot choose the symbol, product, side, amount, stop, target, or risk values.
- Unexpected MCP activity, timeout, denial, malformed output, or mismatch fails closed.

## Safety and differentiation

- Spot-only workflow; no Futures, Margin, Convert, transfer, wallet, payment, borrowing, or withdrawal paths.
- Cryptographically bound, owner/chat-bound, single-use approvals with replay protection.
- SQLite transactions, execution leases, restart recovery, aggregate positions, scale-in, partial close, and PAPER stop/target monitoring.
- LIVE is disabled, disarmed, and blocked until protected Spot write capabilities and reconciliation are independently verified.

## Submission fields

- GitHub repository: pending owner publication; suggested name `riskpilot-agent-os`
- Demo video: pending owner upload; do not invent a URL
- X submission URL: pending owner publication

## Demo scope

Use `./scripts/demo-track-a.sh` for the deterministic offline demo. It uses temporary state, explicitly labels fixtures and PAPER simulation, never sends Telegram, and never performs a Binance write. Use a separate optional Agent OS read command only when genuine read-only evidence is available.

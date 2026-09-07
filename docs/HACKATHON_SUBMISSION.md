# Hackathon submission

**Project:** RiskPilot — Agent OS-powered Spot trading copilot
**Track:** Binance Agent OS Mini Hackathon — Track A
**Categories:** Trading Workflows and Data & Analysis

## One-line pitch

Deterministic Binance Spot analysis, targeted Agent OS verification, enforceable risk limits, and cryptographically bound human approval for protected Spot workflows.

## Problem and solution

Trading assistants often mix probabilistic analysis with execution authority. RiskPilot prefilters deterministically, calls Agent OS only for the best qualified candidate, verifies independent candle evidence, applies deterministic policy, and requires explicit Telegram approval before any PAPER simulation or LIVE Spot write.

## Agent OS and architecture

Agent OS performs a fixed read-only `spot.klines` confirmation. It cannot select assets or order parameters. See [Architecture](ARCHITECTURE.md), [Security](SECURITY.md), and [Evaluation](EVALUATION.md).

## Demo steps

See [Evaluation evidence](EVALUATION.md). The offline demo is repeatable and has no network-write path.

## Submission links

- GitHub: pending owner publication
- Video: pending owner upload

## Short description

RiskPilot is a fail-closed Binance Spot copilot with deterministic analysis, Agent OS read-only verification, protected PAPER or LIVE Spot proposals, and human approval.

## Longer technical description

RiskPilot analyzes closed candles, ranks candidates, and can invoke Agent OS at most once per cycle. Exact independent candle matching gates immutable proposals. SQLite provides replay-safe approvals, leases, recovery, scale-in, partial/full exits, and PAPER protection. When locally armed, LIVE supports owner-approved protected Spot entry, TP/SL restore, partial exit, and full exit; every ambiguous result fails closed for reconciliation. The scheduled scanner is temporarily disabled while its public-REST request budget is optimized to avoid throttling/IP-ban risk.

## X draft

Built RiskPilot for Binance Agent OS Mini Hackathon Track A: deterministic Spot analysis + targeted Agent OS verification + protected Spot proposals + explicit Telegram approval. Scanner is safely paused during rate-limit optimization. Scan. Verify. Approve. Protect. Links pending.

## Known limitations

PAPER simulation is not an exchange fill. LIVE Spot is experimental and owner-confirmed; it is not financial advice. The scheduled scanner is deliberately disabled until public-REST rate-limit/IP-ban protections are verified. Links, screenshots, and video are owner-supplied.

## Repository and media fields

- GitHub repository: pending owner publication; suggested name `riskpilot-agent-os`
- Demo video: pending owner upload; do not invent a URL
- Screenshot assets: pending owner capture and sanitization

## Submission copy

**Short X draft:** RiskPilot for Binance Agent OS Mini Hackathon Track A: deterministic Spot scans, targeted read-only Agent OS verification, protected PAPER/LIVE Spot proposals, and explicit Telegram approval. Scan. Verify. Approve. Protect.

**GitHub description:** Fail-closed Binance Agent OS Spot trading copilot with deterministic signals, risk-controlled paper execution, and human approval.

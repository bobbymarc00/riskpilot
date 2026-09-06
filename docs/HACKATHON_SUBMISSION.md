# Hackathon submission

**Project:** RiskPilot — Agent OS-powered Spot trading copilot
**Track:** Binance Agent OS Mini Hackathon — Track A
**Categories:** Trading Workflows and Data & Analysis

## One-line pitch

Deterministic Binance Spot scanning, targeted Agent OS verification, enforceable risk limits, and cryptographically bound human approval in a fail-closed PAPER workflow.

## Problem and solution

Trading assistants often mix probabilistic analysis with execution authority. RiskPilot prefilters deterministically, calls Agent OS only for the best qualified candidate, verifies independent candle evidence, applies deterministic policy, and requires explicit Telegram approval before PAPER simulation.

## Agent OS and architecture

Agent OS performs a fixed read-only `spot.klines` confirmation. It cannot select assets or order parameters. See [Architecture](ARCHITECTURE.md), [Security](SECURITY.md), and [Evaluation](EVALUATION.md).

## Demo steps

See [Evaluation evidence](EVALUATION.md). The offline demo is repeatable and has no network-write path.

## Submission links

- GitHub: pending owner publication
- Video: pending owner upload

## Short description

RiskPilot is a fail-closed Binance Spot copilot with deterministic signals, Agent OS read-only verification, risk-controlled PAPER execution, and human approval.

## Longer technical description

RiskPilot scans closed candles, ranks candidates, and invokes Agent OS at most once per cycle. Exact independent candle matching gates immutable proposals. SQLite provides replay-safe approvals, leases, recovery, scale-in, partial closes, quotas, and automatic PAPER protection. LIVE refuses execution until every protected-write prerequisite is verified.

## X draft

Built RiskPilot for Binance Agent OS Mini Hackathon Track A: deterministic Spot scans + targeted Agent OS verification + risk-controlled PAPER proposals + explicit Telegram approval. Scan. Verify. Approve. Protect. Links pending.

## Known limitations

PAPER simulation is not an exchange fill. LIVE is not ready. Links, screenshots, and video are owner-supplied.

## Repository and media fields

- GitHub repository: pending owner publication; suggested name `riskpilot-agent-os`
- Demo video: pending owner upload; do not invent a URL
- Screenshot assets: pending owner capture and sanitization

## Submission copy

**Short X draft:** RiskPilot for Binance Agent OS Mini Hackathon Track A: deterministic Spot scans, targeted read-only Agent OS verification, risk-controlled PAPER proposals, and explicit Telegram approval. Scan. Verify. Approve. Protect.

**GitHub description:** Fail-closed Binance Agent OS Spot trading copilot with deterministic signals, risk-controlled paper execution, and human approval.

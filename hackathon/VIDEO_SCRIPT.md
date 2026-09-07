# Final Two-Minute LIVE Demo

This file documents the final public RiskPilot demo.

**Public video:** https://youtu.be/aYC23eYYUx0

The final recording demonstrates real-fund Binance Spot execution rather than the earlier PAPER-only demo plan.

## 0:00–0:10 — Product

Show the Telegram / RiskPilot interface.

Core message:

> RiskPilot is a Binance Agent OS-powered Spot trading copilot with deterministic market scoring, deterministic risk controls, and human-approved LIVE execution.

The architecture deliberately separates analysis, policy, approval, and execution.

## 0:10–0:25 — LIVE Account State

Request the LIVE balance and open-position state.

Example:

```text
check my balance and open position
```

Show that RiskPilot is interacting with the actual Binance Spot account rather than a fixture ledger.

Do not expose credentials, OAuth material, private account identifiers, or other sensitive information.

## 0:25–0:50 — Multi-Asset Analysis

Request:

```text
analyze XRP BNB SOL
```

Show the deterministic RiskPilot market-score ranking.

Key point:

> RiskPilot's ranking is calculated by deterministic code. Agent OS is used as a narrow confirmation/orchestration layer rather than as an unrestricted trading authority.

## 0:50–1:15 — LIVE Proposal and Entry

Create the LIVE trade proposal.

Show:

* symbol;
* amount;
* entry;
* stop;
* target;
* risk information;
* approval requirement.

Explicitly approve the proposal.

Then show the real Spot BUY execution.

Key point:

> Creating a proposal does not itself move funds. LIVE execution remains gated by deterministic policy, owner approval, and local LIVE arming.

## 1:15–1:35 — Protection

Show the position protected with TP / SL.

RiskPilot's LIVE entry path uses protected Spot execution rather than an unprotected MARKET BUY fallback.

The intended protected entry is:

```text
LIMIT BUY
+
Take Profit
+
Stop Loss
=
Spot OTOCO
```

## 1:35–1:50 — Partial Exit

Show an approved partial exit.

The protected partial-exit lifecycle is:

```text
Cancel exact active OCO
→ confirm cancellation
→ SELL approved quantity
→ calculate remainder
→ re-arm TP / SL
```

Show that the remaining position is protected again after the partial exit.

## 1:50–2:00 — Full Exit and Verification

Exit the remaining protected position.

Finish by showing the resulting transaction/order history through Binance.com.

This final Binance.com screen is out-of-band verification by the user.

Closing message:

> AI can interact. Deterministic code defines the boundaries. Humans authorize real execution.

## What the Demo Proves

The public recording demonstrates:

* LIVE Binance Spot account interaction;
* deterministic multi-asset ranking;
* LIVE proposal generation;
* human approval;
* real-fund Spot BUY;
* protected TP / SL lifecycle;
* partial exit;
* protection re-arm;
* full protected exit;
* Binance.com out-of-band verification.

## Safety Notes

The demo is intended to prove execution architecture, not profitability.

RiskPilot remains Spot-only and does not provide an execution route for Futures, Margin, borrowing, transfers, withdrawals, or unrestricted model-selected Binance writes.

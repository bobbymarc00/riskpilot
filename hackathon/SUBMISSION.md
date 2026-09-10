# Binance Agent OS Mini Hackathon — Track A

## Project

**RiskPilot — Binance Agent OS-powered Spot trading copilot**

RiskPilot combines deterministic Spot market analysis, narrowly scoped Agent OS verification, deterministic risk policy, owner-approved LIVE execution, and protected position lifecycle management.

## Public Submission

* **GitHub:** https://github.com/bobbymarc00/riskpilot
* **YouTube:** https://youtu.be/aYC23eYYUx0
* **X:** https://x.com/bobbymarc00/status/2097039814482878806

## Problem

AI trading assistants can blur analysis and authority.

A model may be useful for interaction and orchestration, but real financial execution should not depend on unrestricted model discretion.

RiskPilot therefore separates:

```text
Observation
→ Verification
→ Deterministic Policy
→ Human Authorization
→ Execution
```

## Solution

RiskPilot:

* analyzes Binance Spot market data;
* computes a deterministic native score;
* ranks supported assets;
* uses a narrow Agent OS read-only path for independent market confirmation;
* creates immutable trade proposals;
* enforces deterministic risk boundaries;
* requires explicit owner approval for LIVE writes;
* requires a local LIVE arm;
* executes only a narrow allowlist of protected Spot operations;
* preserves protection through partial and full exit workflows;
* fails closed on ambiguous execution results.

## LIVE Demo

The final public demo uses real funds.

It demonstrates:

```text
LIVE balance
→ open-position check
→ XRP / BNB / SOL analysis
→ deterministic ranking
→ LIVE proposal
→ human approval
→ real Spot BUY
→ TP / SL protection
→ partial exit
→ protection re-arm
→ full exit
→ Binance.com verification
```

The final Binance.com history view is a user-performed out-of-band verification step.

## Binance Agent OS Integration

### Read-only market confirmation

RiskPilot first computes its own deterministic score from Binance public Spot data.

A dedicated Agent OS profile is then used for a narrowly scoped read-only `spot.klines` confirmation.

RiskPilot validates the returned candle evidence before continuing.

The model does not choose:

* the score;
* the winning candidate;
* the product;
* arbitrary Binance tools;
* the approved trade amount;
* arbitrary stop/target values.

### Protected LIVE execution

LIVE uses a separate dedicated execution profile.

Supported write shapes are limited to:

* protected Spot LIMIT BUY OTOCO;
* SELL OCO protection restore;
* exact active-protection cancellation;
* approved MARKET SELL;
* protected partial exit with OCO re-arm.

No unrestricted write interface is exposed to the model.

## Safety and Differentiation

* Spot only.
* Futures disabled.
* Margin disabled.
* Transfers disabled.
* Withdrawals disabled.
* No arbitrary wallet/Convert/payment/borrowing execution route.
* Immutable proposal validation.
* Owner/chat-bound approval.
* Single-use approval claims.
* Local time-limited LIVE arm.
* Replay protection.
* Execution leases.
* Restart recovery.
* Deterministic exposure/risk limits.
* No unprotected LIVE MARKET BUY fallback.
* No automatic retry after an ambiguous LIVE write.
* Protected partial exits re-arm TP / SL for the remaining quantity.

## Public Example Risk Limits

```text
Default amount (legacy/manual):  6 USDT
Active position notional:        20% of equity
Active open exposure:            60% of equity
Active risk / position:           0.5% of equity
Active aggregate open risk:       1.5% of equity
Active free reserve:              20% of equity
Active daily loss cap:             2% of equity
Active weekly LIVE loss cap:       5% of equity
Optional legacy absolute caps:    disabled in example
Maximum economic positions:        5
Maximum active tranches:          10
Maximum successful BUY/day:     10
Pending LIVE proposals:          1
```

The 6 USDT value is the default order amount, not the maximum entry size.

## Reproducible Safe Evaluation

For judges or reviewers who do not want to connect a funded account:

```bash
./scripts/demo-track-a.sh
./scripts/verify.sh
```

The offline demo uses temporary fixture/PAPER state and performs no real Binance write.

## Scanner Status

The Smart Scanner implementation and offline/synthetic coverage are present in
the repository. Its live timer/deployment state is not claimed here because it
cannot be verified from the repository alone.

This does not affect the demonstrated manual analysis and owner-approved LIVE execution workflow.

## Final Positioning

RiskPilot is not an unrestricted autonomous trading bot.

It is a **risk-controlled agentic Spot execution architecture** where:

> **AI can interact. Deterministic code defines the boundaries. Humans authorize real execution.**

## Disclaimer

RiskPilot is experimental hackathon software, not financial advice, and provides no profit guarantee.

LIVE cryptocurrency trading involves financial risk.

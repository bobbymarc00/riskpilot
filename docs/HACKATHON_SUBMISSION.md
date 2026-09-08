# Hackathon Submission

**Project:** RiskPilot — Binance Agent OS-powered Spot trading copilot
**Track:** Binance Agent OS Mini Hackathon — Track A

## One-line pitch

RiskPilot combines deterministic Binance Spot market scoring, narrowly scoped Agent OS verification, enforceable risk controls, human-approved LIVE execution, and protected position management.

## Problem

Trading assistants can blur the boundary between analysis and execution.

A probabilistic model should not be able to:

* invent position sizes;
* bypass risk limits;
* select arbitrary Binance tools;
* silently switch PAPER activity into LIVE execution;
* retry an ambiguous financial write;
* remove protection without an explicit approved workflow.

RiskPilot separates observation, verification, policy, authorization, and mutation.

## Solution

RiskPilot uses:

1. Binance public Spot data for deterministic market analysis.
2. A canonical deterministic score engine for ranking and candidate eligibility.
3. A dedicated Binance Agent OS read-only path for narrow market confirmation.
4. Deterministic policy checks for risk and execution eligibility.
5. Immutable trade proposals.
6. Owner-bound human approval.
7. A separate dedicated Agent OS / MCP execution profile for supported LIVE Spot writes.
8. Protected TP / SL lifecycle management.
9. Fail-closed handling for ambiguous execution results: targeted read-back where implemented, otherwise `RECONCILE` with no blind retry.

The language-model-facing layer can interact, explain, and orchestrate supported workflows, but it does not define RiskPilot's market score or override deterministic execution boundaries.

## Agent OS Integration

RiskPilot uses Binance Agent OS / MCP through separate narrow paths.

### Market-data confirmation

The market-analysis path is:

```text
Binance public Spot data
        ↓
60 closed candles
        ↓
Deterministic RiskPilot score
        ↓
Candidate selection
        ↓
Dedicated Agent OS read-only spot.klines confirmation
        ↓
Exact time / OHLC validation
```

Agent OS does not calculate the RiskPilot score and does not choose the winning candidate.

### LIVE execution

LIVE execution uses a separate dedicated execution profile and authenticated direct MCP transport.

The supported write surface is deliberately restricted to the Spot operations required by RiskPilot:

* protected LIMIT BUY using OTOCO;
* SELL OCO protection restore;
* cancellation of the exact active protected OCO;
* approved MARKET SELL exit;
* protected partial exit using cancel → verify → sell → re-arm.

Every LIVE write remains gated by deterministic policy, local LIVE arming, owner-bound approval, immutable proposal validation, and Binance response checks.

## Public LIVE Demo

The public demonstration uses real funds and shows a complete protected Spot lifecycle.

### Demo flow

1. Read LIVE Spot account state.
2. Check open positions.
3. Analyze XRP, BNB, and SOL.
4. Rank assets using RiskPilot's deterministic market score.
5. Generate a LIVE proposal.
6. Approve the proposal.
7. Execute a real Spot BUY.
8. Arm TP / SL protection.
9. Perform a partial exit.
10. Re-arm protection for the remaining quantity.
11. Fully exit the position.
12. Verify the resulting history through Binance.com.

The Binance.com history view is an out-of-band user verification step. RiskPilot itself validates execution responses and protected-order state rather than claiming a general transaction-history reader.

## Submission Links

* **GitHub:** https://github.com/bobbymarc00/riskpilot
* **YouTube demo:** https://youtu.be/aYC23eYYUx0
* **X submission:** https://x.com/bobbymarc00/status/2097039814482878806
* **Public LIVE evidence bridge:** [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md)

## Demonstrated Capabilities

### LIVE

* Binance Spot account interaction
* multi-asset analysis
* deterministic ranking
* LIVE proposal generation
* explicit human approval
* real-fund Spot BUY
* protected TP / SL lifecycle
* partial exit
* protection re-arm
* full protected exit
* Binance.com out-of-band verification

### Repository / engineering

* deterministic market score engine
* Agent OS read-only market confirmation
* immutable proposals
* owner/chat-bound approval
* replay protection
* execution leases
* restart recovery
* PAPER position accounting
* scale-in
* partial/full PAPER close
* automatic PAPER TP / SL
* LIVE protected OTOCO entry
* OCO restore
* exact protected-OCO cancellation
* protected partial exit
* protected full exit
* fail-closed reconciliation
* automated safety tests

## Safety Model

RiskPilot intentionally supports Spot only.

The intended execution surface does not support:

* Futures
* Margin
* Convert
* arbitrary wallet operations
* transfers
* payments
* borrowing
* withdrawals
* unrestricted model-selected writes

LIVE execution also requires a short-lived local arm. Remote natural-language input cannot independently arm LIVE trading.

Unknown or ambiguous financial write results are not automatically retried. The protected partial-exit cancellation path includes targeted open-order read-back verification; generic automatic reconciliation for every ambiguous Binance write is intentionally unavailable in this release.

## Current Public Example Limits

The repository's example configuration includes:

```text
Default order size:             6 USDT
Minimum quote amount:           5 USDT
Maximum LIVE entry:           100 USDT
Maximum PAPER entry:          100 USDT
Maximum open exposure:        500 USDT
Maximum economic positions:     5
Maximum active tranches:        10
Minimum LIVE free reserve:       8 USDT
Maximum risk / position:         2 USDT
Maximum aggregate risk:          4 USDT
Daily realized-loss cap:         5 USDT
Weekly LIVE loss cap:           20 USDT
Successful BUY entries/day:     10
Pending LIVE proposals:          1
Minimum reward:risk:           2.0
```

`risk.default_order_size_usdt = 6` is a default amount, not the maximum permitted trade size.

## Scanner Status

The scheduled scanner implementation remains in the repository but is currently operationally disabled while Binance public REST request budgeting and rate-limit/IP-ban protections are optimized.

The scanner has no independent LIVE execution authority.

Manual analysis and owner-approved LIVE workflows remain separate.

## Reproducible Offline Evaluation

The safe local evaluation path does not require Binance OAuth and cannot make a real financial write:

```bash
./scripts/demo-track-a.sh
./scripts/verify.sh
```

This offline path proves deterministic behavior and safety invariants. The authenticated real-funds path is evidenced separately by the public demo and [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md).

The repository maps critical capabilities to implementation files and automated tests in:

* `docs/EVALUATION.md`
* `docs/ARCHITECTURE.md`
* `docs/SECURITY.md`
* `docs/SCORE_ENGINE.md`

## Known Limitations

* PAPER fills are simulations, not exchange fills.
* LIVE execution is experimental.
* Authenticated LIVE writes require a private local OAuth runtime profile and therefore are not executed by public CI.
* Generic automatic reconciliation for every ambiguous LIVE write is not implemented; unresolved outcomes remain `RECONCILE` and must not be blindly retried.
* The scheduled scanner is currently disabled during rate-limit optimization.
* LIVE operation depends on Binance account permissions, exchange filters, and supported Agent OS / MCP capabilities.
* Ambiguous LIVE results require reconciliation rather than automatic retry.
* RiskPilot provides no guarantee of trading profitability.

## Disclaimer

RiskPilot is experimental hackathon software and is not financial advice.

Cryptocurrency trading involves financial risk. Users remain responsible for reviewing and approving LIVE actions, securing their account permissions, and complying with applicable Binance terms and regional requirements.

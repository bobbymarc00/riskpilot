RiskPilot

«Binance Agent OS-powered Spot trading copilot with deterministic market scoring, deterministic risk controls, Smart Scanner discovery, and human-approved LIVE execution.»

Analyze → Rank → Risk-check → Propose → Approve → Execute → Protect → Exit

""Demo" (https://img.shields.io/badge/YouTube-LIVE%20Demo-red?logo=youtube)" (https://youtu.be/aYC23eYYUx0)
""X Submission" (https://img.shields.io/badge/X-Hackathon%20Submission-black?logo=x)" (https://x.com/bobbymarc00/status/2097039814482878806)

Binance Agent OS Mini Hackathon — Track A

Current repository release: v1.0.4

Hackathon submission snapshot: v1.0.1

- "YouTube LIVE demo" (https://youtu.be/aYC23eYYUx0)
- "X submission" (https://x.com/bobbymarc00/status/2097039814482878806)
- "Public LIVE evidence bridge" (docs/LIVE_EVIDENCE.md)
- "Evaluation evidence" (docs/EVALUATION.md)
- "Architecture" (docs/ARCHITECTURE.md)
- "Security model" (docs/SECURITY.md)
- "Risk Policy" (docs/RISKPILOT_POLICY.md)

---

What is RiskPilot?

RiskPilot is a Binance Spot trading copilot built around one principle:

«AI can interact and orchestrate. Deterministic code defines the trading boundaries. Humans authorize real execution.»

Instead of giving an LLM unrestricted trading access, RiskPilot separates:

Natural-language interaction
        ↓
Deterministic market analysis
        ↓
Agent OS confirmation
        ↓
Deterministic risk policy
        ↓
Immutable proposal
        ↓
Human approval
        ↓
Protected Spot execution
        ↓
TP / SL lifecycle
        ↓
Partial or full exit

RiskPilot supports:

- Binance Spot market analysis
- deterministic technical scoring
- multi-symbol ranking
- Smart Scanner market discovery
- Binance Agent OS / MCP confirmation
- deterministic equity-aware risk controls
- PAPER trading
- human-approved LIVE Spot execution
- protected OTOCO entries
- TP / SL protection
- partial exits
- automatic TP / SL re-arming
- protected full exits
- replay and duplicate-execution protection
- restart recovery
- fail-closed reconciliation
- Telegram interaction through OpenClaw

RiskPilot is intentionally not an unrestricted autonomous trader.

---

Hackathon submission snapshot

The final pre-deadline release was v1.0.1, published on
2026-09-08 at 22:37 UTC, before the official 23:59 UTC submission deadline.

Later releases preserve the original tagged submission history while adding
post-submission hardening, testing, and LIVE execution validation.

The hackathon snapshot remains available through the repository's tagged release history. The current "main" branch contains additional post-submission engineering work without rewriting the original submission tag.

---

Why RiskPilot?

Many AI trading demos effectively stop here:

Analyze → Buy

RiskPilot treats entry as only one stage of a controlled financial workflow:

Analyze
   ↓
Rank
   ↓
Risk Check
   ↓
Proposal
   ↓
Human Approval
   ↓
Protected LIVE Entry
   ↓
TP / SL
   ↓
Partial Exit
   ↓
Re-arm Protection
   ↓
Full Exit

Three boundaries remain separate:

1. The AI interaction layer understands user intent.
2. Deterministic code calculates scoring, sizing, and risk.
3. The human owner authorizes every LIVE financial write.

A model cannot override RiskPilot's execution policy.

---

Architecture

flowchart LR
    A[Binance Public Spot Data] --> B[Smart Scanner / Deterministic Scoring]
    B --> C[Candidate Ranking]
    C --> D[Agent OS Read-only Confirmation]
    D --> E[Deterministic Risk Engine]
    E --> F[Immutable Proposal]
    F --> G[Human Approval]
    G --> H{Execution Mode}
    H -->|PAPER| I[PAPER SQLite Ledger]
    H -->|LIVE| J[Binance Agent OS / MCP]
    J --> K[Protected OTOCO Entry]
    K --> L[TP / SL Protection]
    L --> M[Partial / Full Exit]
    M --> N[Protection Reconciliation]

The security boundary can also be summarized as:

Observation
    ↓
Verification
    ↓
Policy
    ↓
Authorization
    ↓
Mutation

---

Separation of Responsibilities

Agent-facing layer

The OpenClaw / agent-facing layer handles:

- n

# Binance Agent OS through Codex CLI

OpenClaw is the Telegram router. It is not the Binance MCP client. The supported path is:

```text
OpenClaw -> RiskPilot CLI -> Codex CLI (one process) -> binance-mcp-server
```

Codex runs only for an explicit demo or `AI REVIEW` callback and exits after the read. The five-minute monitor never launches Codex.

## Allowed commands

- Readiness: `riskpilot --json agent-os status`
- Natural analysis: `riskpilot --json analyze SYMBOL`
- Verified market read: `riskpilot --json agent-os market --symbol SYMBOL`
- Demo candidate: `riskpilot --json agent-os demo --symbol SYMBOL --notify`
- Candidate review: `riskpilot --json agent-os review --candidate ID --notify`

Never invoke `codex exec` directly and never pass arbitrary prompts to Codex. RiskPilot supplies a fixed prompt and JSON Schema, launches Codex with a read-only sandbox and a sanitized environment, and rejects the result unless the JSONL transcript contains a successful `mcp_tool_call` from `binance-mcp-server`. It also rejects shell, file-change, and web-search events.

Every read-only probe is stored as a redacted local audit event: outcome class, timestamp, fixed argv, sanitized environment-key list, process exit code, output hashes, and MCP event summary. Prompts, OAuth material, raw tool output, and credentials are never stored. A successful probe is usable for at most 300 seconds; configured/authenticated MCP status alone is never usable readiness.

Natural analysis first uses the same Binance public REST prefilter as the scheduled scan: request 61 candles, discard any forming candle, validate them, and score the newest 60 with `scheduled-signal-v1`. This REST prefilter is the score source, not a fallback. It then uses the exact, verified `binance-marketdata` generic outer `tool_execute` call for inner target `spot.klines` as the confirmation source. One Agent OS request obtains three raw candles because providers may include a forming candle; after a two-second close grace it retains the two newest validated closed candles and verifies the latest OHLC against the REST prefilter. The profile must be an explicit dedicated CODEX_HOME with a neutral workspace; no default or inherited profile is used. Balance, account, order, cancel, transfer, Convert, Futures, Margin, wallet, payment, and on-chain calls are outside v0.2. Binance MCP read-only: Agentic account + market data.

The returned summary is untrusted text for display only. Deterministic code owns symbol allowlisting, price validation, spread/drift checks, quote amount, stop/target references, expiry, approval token, and replay protection.

## Paper-only release boundary

RiskPilot uses live Agent OS market data but simulates execution. If any command, config, or response suggests `mode: live`, stop and report a configuration mismatch. Do not fund an account for this release and do not claim that an order was placed.

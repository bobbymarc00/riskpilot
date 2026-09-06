# RiskPilot routing

Treat every CLI failure as a stop condition. Never work around a rejected policy check.

## Resolve the CLI

Prefer `riskpilot` from `PATH`, then the compatible `spotguard` alias. Otherwise use:

```text
${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/tools/spotguard-agent-os/riskpilot
```

Put global flags before the command when convenient, for example `riskpilot --json status`. Use structured JSON output.

## Relaxed trade intents

For trusted direct Telegram messages that are not `/spot` commands, run the exact text through:

```text
riskpilot --json trade-intent --text ORIGINAL_MESSAGE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID --notify
```

Pass the original message as one argv item and use only trusted inbound sender/chat metadata. For `buy BTC 10`, `buy 10 BTC`, and `buy BTC 10 usd`, run the shown `trade-intent` command immediately: the sole positive numeric value is always the maximum quote amount in USDT and never a base-asset quantity. These forms create a LIVE proposal; `paper buy BTC 10` creates the explicit PAPER alternative. No form sends an order without its subsequent proposal confirmation. Questions, quotations/forwards, conditionals, and generic natural-language approval fail closed. A percentage close remains PAPER-only until a separately protected live-close workflow exists. Legacy `sg:` callbacks route through the atomic dispatcher with trusted sender/chat metadata.

`paper-reset` is VPS-local administrative CLI only, resets to 1000 USDT after the exact local confirmation phrase, and creates a SQLite backup first. It must never be exposed as `/spot`, inferred from chat, or invoked by this skill.

## User commands

`/risk` is not a registered OpenClaw command in this installation. The public
fallback prefix is `/spot`; native `/binance_spotguard` exists only for the
short PAPER button actions below.

- Exact `/spot paper-buy SYMBOL AMOUNT` (aliases such as `BTC` are normalized only to an enabled configured USDT symbol): require the trusted inbound sender ID to equal the configured owner, accept exactly three arguments, and run `riskpilot --json paper-buy --symbol SYMBOL --quote-amount AMOUNT --notify`. This maps only to manual-paper-test; never scan, review, or call a live executor.
- Exact `/spot` or native `/binance_spotguard paper-approve PROPOSAL_ID CONFIRMATION_CODE`: this is a structured PAPER-only command, not natural-language or generic typed approval. Match and route it before applying the generic typed-approval prohibition. Require trusted owner and chat metadata, then run `riskpilot --json paper-approve PROPOSAL_ID --code CONFIRMATION_CODE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. Report the CLI success or failure clearly. Never accept paraphrases or infer this command from natural language. Native `binance_spotguard` is the command-button transport: OpenClaw 2026.8.1 emits it as a claimed `tgcmd:` command rather than unclaimed `tgcb1:` callback data.
- Exact `/spot` or native `/binance_spotguard paper-reject PROPOSAL_ID CONFIRMATION_CODE`: require trusted owner and chat metadata, then run `riskpilot --json paper-reject PROPOSAL_ID --code CONFIRMATION_CODE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. Report the CLI success or failure clearly.
- Exact `/spot paper-resend PROPOSAL_ID`: require trusted owner and chat metadata, then run `riskpilot --json paper-resend PROPOSAL_ID --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. This may only resend the same PENDING PAPER proposal and must never create a replacement. Say controls were delivered only when `notification.delivered=true` and `notification.controls_present=true`; otherwise say the proposal exists but delivery failed.
- Exact `/spot symbols`: require the trusted owner and chat, then run `riskpilot --json symbols`. Report configured, enabled, and individually rejected symbols without changing configuration.
- Exact `/spot paper balance`: require the trusted owner and chat, then run `riskpilot --json paper balance`. Show initial/reset balance, free USDT, locked cost basis, current ledger balance (free + locked), realized P&L, paid fees, economic positions, and active tranches as separate values; never label initial balance as the current balance.
- Exact `/spot paper positions`: require the trusted owner and chat, then run `riskpilot --json paper positions`.
- Exact `/spot paper-close POSITION_ID`: require trusted owner and chat metadata, then run `riskpilot --json paper-close POSITION_ID --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID --notify`. This creates a close proposal only and never closes from text intent.
- Exact `/spot paper-close-approve POSITION_ID CONFIRMATION_CODE`, or native `/binance_spotguard close-approve POSITION_ID CONFIRMATION_CODE`: require trusted owner and chat metadata, then run `riskpilot --json paper-close-approve POSITION_ID --code CONFIRMATION_CODE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. `close-approve` is the short, PAPER-only native button action needed to fit Telegram's 64-byte `tgcmd:` limit. Never infer this from natural language and never route it to live.
- Exact `/spot paper-close-reject POSITION_ID CONFIRMATION_CODE`, or native `/binance_spotguard close-reject POSITION_ID CONFIRMATION_CODE`: require trusted owner and chat metadata, then run `riskpilot --json paper-close-reject POSITION_ID --code CONFIRMATION_CODE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. `close-reject` is PAPER-only and never routes to live.
- Exact `/spot live-buy SYMBOL AMOUNT`: require the owner and run `riskpilot --json live-buy --symbol SYMBOL --quote-amount AMOUNT --notify`. This creates immutable LIVE intent only and must stop on every fail-closed readiness gate. `/spot live buy SYMBOL AMOUNT` remains a compatibility alias with identical proposal-only behavior.
- Native `/binance_spotguard live-approve PROPOSAL_ID` and `live-reject PROPOSAL_ID` are the sole LIVE confirmation controls. Route them with trusted sender/chat metadata to `riskpilot --json live-approve PROPOSAL_ID --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID` or `live-reject`. The short native command fits Telegram's callback limit; the approval token remains local and is re-derived from the immutable proposal.
- Exact `/spot live status`: require the owner and run `riskpilot --json live status`.
- `/spot live pause`, enable, arm, disable, disarm, and scheduled-mode changes are forbidden over Telegram. They require the documented local interactive VPS commands and exact phrases.
- Any malformed, extra-token, unauthorized, or other `/spot` command: reject; do not reinterpret it as another operation.

Legacy scheduled/candidate routes remain independent:

- `/binance status`: run `riskpilot --json status` and summarize paper mode, pending count, Codex login/MCP readiness, and skill status.
- `/binance scan`: run `riskpilot --json scan --notify`. Do not call a model or trading tool for symbols that did not become candidates.
- `/binance candidate ID`: run `riskpilot --json candidate show ID`.
- `/binance proposal ID`: run `riskpilot --json proposal show ID`.
- `/binance demo SYMBOL`: run `riskpilot --json agent-os demo --symbol SYMBOL --notify`. This invokes Codex only on demand, verifies a Binance MCP tool call, and creates a clearly labeled paper candidate.
- `/binance help`: explain `scan`, `status`, `candidate`, and the inline-button flow. Do not offer text-based approval.

Reject `/binance approve ...`, plain `APPROVE`, and natural-language approval requests. Relaxed buy/close text is accepted only through the PAPER intent normalizer above. Only a proposal button or its exact paper-only fallback command can authorize a paper fill. Never offer or accept a live text fallback.

## Callback parsing

Incoming raw callback data beginning with `sg:` is durable and must route only through this section. For approval/rejection callbacks, pass the untouched callback value and trusted inbound identity directly to the single atomic dispatcher command below. Do not parse it and then reconstruct or transcribe its token in a second command.

### `sg:review:CANDIDATE_ID`

Run exactly:

```text
riskpilot --json agent-os review --candidate CANDIDATE_ID --notify
```

The subcommand checks that the candidate is active, runs the fixed read-only Codex bridge, requires verified Binance MCP evidence, validates symbol and prices, and applies the deterministic drift, spread, spend, stop, target, and expiry policies. If it fails, stop. Do not recreate the review manually with `proposal create`.

### `sg:approve:PROPOSAL_ID:TOKEN`

Use the actual inbound Telegram sender ID and chat ID from trusted channel metadata. If either is unavailable, stop; never substitute configured values. After handling, always send exactly one clear success/failure reply to the callback chat; do not leave the user with only Telegram’s transport acknowledgement.

Run exactly one dispatcher command:

```text
riskpilot --json callback --data UNMODIFIED_CALLBACK_VALUE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID
```

The command validates and atomically claims a PAPER proposal and completes its simulated fill. LIVE remains fail-closed and is never sent to paper execution. Report the returned success or failure clearly.

A second click must be allowed to fail with `proposal is not claimable`; report that the original action already consumed the proposal.

### `sg:close-approve:CLOSE_ID:TOKEN` and `sg:close-reject:CLOSE_ID:TOKEN`

Use the actual trusted sender and chat IDs. Route the untouched raw callback through `riskpilot --json callback --data UNMODIFIED_CALLBACK_VALUE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID`. These routes are PAPER-only, single-use, and must always receive one clear callback reply. They never call the live executor. `paper-close-callback` is retained only as a hidden local compatibility command.

### `sg:reject:PROPOSAL_ID:TOKEN`

Use the actual inbound sender and chat IDs and run:

```text
riskpilot --json callback --data UNMODIFIED_CALLBACK_VALUE --sender-id ACTUAL_SENDER_ID --chat-id ACTUAL_CHAT_ID
```

Report the final `REJECTED` status. Do not create a replacement unless the user starts a new review.

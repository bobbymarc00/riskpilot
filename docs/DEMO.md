# RiskPilot reproducible demo

This demonstration is designed for international hackathon judges. It uses a fresh temporary SQLite ledger, fixture market data, and disabled Telegram delivery.

```bash
./scripts/demo-track-a.sh
```

Expected behavior:

1. A deterministic PAPER candidate is created from synthetic Spot candles.
2. A PAPER buy proposal is created for the configured symbol and amount.
3. The simulated fill completes only after the local demo approval path.
4. A partial PAPER close is proposed and approved.
5. A request above the configured entry maximum is rejected before a fill.
6. The final ledger balance and audit-event count are printed.

The script reports `PASS` only when every step uses temporary state. It does not require credentials, a Telegram account, a Binance account, or a network write. It places no real orders.

For a read-only Agent OS demonstration, configure an explicit permitted profile and run:

```bash
./riskpilot --config /path/to/sanitized-config.json --json analyze BTC
```

The bridge accepts exactly one `tool_execute` call for `spot.klines`, requires three raw candles, discards a forming candle, and uses the newest two verified closed candles.

Presentation examples are deterministic:

```text
analyze BNB
paper buy 40 usd of BNB
```

Reason: normal trading intent defaults to a protected LIVE proposal. The word paper must be explicit when the documentation intends to demonstrate PAPER execution.
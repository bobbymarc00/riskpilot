# RiskPilot canonical score engine

## Audited scheduled-signal pipeline

The active scheduled route is `riskpilot scan`, invoked by the monitor timer. For each configured symbol it validates Binance Spot `exchangeInfo`, requests 61 candles from Binance public REST `/api/v3/klines`, removes any forming candle, validates interval continuity and OHLC values, and scores the newest 60 closed candles. The configured `market.lookback` value is not used by this legacy route; the active compatibility boundary is the constants `PREFILTER_REQUEST_COUNT = 61` and `ANALYSIS_CANDLE_COUNT = 60`.

The native score is the sum below, clamped to `[0, 100]`:

- `ema_fast_above_ema_slow`: +25 when EMA(12) > EMA(26).
- `close_above_ema_fast`: +10 when close > EMA(12).
- `rsi_14`: +20 for 50–68 inclusive; otherwise +10 for 45–72 inclusive; otherwise -10 above 78; otherwise 0.
- `momentum_3_pct`: +15 for 0.05–3.0 inclusive; otherwise +7 when positive; otherwise 0.
- `volume_ratio_20`: +15 at or above 1.2; otherwise +8 at or above 0.8; otherwise 0.
- `atr_pct_in_range`: +10 for 0.15–3.0 inclusive.
- `breakout_20`: +5 when the close is at least the maximum of the preceding 20 closes.

The default score threshold is 70 (`market.min_signal_score`). Passing the score threshold alone does not make a candidate. The existing bullish gate also requires: configured symbol, EMA(12) > EMA(26), close > EMA(12), RSI(14) from 45 through 72, positive 3-candle momentum, and ATR percentage no greater than `risk.max_stop_distance_pct`.

Threshold-passing signals are subject to per-symbol cooldown/deduplication. Signals are ordered by score and candle close time, descending. Before Agent OS is invoked, the scheduled PAPER route checks active-tranche capacity and the daily successful-entry quota. Only the selected signal is confirmed. Binance Agent OS must return a fresh closed candle with exactly matching open time and OHLC; failure produces no candidate. A confirmed signal is then persisted as a candidate.

The later Agent OS review/proposal stage reads the candidate, obtains the verified closed-candle price, and applies spread, drift, quote-size, ATR stop-distance, and reward/risk checks. PAPER entry guards cover active tranches, distinct economic positions and scale-ins, exposure, per-position and aggregate risk, free balance, daily fills, daily realized loss, minimum notional/quantity steps, and pending proposals. Proposal creation and execution remain separate, stateful approval stages.

## Refactoring boundary

`score_engine.score_market` is the pure canonical candle-to-score/decision function. `indicators.analyze` retains the original floating-point indicator calculations and now obtains its total from the same named component function. `strategy.evaluate` retains the scheduled `Signal` identity and fingerprint but delegates candidate eligibility to the canonical predicate. The scheduler keeps its prior side-effect order. Manual analysis adds a read-only orchestration layer around the same REST candles, canonical score result, Agent OS confirmation, and hypothetical policy projection.

Manual analysis does not call proposal creation, reserve funds, expire proposals, consume quota, or write balances/positions. The market score is never adjusted for an open position or another execution constraint.

## Compatibility evidence

`tests/fixtures/scheduled_score_golden.json` records pre-refactor UP, DOWN, FLAT, exact-threshold, below-threshold, and tie expectations. `tests/test_score_engine_unification.py` proves those native scores, contributions, threshold results, classifications, and candidate decisions remain unchanged and that scheduler/analyze results match for the same candles.

The unused `market.lookback` setting is a pre-existing configuration defect/ambiguity. It is documented but intentionally not corrected in this change because making it authoritative would alter scheduled scores.

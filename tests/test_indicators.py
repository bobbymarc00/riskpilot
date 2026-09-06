from __future__ import annotations

import unittest

from spotguard.indicators import analyze, atr, ema, rsi
from spotguard.market import synthetic_bullish_klines


class IndicatorTests(unittest.TestCase):
    def test_synthetic_fixture_is_a_reviewable_bullish_candidate(self) -> None:
        snapshot = analyze(synthetic_bullish_klines())
        self.assertGreaterEqual(snapshot.score, 70)
        self.assertGreater(snapshot.ema_fast, snapshot.ema_slow)
        self.assertGreater(snapshot.close, snapshot.ema_fast)
        self.assertGreaterEqual(snapshot.rsi_14, 45)
        self.assertLessEqual(snapshot.rsi_14, 72)
        self.assertGreater(snapshot.volume_ratio_20, 1.2)

    def test_indicator_primitives_are_finite(self) -> None:
        klines = synthetic_bullish_klines()
        closes = [item.close for item in klines]
        self.assertEqual(len(ema(closes, 12)), len(closes))
        self.assertTrue(0 <= rsi(closes) <= 100)
        self.assertGreater(atr(klines), 0)


if __name__ == "__main__":
    unittest.main()

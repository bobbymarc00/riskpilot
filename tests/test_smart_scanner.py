from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "riskpilot-smart-scanner.py"
spec = importlib.util.spec_from_file_location("riskpilot_smart_scanner", MODULE_PATH)
smart = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = smart
spec.loader.exec_module(smart)


def ticker(symbol: str, q: float, trades: int, change: float = 0.0, bid: float = 100.0, ask: float = 100.05):
    return smart.Ticker(symbol, 100.0, 99.0, 102.0, 98.0, bid, ask, q, trades, change, 1)


def feature(symbol: str, q: float, trades: int, *, r5=0.0, r15=0.0, q5=0.0, n5=0.0, new=False):
    f = smart.Feature(ticker(symbol, q, trades), symbol[:-4], new_symbol=new)
    f.ret_5m = r5
    f.ret_15m = r15
    f.quote_delta_5m = q5
    f.quote_delta_15m = q5 * 2
    f.trades_delta_5m = n5
    f.trades_delta_15m = n5 * 2
    return f


class SmartScannerTests(unittest.TestCase):
    def test_active_count_keeps_large_buffer(self):
        cfg = dict(smart.DEFAULTS)
        rows = [feature(f"X{i}USDT", 2_000_000 + i, 10_000 + i, r5=3, r15=5, q5=500_000, n5=1000) for i in range(30)]
        smart.score_features(rows)
        self.assertLessEqual(smart.choose_active_count(cfg, rows, 100), 48)
        self.assertEqual(smart.choose_active_count(cfg, rows, 610), 24)
        self.assertEqual(smart.choose_active_count(cfg, rows, 760), 0)

    def test_bucket_design_prevents_volume_only_selection(self):
        cfg = dict(smart.DEFAULTS)
        rows = []
        # Liquid majors.
        for i in range(12):
            rows.append(feature(f"BIG{i}USDT", 100_000_000 - i * 1_000_000, 1_000_000 - i * 1000))
        # Lower-volume assets with strong fresh acceleration.
        for i in range(20):
            rows.append(feature(f"HYPE{i}USDT", 2_000_000 + i * 10_000, 50_000 + i,
                                r5=2 + i / 10, r15=4 + i / 10, q5=800_000 + i * 1000, n5=5000 + i * 10))
        smart.score_features(rows)
        selected = smart.select_universe(rows, cfg, 36)
        active = [f.ticker.symbol for f in selected["active"]]
        self.assertEqual(len(active), 32 if len(rows) == 32 else 36)
        self.assertTrue(any(s.startswith("HYPE") for s in active))
        self.assertLessEqual(len(selected["core"]), 8)

    def test_first_boot_does_not_label_every_symbol_new(self):
        cfg = dict(smart.DEFAULTS)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "baseline.json"
            rows = [ticker("AAAUSDT", 1_000_000, 1000), ticker("BBBUSDT", 2_000_000, 2000)]
            baseline = smart.load_or_create_baseline(path, rows, cfg, 1000.0)
            self.assertEqual(baseline, {"AAAUSDT", "BBBUSDT"})

    def test_429_cooldown_and_418_disable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "circuit.json"
            smart.trip_circuit(path, smart.RateLimitError(429, 120), 1000.0)
            state = smart.read_json(path, {})
            self.assertFalse(state["disabled"])
            self.assertEqual(state["cooldown_until"], 1120.0)
            smart.trip_circuit(path, smart.RateLimitError(418, None), 2000.0)
            state = smart.read_json(path, {})
            self.assertTrue(state["disabled"])


if __name__ == "__main__":
    unittest.main()

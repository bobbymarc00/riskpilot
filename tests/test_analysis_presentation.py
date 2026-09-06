from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from spotguard.codex_bridge import CodexAgentOSBridge
from spotguard.config import load_settings
from spotguard.market import Kline
from spotguard.presentation import momentum_strength, render
from spotguard.service import SpotGuard
from tests.helpers import write_config


def evidence(symbol: str, previous_close: str, current_open: str, current_high: str,
             current_low: str, current_close: str, volume: str) -> dict:
    previous = Kline(0, float(previous_close), float(previous_close) + 2, float(previous_close) - 3,
                     float(previous_close), 10.0, 899999)
    current = Kline(900000, float(current_open), float(current_high), float(current_low),
                    float(current_close), float(volume), 1799999)
    return {"symbol": symbol, "interval": "15m", "candle": current, "candles": [previous, current],
            "raw_candle_count": 3, "closed_candle_count": 2, "used_candle_count": 2,
            "discarded_open_candle_count": 1, "latest_closed_at": "1970-01-01T00:29:59Z",
            "freshness_seconds": 7, "mcp_server": "binance-marketdata",
            "mcp_tool_call": {"tool": "tool_execute"}, "observed_at": "1970-01-01T00:30:00Z",
            "elapsed_ms": 1}


class AnalysisPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.settings = load_settings(write_config(self.root))

    def test_formulae_signal_support_range_volume_and_freshness(self):
        bridge = CodexAgentOSBridge(self.settings)
        with patch.object(bridge, "confirm_candle", return_value=evidence("BTCUSDT", "100", "101", "106", "98", "105", "123.45")):
            result = bridge.analyze_market("BTC")
        values = result["indicators"]
        self.assertEqual(values["open_to_close_change"], "4.0")
        self.assertEqual(values["open_to_close_change_pct"], str(Decimal("4") / Decimal("101") * 100))
        self.assertEqual(values["closed_candle_change"], "5.0")
        self.assertEqual(values["closed_candle_change_pct"], "5.00")
        self.assertEqual(values["high_low_range"], "8.0")
        self.assertEqual(values["local_support"], "97.0")
        self.assertEqual(values["local_resistance"], "106.0")
        self.assertEqual(values["latest_volume"], "123.45")
        self.assertEqual(result["signal"], "UP")
        self.assertEqual(result["freshness_seconds"], 7)
        self.assertEqual(result["discarded_open_candle_count"], 1)

    def test_strength_boundaries_and_signal_states(self):
        self.assertEqual([momentum_strength(v) for v in ("0.0999", "0.10", "0.2999", "0.30", "0.7499", "0.75")],
                         ["very_weak", "weak", "weak", "moderate", "moderate", "strong"])
        bridge = CodexAgentOSBridge(self.settings)
        for close, expected in (("100", "FLAT"), ("99", "DOWN")):
            with self.subTest(close=close), patch.object(bridge, "confirm_candle", return_value=evidence("BTCUSDT", "100", "100", "101", "98", close, "1")):
                self.assertEqual(bridge.analyze_market("BTC")["signal"], expected)

    def test_localized_single_presentation_has_matching_facts(self):
        bridge = CodexAgentOSBridge(self.settings)
        with patch.object(bridge, "confirm_candle", return_value=evidence("BTCUSDT", "100", "101", "106", "98", "105", "123.45")):
            result = bridge.analyze_market("BTC")
        for locale, expected in (("en", "Latest closed-candle volume"), ("id", "Volume candle tertutup terbaru")):
            text = render(result, locale)
            self.assertIn(expected, text); self.assertIn("105", text); self.assertIn("123", text)
            self.assertIn("Forming" if locale == "en" else "berjalan", text)
            self.assertIn("Binance Agent OS", text); self.assertIn("PAPER", text)

    def test_five_symbol_exact_ranking_ties_open_position_and_no_proposal(self):
        settings = replace(self.settings, market=replace(self.settings.market,
            symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")))
        service = SpotGuard(settings)
        rows = {
            "BTCUSDT": evidence("BTCUSDT", "100", "100", "102", "99", "100.1000", "10"),
            "ETHUSDT": evidence("ETHUSDT", "100", "100", "103", "99", "100.10000", "11"),
            "BNBUSDT": evidence("BNBUSDT", "100", "100", "104", "99", "100.10001", "12"),
            "SOLUSDT": evidence("SOLUSDT", "100", "100", "105", "99", "99.9", "13"),
            "XRPUSDT": evidence("XRPUSDT", "100", "100", "106", "99", "100.1000", "14"),
        }
        produced = []
        bridge = CodexAgentOSBridge(settings)
        native_scores = {"BTCUSDT": 90, "ETHUSDT": 90, "BNBUSDT": 95, "SOLUSDT": 80, "XRPUSDT": 90}
        for symbol, item in rows.items():
            with patch.object(bridge, "confirm_candle", return_value=item):
                row = bridge.analyze_market(symbol)
            row.update({"source": "binance-public-rest-prefilter", "native_signal_score": native_scores[symbol],
                "score_engine_version": "scheduled-signal-v1", "score_components": [],
                "threshold_result": {"minimum_signal_score": 70, "passed": True},
                "candidate_eligible": True, "candidate_ineligibility_reason": None,
                "agent_os_confirmation": {"matched": True}, "hypothetical_order_amount_usdt": "6",
                "hypothetical_order_amount_source": "configured default_order_size_usdt",
                "execution_eligibility": {"eligible": True, "blocking_reason": None},
                "paper_position_open": symbol == "ETHUSDT"})
            produced.append(row)
        with patch.object(service, "_analyze_one", side_effect=produced) as call, \
             patch.object(service.ledger, "list_paper_positions", return_value=[{"symbol": "ETHUSDT"}]):
            compared = service.compare_markets(["BTC", "ETH", "BNB", "SOL", "XRP"])
        self.assertEqual(call.call_count, 5)
        self.assertEqual([row["symbol"] for row in compared["ranking"]], ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"])
        self.assertTrue(next(row for row in compared["ranking"] if row["symbol"] == "ETHUSDT")["paper_position_open"])
        self.assertFalse(compared["proposal_created"])
        self.assertEqual(service.ledger.list_proposals(), [])
        self.assertIn("#1 · BNBUSDT", render(compared, "en"))
        self.assertIn("OPEN", render(compared, "en"))

    def test_fresh_process_renderer_and_installed_skill_analysis_route(self):
        source = self.root / "result.json"
        payload = {"source": "binance_agent_os_mcp", "symbol": "BTCUSDT", "interval": "15m",
                   "candle": {"close": "105"}, "latest_closed_at": "t", "freshness_seconds": 7, "signal": "UP",
                   "indicators": {"open_to_close_change": "4", "open_to_close_change_pct": "3.960396", "closed_candle_change": "5", "closed_candle_change_pct": "5", "high_low_range": "8", "local_support": "97", "local_resistance": "106", "latest_volume": "123"}}
        source.write_text(json.dumps(payload), encoding="utf-8")
        command = [sys.executable, "-c", "import json,sys; from spotguard.presentation import render; print(render(json.load(open(sys.argv[1])), 'en'))", str(source)]
        completed = subprocess.run(command, text=True, capture_output=True, check=True)
        self.assertIn("Latest closed-candle volume", completed.stdout)
        source_skill = Path("skills/binance-spotguard/SKILL.md").read_text(encoding="utf-8")
        route = "--json analyze SYMBOL"
        self.assertIn(route, source_skill)
        installed_path = Path.home() / ".openclaw/workspace/skills/binance-spotguard/SKILL.md"
        if installed_path.exists():
            self.assertIn(route, installed_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from spotguard.config import load_settings
from spotguard.market import Kline, synthetic_bullish_klines
from spotguard.presentation import render
from spotguard.score_engine import SCORE_ENGINE_VERSION, score_market
from spotguard.service import SpotGuard
from spotguard.strategy import evaluate
from tests.helpers import write_config


GOLDEN = json.loads(Path("tests/fixtures/scheduled_score_golden.json").read_text(encoding="utf-8"))


def golden_candles(name: str) -> list[Kline]:
    base = synthetic_bullish_klines(60, final_close_ms=GOLDEN["base"]["final_close_ms"])
    spec = GOLDEN["cases"][name]
    if spec.get("last_candle") == "legacy":
        return base
    previous = base[-2]
    close = previous.close + float(spec["close_delta"])
    if spec["volume_ratio"] == "legacy":
        volume = base[-1].volume
    else:
        volume = sum(item.volume for item in base[-21:-1]) / 20 * float(spec["volume_ratio"])
    current = Kline(base[-1].open_time, previous.close, max(previous.close, close) + .2,
                    min(previous.close, close) - .2, close, volume, base[-1].close_time)
    return base[:-1] + [current]


def validation() -> dict[str, str]:
    return {"market_step_size": "0.0001", "min_notional": "5", "status": "TRADING"}


def confirmation(symbol: str, candle: Kline) -> dict:
    return {"symbol": symbol, "interval": "15m", "expected_open_time": candle.open_time,
            "matched": True, "actual_open_time": candle.open_time, "observed_at": "now",
            "tool_name": "spot.klines", "failure_reason": None, "elapsed_ms": 1}


class ScoreEngineGoldenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.settings = load_settings(write_config(self.root))
        self.service = SpotGuard(self.settings)

    def test_pre_refactor_golden_scores_components_and_decisions(self):
        for name in ("up", "down", "flat", "threshold", "below_threshold"):
            with self.subTest(name=name):
                expected = GOLDEN["cases"][name]
                result = score_market(self.settings, "BTCUSDT", golden_candles(name))
                self.assertEqual(result.native_total_score, expected["score"])
                self.assertEqual(result.market_signal_classification, expected["classification"])
                self.assertEqual(result.threshold_passed, expected["threshold_passed"])
                self.assertEqual(result.candidate_eligible, expected["candidate_eligible"])
                self.assertEqual([item.contribution for item in result.components], expected["contributions"])
                self.assertEqual(result.snapshot.score, expected["score"])
                self.assertEqual(bool(evaluate(self.settings, "BTCUSDT", result.snapshot)), expected["candidate_eligible"])
        tied = [score_market(self.settings, symbol, golden_candles("up"))
                for symbol in GOLDEN["cases"]["tie"]["symbols"]]
        tied.sort(key=lambda item: (-Decimal(item.native_total_score), item.symbol))
        self.assertEqual([item.symbol for item in tied], GOLDEN["cases"]["tie"]["ordered_symbols"])
        self.assertTrue(all(item.candidate_eligible for item in tied))

    def test_scheduled_and_analyze_paths_are_identical_for_same_candles(self):
        candles = golden_candles("up")
        with patch("spotguard.service.validate_spot_symbol", return_value=validation()), \
             patch("spotguard.service.fetch_klines", return_value=candles), \
             patch.object(self.service, "_confirm_prefilter_candle", return_value=confirmation("BTCUSDT", candles[-1])):
            analyzed = self.service.analyze_market("BTC")
        scheduled = score_market(self.settings, "BTCUSDT", candles).to_dict()
        self.assertEqual(analyzed["score_engine_version"], scheduled["score_engine_version"])
        self.assertEqual(analyzed["native_signal_score"], scheduled["native_total_score"])
        self.assertEqual(analyzed["score_components"], scheduled["components"])
        self.assertEqual(analyzed["threshold_result"], scheduled["threshold_result"])
        self.assertEqual(analyzed["market_signal_classification"], scheduled["market_signal_classification"])
        self.assertEqual(analyzed["candidate_eligible"], scheduled["candidate_eligible"])

    def test_exact_native_ranking_and_symbol_tie_break(self):
        base = self._row("ETHUSDT", 95, eligible=True)
        btc = self._row("BTCUSDT", Decimal("95"), eligible=True)
        low = self._row("BNBUSDT", Decimal("94"), eligible=True)
        with patch.object(self.service, "_analyze_one", side_effect=[base, btc, low]):
            result = self.service.compare_markets(["ETH", "BTC", "BNB"])
        self.assertEqual([row["symbol"] for row in result["market_score_ranking"]],
                         GOLDEN["cases"]["tie"]["ordered_symbols"] + ["BNBUSDT"])

    def test_market_score_is_independent_of_execution_eligibility(self):
        candles = golden_candles("up")
        with patch("spotguard.service.validate_spot_symbol", return_value=validation()), \
             patch("spotguard.service.fetch_klines", return_value=candles), \
             patch.object(self.service, "_confirm_prefilter_candle", return_value=confirmation("BTCUSDT", candles[-1])), \
             patch.object(self.service, "_hypothetical_paper_eligibility", return_value={"eligible": False, "blocking_reason": "blocked", "hypothetical_only": True}):
            blocked = self.service.analyze_market("BTC")
        self.assertEqual(blocked["native_signal_score"], 95)
        self.assertTrue(blocked["candidate_eligible"])
        self.assertFalse(blocked["execution_eligibility"]["eligible"])

    def test_explicit_or_configured_default_amount(self):
        candles = golden_candles("up")
        def run(amount):
            with patch("spotguard.service.validate_spot_symbol", return_value=validation()), \
                 patch("spotguard.service.fetch_klines", return_value=candles), \
                 patch.object(self.service, "_confirm_prefilter_candle", return_value=confirmation("BTCUSDT", candles[-1])):
                return self.service.analyze_market("BTC", amount)
        self.assertEqual(run(Decimal("8.25"))["hypothetical_order_amount_usdt"], "8.25")
        default = run(None)
        self.assertEqual(default["hypothetical_order_amount_usdt"], str(self.settings.risk.default_order_size_usdt))
        self.assertEqual(default["hypothetical_order_amount_source"], "configured default_order_size_usdt")

    def test_analysis_does_not_mutate_ledger_objects_or_quota(self):
        candles = golden_candles("up")
        before = copy.deepcopy(self.service.ledger.counts())
        balance = copy.deepcopy(self.service.ledger.paper_balance())
        quota = self.service.ledger.successful_paper_entries("2026-09-06")
        with patch("spotguard.service.validate_spot_symbol", return_value=validation()), \
             patch("spotguard.service.fetch_klines", return_value=candles), \
             patch.object(self.service, "_confirm_prefilter_candle", return_value=confirmation("BTCUSDT", candles[-1])), \
             patch("spotguard.service.build_proposal", side_effect=AssertionError("analysis must not build a proposal")):
            result = self.service.analyze_market("BTC")
        self.assertFalse(result["proposal_created"])
        self.assertEqual(self.service.ledger.counts(), before)
        self.assertEqual(self.service.ledger.paper_balance(), balance)
        self.assertEqual(self.service.ledger.successful_paper_entries("2026-09-06"), quota)

    def test_blocked_winner_and_eligible_alternative(self):
        winner = self._row("ETHUSDT", 95, eligible=False, reason="position limit reached")
        alternative = self._row("BTCUSDT", 90, eligible=True)
        with patch.object(self.service, "_analyze_one", side_effect=[winner, alternative]):
            result = self.service.compare_markets(["ETH", "BTC"])
        self.assertEqual(result["highest_market_score_symbol"], "ETHUSDT")
        self.assertEqual(result["highest_market_score_ineligibility_reason"], "position limit reached")
        self.assertEqual(result["highest_scoring_eligible_candidate"], "BTCUSDT")

    def test_confirmation_source_labels_and_one_call_per_source(self):
        candles = golden_candles("up")
        validate = Mock(return_value=validation()); fetch = Mock(return_value=candles)
        confirm = Mock(return_value=confirmation("BTCUSDT", candles[-1]))
        with patch("spotguard.service.validate_spot_symbol", validate), \
             patch("spotguard.service.fetch_klines", fetch), \
             patch.object(self.service, "_confirm_prefilter_candle", confirm):
            result = self.service.analyze_market("BTC")
        validate.assert_called_once_with(self.settings, "BTCUSDT")
        fetch.assert_called_once_with(self.settings, "BTCUSDT", validated=True)
        confirm.assert_called_once_with("BTCUSDT", candles[-1], record_event=False)
        self.assertEqual(result["score_source"], "Binance closed-candle prefilter")
        self.assertEqual(result["confirmation_source"], "Binance Agent OS")
        self.assertEqual(result["score_engine_version"], SCORE_ENGINE_VERSION)

    def test_scheduler_and_analysis_use_same_agent_os_confirmation_gate(self):
        candles = golden_candles("up")
        gate = confirmation("BTCUSDT", candles[-1])
        with patch("spotguard.service.validate_spot_symbol", return_value=validation()), \
             patch("spotguard.service.fetch_klines", return_value=candles), \
             patch.object(self.service, "_confirm_prefilter_candle", return_value=gate) as confirm_gate:
            scheduled = self.service.scan(symbols=["BTCUSDT"])
            analyzed = self.service.analyze_market("BTCUSDT")
        self.assertEqual(scheduled["results"][0]["confirmation"], analyzed["agent_os_confirmation"])
        self.assertEqual(confirm_gate.call_count, 2)
        self.assertEqual(confirm_gate.call_args_list[0].args, ("BTCUSDT", candles[-1]))
        self.assertEqual(confirm_gate.call_args_list[1].kwargs, {"record_event": False})

    def test_en_id_structured_parity_and_canonical_rendering(self):
        row = self._row("BTCUSDT", 95, eligible=True)
        row.update({"score_engine_version": SCORE_ENGINE_VERSION, "candle": {"close": "101"},
                    "indicators": {"closed_candle_change_pct": "1", "closed_candle_change": "1",
                    "open_to_close_change": "1", "open_to_close_change_pct": "1", "high_low_range": "2",
                    "local_support": "99", "local_resistance": "102", "latest_volume": "10"},
                    "interval": "15m", "latest_closed_at": "t", "freshness_seconds": 1,
                    "threshold_result": {"minimum_signal_score": 70, "passed": True},
                    "score_components": [{"name": "breakout_20", "value": True, "weight": 5, "contribution": 5}],
                    "hypothetical_order_amount_usdt": "6", "hypothetical_order_amount_source": "configured default_order_size_usdt",
                    "paper_position_open": False})
        en, indonesian = render(row, "en"), render(row, "id")
        for raw in ("95", SCORE_ENGINE_VERSION, "breakout_20", "6"):
            self.assertIn(raw, en); self.assertIn(raw, indonesian)
        self.assertIn("Score source: Binance closed-candle prefilter", en)
        self.assertIn("Sumber skor: Penyaring awal candle tertutup Binance", indonesian)

    def test_fake_openclaw_installed_skill_delivers_canonical_text_verbatim(self):
        source = Path("skills/binance-spotguard/SKILL.md").read_text(encoding="utf-8")
        directive = "Display the returned `presentation.text` verbatim"
        self.assertIn(directive, source)
        installed_path = Path.home() / ".openclaw/workspace/skills/binance-spotguard/SKILL.md"
        if installed_path.exists():
            self.assertIn(directive, installed_path.read_text(encoding="utf-8"))
        canonical = "line one\nscore_engine_version=scheduled-signal-v1\nline three"
        fake_openclaw_delivery = lambda payload: payload["presentation"]["text"]
        self.assertEqual(fake_openclaw_delivery({"presentation": {"text": canonical}}), canonical)

    @staticmethod
    def _row(symbol: str, score, *, eligible: bool, reason: str | None = None) -> dict:
        return {"symbol": symbol, "native_signal_score": score, "candidate_eligible": True,
                "candidate_ineligibility_reason": None, "agent_os_confirmation": {"matched": True},
                "execution_eligibility": {"eligible": eligible, "blocking_reason": reason},
                "score_engine_version": SCORE_ENGINE_VERSION, "candle": {"close": "101"},
                "indicators": {"closed_candle_change_pct": "1", "closed_candle_change": "1",
                    "open_to_close_change": "1", "open_to_close_change_pct": "1", "high_low_range": "2",
                    "local_support": "99", "local_resistance": "102", "latest_volume": "10"},
                "interval": "15m", "latest_closed_at": "t", "freshness_seconds": 1,
                "signal": "UP", "threshold_result": {"minimum_signal_score": 70, "passed": True},
                "score_components": [], "hypothetical_order_amount_usdt": "6",
                "hypothetical_order_amount_source": "configured default_order_size_usdt",
                "paper_position_open": False}


if __name__ == "__main__":
    unittest.main()

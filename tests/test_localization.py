"""Localization fixtures exercise real policy and persistence with offline markets."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from string import Formatter
from unittest.mock import patch

from spotguard.cli import main
from spotguard.config import ConfigError, load_settings
from spotguard.db import Ledger
from spotguard.intent import normalize_paper_intent
from spotguard.market import SpotMarketSnapshot, scaled_synthetic_klines
from spotguard.presentation import catalog, compact_number, detect_locale, error_text, number, render, translate
from spotguard.service import SpotGuard
from tests.helpers import write_config

ROOT = Path(__file__).resolve().parents[1]
OWNER = "123456789"
MARKET = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"), Decimal("5"), Decimal("0.001"), "TRADING", 1)


def analysis(symbol="BNBUSDT"):
    score = 95 if symbol in {"BNB", "BNBUSDT"} else 90
    return {"source": "binance-public-rest-prefilter", "symbol": symbol, "interval": "15m",
            "candle": {"close": "80029.29"}, "latest_closed_at": "2030-01-01T00:00:00Z",
            "freshness_seconds": 3, "signal": "UP",
            "native_signal_score": score, "score_engine_version": "scheduled-signal-v1",
            "score_components": [{"name": "breakout_20", "value": True, "weight": 5, "contribution": 5}],
            "threshold_result": {"minimum_signal_score": 70, "passed": True},
            "candidate_eligible": True, "candidate_ineligibility_reason": None,
            "agent_os_confirmation": {"matched": True},
            "hypothetical_order_amount_usdt": "6",
            "hypothetical_order_amount_source": "configured default_order_size_usdt",
            "execution_eligibility": {"eligible": True, "blocking_reason": None},
            "paper_position_open": False,
            "indicators": {"closed_candle_change": "987.89", "closed_candle_change_pct": "1.25",
                           "open_to_close_change": "500", "open_to_close_change_pct": "0.6285",
                           "high_low_range": "1200", "local_support": "78000",
                           "local_resistance": "80500", "latest_volume": "42"}}


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = write_config(self.root)
        self.settings = load_settings(self.config)
        self.service = SpotGuard(self.settings)
        self.spot = patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET).start()
        self.klines = patch("spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(100)).start()
        self.addCleanup(patch.stopall)

    def create(self, locale):
        self.service.locale = locale
        return self.service.create_manual_buy_proposal("BTC", Decimal("25"), notify=True, dry_run=True)

    def approve(self, made, service=None):
        service = service or self.service
        proposal = made["proposal"]
        code = service.signer.paper_confirmation_code(proposal["canonical_json"])
        return service.paper_text_approve(proposal["id"], code, OWNER, OWNER)

    def test_catalog_key_parity_duplicates_usage_and_placeholders(self):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                self.assertNotIn(key, result, f"duplicate key: {key}")
                result[key] = value
            return result
        en, localized = [json.loads((ROOT / "src/spotguard/locales" / f"{locale}.json").read_text(), object_pairs_hook=unique) for locale in ("en", "id")]
        self.assertEqual(set(en), set(localized))
        source = "\n".join(path.read_text() for path in (ROOT / "src/spotguard").glob("*.py"))
        for key in en:
            self.assertIn(f'"{key}"', source, f"unused key: {key}")
            fields = lambda text: {(name, spec, conversion) for _, name, spec, conversion in Formatter().parse(text) if name is not None}
            self.assertEqual(fields(en[key]), fields(localized[key]), key)
            for locale in ("en", "id"):
                translate(key, locale, **{name: "fixture" for name, _, _ in fields(en[key])})

    def test_required_utterances_and_aliases(self):
        fixtures = {
            "en": ["analyze BNB", "analize BTC", "analyse BTC", "analixe BTC", "analuze XRP", "buy BNB 40 usd", "sell 40% BNB", "close all BTC positions", "show balance and positions", "which coin is best for entry?"],
            "id": ["analisis BNB", "analisa BTC", "beli BNB 40 usdt", "jual 40% BNB", "tutup semua posisi BTC", "cek saldo dan posisi", "koin mana yang paling bagus untuk entry?"],
        }
        for locale, utterances in fixtures.items():
            for utterance in utterances:
                with self.subTest(utterance=utterance):
                    self.assertEqual(detect_locale(utterance, "id" if locale == "en" else "en"), locale)
                    self.assertNotIn(normalize_paper_intent(utterance, self.settings.market.symbols)["action"], {"clarify", "invalid", "info"})

    def test_mixed_input_previous_chat_and_fallback_without_calls(self):
        with patch("subprocess.run", side_effect=AssertionError("locale detection must not spawn")):
            self.assertEqual(detect_locale("buy BNB cek saldo", "en"), "id")
            self.assertEqual(detect_locale("BNB 40", "en"), "en")
            self.assertEqual(detect_locale("BNB 40"), "id")
            self.assertEqual(detect_locale("BNB 40", default="en"), "en")
        self.service.select_locale("cek saldo")
        self.assertEqual(SpotGuard(self.settings).locale, "id")
        self.assertEqual(SpotGuard(self.settings).select_locale("BNB"), "id")

    def test_localized_number_does_not_change_decimal(self):
        value = Decimal("80029.29")
        self.assertEqual(number(value, "en"), "80,029.29")
        self.assertEqual(number(value, "id"), "80.029,29")
        self.assertEqual(value, Decimal("80029.29"))
        self.assertEqual(compact_number("769.7870903534004520238339840", "en", 8), "769.78709035")
        self.assertEqual(compact_number("769.7870903534004520238339840", "id", 8), "769,78709035")

    def test_analysis_uses_one_existing_call_in_each_language(self):
        for locale in ("en", "id"):
            self.service.locale = locale
            with patch.object(self.service, "_analyze_one", return_value=analysis()) as read:
                result = self.service.analyze_market("BNB")
            read.assert_called_once_with("BNB", None)
            self.assertEqual(result["presentation"]["locale"], locale)
            self.assertIn(number("80029.29", locale), result["presentation"]["text"])
            self.assertEqual(result["candle"]["close"], "80029.29")
        explicit = normalize_paper_intent("analyze BTC 8.25 usdt", self.settings.market.symbols, "en")
        self.assertEqual(explicit, {"action": "analysis", "symbol": "BTC", "quote_amount": "8.25"})

    def test_multi_symbol_ranking_consistent_locale_and_no_detection_call(self):
        for locale in ("en", "id"):
            self.service.locale = locale
            with patch.object(self.service, "_analyze_one", side_effect=lambda symbol, amount: analysis(symbol)) as read:
                result = self.service.compare_markets(["BTC", "BNB"])
            self.assertEqual(read.call_count, 2)
            self.assertEqual([row["symbol"] for row in result["ranking"]], ["BNBUSDT", "BTCUSDT"])
            self.assertTrue(result["presentation"]["text"].startswith(translate("analysis.score.ranking", locale,
                eligible="BNBUSDT, BTCUSDT", strongest="BNBUSDT", winner="BNBUSDT", winner_reason=translate("analysis.none", locale))))
            self.assertIn("BTCUSDT", result["presentation"]["text"])

    def test_comparison_failure_never_returns_partial_ranking(self):
        with patch.object(self.service, "_analyze_one", side_effect=[analysis(), ValueError("unavailable")]):
            with self.assertRaises(ValueError):
                self.service.compare_markets(["BTC", "BNB"])

    def test_buy_minimum_maximum_and_risk_refusals_in_both_locales(self):
        for locale in ("en", "id"):
            self.service.locale = locale
            for amount, key in (("1", "policy.min_order"), ("101", "policy.max_order_exceeded"), ("0", "policy.non_positive")):
                with self.assertRaises(Exception) as caught:
                    self.service.create_manual_buy_proposal("BTC", Decimal(amount))
                self.assertEqual(caught.exception.presentation_locale, locale)
                expected = translate(key, locale, amount="101", maximum="100") if key == "policy.max_order_exceeded" else translate(key, locale)
                self.assertEqual(error_text(caught.exception, locale), expected)
            guarded = SpotGuard(replace(self.settings, paper=replace(self.settings.paper, max_risk_per_position_usdt=Decimal("0.001"))), locale=locale)
            with self.assertRaises(Exception) as caught:
                guarded.create_manual_buy_proposal("BTC", Decimal("25"))
            self.assertEqual(error_text(caught.exception, locale), translate("policy.risk_limit", locale))
        self.assertEqual(self.service.ledger.list_proposals(), [])

    def test_proposals_and_buttons_preserve_actions_and_numeric_payloads(self):
        made = self.create("id")
        proposal = made["proposal"]
        self.assertEqual(self.service.ledger.presentation_locale("proposal", proposal["id"]), "id")
        from spotguard.telegram import proposal_message
        code = self.service.signer.paper_confirmation_code(proposal["canonical_json"])
        en_text, en_buttons = proposal_message(proposal, "unused", code, "en")
        id_text, id_buttons = proposal_message(proposal, "unused", code, "id")
        self.assertEqual([b["command"] for b in en_buttons], [b["command"] for b in id_buttons])
        self.assertEqual([b["label"] for b in en_buttons], ["APPROVE PAPER", "REJECT PAPER"])
        self.assertEqual([b["label"] for b in id_buttons], ["SETUJUI PAPER", "TOLAK PAPER"])
        self.assertNotEqual(en_text, id_text)
        self.assertNotIn("locale", proposal["canonical"])

    def test_approval_and_replay_keep_proposal_locale_after_restart(self):
        made = self.create("id")
        self.service.select_locale("show balance")
        restarted = SpotGuard(self.settings)
        done = self.approve(made, restarted)
        self.assertEqual(done["presentation"]["locale"], "id")
        self.assertIn(translate("approval.paper.filled", "id", symbol="BTCUSDT", quantity="0,24975", price="100", spend="25", fee="0,00025", fee_asset="BTC", stop="99,27436788", target="101,45126424", order_id=done["proposal"]["execution_summary"]["paper_order_id"], position_id=done["proposal"]["execution_summary"]["position_id"]), done["presentation"]["text"])
        with self.assertRaises(Exception) as caught:
            self.approve(made, restarted)
        self.assertEqual(caught.exception.presentation_locale, "id")

    def test_rejection_in_fresh_cli_process_keeps_original_locale(self):
        made = self.create("id"); proposal = made["proposal"]
        self.service.select_locale("show balance")
        code = self.service.signer.paper_confirmation_code(proposal["canonical_json"])
        command = [sys.executable, "-m", "spotguard", "--config", str(self.config), "--json", "paper-reject", proposal["id"], "--code", code, "--sender-id", OWNER, "--chat-id", OWNER]
        result = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["presentation"]["locale"], "id")
        replay = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
        self.assertEqual(replay.returncode, 2)
        self.assertEqual(json.loads(replay.stdout)["presentation"]["locale"], "id")

    def test_expiry_keeps_original_locale(self):
        made = self.create("id")
        with self.service.ledger.connect() as connection:
            connection.execute("UPDATE proposals SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (made["proposal"]["id"],))
        with self.assertRaises(Exception) as caught:
            self.approve(made, SpotGuard(self.settings))
        self.assertEqual(error_text(caught.exception, caught.exception.presentation_locale), translate("approval.expired", "id"))

    def test_partial_full_close_buttons_and_restart(self):
        self.approve(self.create("en"))
        for locale, percentage, key in (("id", "40", "position.close.partial"), ("en", "100", "position.close.full")):
            self.service.locale = locale
            position = self.service.ledger.list_paper_positions(True)[0]
            made = self.service.create_paper_close_proposal(position["id"], OWNER, OWNER, Decimal(percentage), notify=True, dry_run=True)
            buttons = made["notification"]["payload"]["presentation"]["blocks"][0]["buttons"]
            self.assertEqual(buttons[0]["label"], translate("button.approve_close", locale))
            self.assertEqual(buttons[1]["label"], translate("button.reject_close", locale))
            command = buttons[0]["action"]["command"]
            self.service.select_locale("show balance" if locale == "id" else "cek saldo")
            done = SpotGuard(self.settings).approve_paper_close_by_position(position["id"], command.split()[-1], OWNER, OWNER)
            self.assertEqual(done["presentation"], {"locale": locale, "text": translate(key, locale)})
        self.assertEqual(self.service.ledger.list_paper_positions(True), [])

    def test_close_rejection_keeps_locale(self):
        self.approve(self.create("en"))
        self.service.locale = "id"
        position = self.service.ledger.list_paper_positions(True)[0]
        made = self.service.create_paper_close_proposal(position["id"], OWNER, OWNER, notify=True, dry_run=True)
        code = made["notification"]["payload"]["message"].split()[-1]
        done = SpotGuard(self.settings).reject_paper_close_by_position(position["id"], code, OWNER, OWNER)
        self.assertEqual(done["presentation"]["locale"], "id")
        self.assertEqual(len(self.service.ledger.list_paper_positions(True)), 1)

    def test_balance_positions_numbers_and_language(self):
        self.approve(self.create("en"))
        for locale in ("en", "id"):
            service = SpotGuard(self.settings, locale=locale)
            result = service.paper_status()
            self.assertIn(translate("position.title", locale), result["presentation"]["text"])
            self.assertIn(compact_number(result["balance"]["locked_cost_basis_usdt"], locale, 4), result["presentation"]["text"])
            self.assertEqual(result["presentation"]["locale"], locale)

    def test_balance_presentation_reconciles_raw_values_in_both_locales(self):
        self.approve(self.create("en"))
        for locale in ("en", "id"):
            report = SpotGuard(self.settings, locale=locale).paper_status()
            balance = report["balance"]
            self.assertEqual(Decimal(balance["free_usdt"]) + Decimal(balance["locked_cost_basis_usdt"]), Decimal(balance["current_ledger_balance_usdt"]))
            self.assertEqual(sum((Decimal(row["quote_spent"]) for row in report["positions"]), Decimal("0")), Decimal(balance["locked_cost_basis_usdt"]))
            text = report["presentation"]["text"]
            for field in ("BTCUSDT", compact_number(balance["current_ledger_balance_usdt"], locale, 4), compact_number(report["positions"][0]["final_stop"], locale, 8)):
                self.assertIn(field, text)
            self.assertNotRegex(text, r"\d{12,}")

    def test_legacy_database_and_unsigned_locale_compatibility(self):
        made = self.create("en")
        canonical = made["proposal"]["canonical_json"]
        with self.service.ledger.connect() as connection:
            connection.execute("DROP TABLE presentation_locales")
        legacy = SpotGuard(replace(self.settings, default_locale="id"))
        self.assertEqual(legacy.ledger.get_proposal(made["proposal"]["id"])["canonical_json"], canonical)
        self.assertEqual(self.approve(made, legacy)["presentation"]["locale"], "id")

    def test_scheduled_proposal_uses_latest_destination_chat_locale(self):
        candidate = self.service.create_demo_candidate("BTCUSDT", Decimal("100"))["candidate"]
        self.service.ledger.remember_chat_locale(OWNER, "id")
        made = self.service.create_proposal(candidate["id"], Decimal("99.9"), Decimal("100"), Decimal("6"), "Offline fixture")
        self.assertEqual(self.service.ledger.presentation_locale("proposal", made["proposal"]["id"]), "id")

    def test_cli_original_utterance_errors_and_analysis(self):
        for text, locale in (("analyze BNB", "en"), ("analisis BNB", "id")):
            with patch("spotguard.service.SpotGuard._analyze_one", return_value=analysis()) as read:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["--config", str(self.config), "paper-intent", "--text", text, "--sender-id", OWNER, "--chat-id", OWNER])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["presentation"]["locale"], locale)
            self.assertEqual(read.call_count, 1)

    def test_xrp_buy_uses_temporary_paper_state_and_keeps_detailed_fill_locale(self):
        xrp = SpotMarketSnapshot("XRPUSDT", Decimal("1.409"), Decimal("1.41"), Decimal("1.41"), Decimal("5"), Decimal("1"), "TRADING", 1)
        raw = json.loads(self.config.read_text())
        raw["market"]["symbols"].append("XRPUSDT")
        self.config.write_text(json.dumps(raw))
        for locale, utterance in (("en", "buy XRP 12"), ("id", "beli XRP 12 usdt")):
            with self.subTest(locale=locale), patch("spotguard.service.fetch_spot_snapshot", return_value=xrp), patch(
                    "spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(1.4)):
                approver = SpotGuard(load_settings(self.config), locale=locale)
                intent = normalize_paper_intent(utterance, approver.settings.market.symbols, locale)
                self.assertEqual(intent["symbol"], "XRPUSDT")
                proposal = approver.create_manual_buy_proposal(intent["symbol"], Decimal(intent["quote_amount"]))["proposal"]
                code = approver.signer.paper_confirmation_code(proposal["canonical_json"])
                approved = approver.paper_text_approve(proposal["id"], code, OWNER, OWNER)
                text = approved["presentation"]["text"]
                self.assertEqual(approved["presentation"]["locale"], locale)
                self.assertIn("XRPUSDT", text)
                self.assertIn("PAPER / FILLED", text)
                self.assertNotRegex(text, r"\d{12,}")

    def test_invalid_locale_fails_config_validation_and_new_install_default(self):
        raw = json.loads(self.config.read_text())
        raw["presentation"] = {"default_locale": "invalid"}
        self.config.write_text(json.dumps(raw))
        with self.assertRaises(ConfigError):
            load_settings(self.config, create_state=False)
        self.assertEqual(json.loads((ROOT / "config.example.json").read_text())["presentation"]["default_locale"], "en")


if __name__ == "__main__":
    unittest.main()

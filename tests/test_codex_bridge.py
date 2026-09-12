from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch
from pathlib import Path

from spotguard.codex_bridge import CodexAgentOSBridge, CodexBridgeError, _safe_environment
from spotguard.config import load_settings
from spotguard.service import SpotGuard
from spotguard.market import Kline, synthetic_bullish_klines
from spotguard.util import isoformat, utcnow

from tests.helpers import config_dict


class CodexBridgeTests(unittest.TestCase):
    def test_successful_confirmation_gates_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(self._settings(Path(directory)))
            candles = synthetic_bullish_klines()[-60:]
            service.agent_os.confirm_candle = Mock(return_value={
                "candle": candles[-1], "mcp_server": "binance-mcp-server",
                "mcp_tool_call": {"tool": "tool_execute", "toolName": "spot.klines"},
                "token_usage": 100, "elapsed_ms": 10,
            })
            with patch("spotguard.service.fetch_klines", return_value=candles):
                result = service.scan(symbols=["BTCUSDT"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["results"][0]["source"], "binance-public-rest-prefilter")
            self.assertTrue(result["results"][0]["confirmation"]["matched"])
            self.assertIsNotNone(result["results"][0]["candidate"])
            service.agent_os.confirm_candle.assert_called_once_with("BTCUSDT")

    def test_no_candidate_does_not_call_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(self._settings(Path(directory)))
            service.agent_os.confirm_candle = Mock(side_effect=AssertionError("must not be called"))
            candles = synthetic_bullish_klines()[-60:]
            with patch("spotguard.service.fetch_klines", return_value=candles), patch(
                "spotguard.service.evaluate", return_value=None
            ):
                result = service.scan(symbols=["BTCUSDT"])
            self.assertTrue(result["ok"])
            service.agent_os.confirm_candle.assert_not_called()

    def test_denial_fails_closed_without_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(self._settings(Path(directory)))
            candles = synthetic_bullish_klines()[-60:]
            service.agent_os.confirm_candle = Mock(side_effect=CodexBridgeError("denied"))
            with patch("spotguard.service.fetch_klines", return_value=candles):
                result = service.scan(symbols=["BTCUSDT"], notify=True, dry_run=True)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["results"][0]["candidate"])
            self.assertEqual(result["results"][0]["confirmation"]["failure_reason"], "denied")

    def _settings(self, root: Path, mode: str = "ok"):
        fake_codex = root / f"fake-codex-{mode}"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys, time\n"
            f"mode = {mode!r}\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['login', 'status']:\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['mcp', 'get']:\n"
            "    print(json.dumps({'url': 'https://agent.binance.com/mcp/agentic', 'oauth_client_id': 'codex'}))\n"
            "    raise SystemExit(0)\n"
            "if 'exec' not in args:\n"
            "    raise SystemExit(64)\n"
            "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
            "symbol = 'ETHUSDT' if mode == 'mismatch' else 'BTCUSDT'\n"
            "now = int(time.time()*1000)\n"
            "payload = {'symbol': symbol, 'interval': '15m', 'candles': [\n"
            " {'open_time': now-2700000, 'open':'59999.00','high':'60000.00','low':'59998.00','close':'59999.50','volume':'1','close_time':now-1800001},\n"
            " {'open_time': now-1800000, 'open':'59999.50','high':'60000.00','low':'59999.00','close':'59999.80','volume':'1','close_time':now-900001},\n"
            " {'open_time': now-900000, 'open':'59999.80','high':'60001.00','low':'59999.50','close':'60000.00','volume':'1','close_time':now-1}] }\n"
            "output.write_text(json.dumps(payload), encoding='utf-8')\n"
            "if mode == 'forbidden':\n"
            "    print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'env'}}))\n"
            "if mode != 'no-mcp':\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {\n"
            "        'id': 'item_1', 'type': 'mcp_tool_call',\n"
            "        'server': 'other-server' if mode == 'other-mcp' else 'binance-marketdata',\n"
            "        'tool': 'tool_execute', 'arguments': {'toolName':'spot.klines','arguments':{'symbol':symbol,'interval':'15m','limit':3}}, 'status':'completed', 'result': {'content': []}\n"
            "    }}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        raw = config_dict(root)
        raw["codex"]["command"] = str(fake_codex)
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        path.chmod(0o600)
        return load_settings(path)

    def test_verified_mcp_market_read_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            bridge = CodexAgentOSBridge(settings)
            previous = os.environ.get("TELEGRAM_BOT_TOKEN")
            os.environ["TELEGRAM_BOT_TOKEN"] = "must-not-reach-codex"
            try:
                result = bridge.review_market("BTCUSDT")
            finally:
                if previous is None:
                    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                else:
                    os.environ["TELEGRAM_BOT_TOKEN"] = previous
            self.assertEqual(result["mcp_tool_calls"], ["tool_execute"])
            self.assertEqual(result["execution_mode"], "paper")
            self.assertTrue(bridge.status()["ready"])

    def test_openclaw_minimal_environment_uses_dedicated_profile_not_caller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch.dict(os.environ, {"CODEX_HOME": "/wrong-caller-home"}, clear=True):
                environment = _safe_environment(settings)
            self.assertEqual(environment["CODEX_HOME"], str(settings.codex.agent_os_home))
            self.assertEqual(environment["SHELL"], "/bin/sh")
            self.assertEqual(environment["PATH"], os.defpath)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", environment)

    def test_confirmation_audit_uses_neutral_cwd_absolute_executable_and_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            with patch("spotguard.codex_bridge.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)) as run:
                with self.assertRaises(CodexBridgeError):
                    bridge.confirm_candle("BTCUSDT")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["cwd"], str(bridge.settings.codex.agent_os_workspace))
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(bridge.settings.codex.agent_os_home))
            self.assertTrue(Path(run.call_args.args[0][0]).is_absolute())
            probe = bridge.status()["last_read_only_probe"]
            self.assertTrue(probe["dedicated_home_applied"])
            self.assertEqual(probe["cwd"], str(bridge.settings.codex.agent_os_workspace))

    def test_keyring_and_stderr_classification_fail_closed_without_secret_echo(self) -> None:
        self.assertEqual(CodexAgentOSBridge._classify_subprocess_error("DBus keyring unavailable"), "keyring_unavailable")
        credential = "secret-" + "value"
        safe = CodexAgentOSBridge._safe_error(f"authorization: Bearer {credential}")
        self.assertNotIn(credential, safe)

    def test_legacy_oauth_approval_override_is_one_child_tool_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            bridge.settings = replace(bridge.settings, codex=replace(bridge.settings.codex, legacy_oauth_profile=True))
            self.assertEqual(bridge._legacy_tool_approval_override(), [
                "-c", 'mcp_servers.binance-marketdata.tools.tool_execute.approval_mode="approve"'])
            self.assertEqual(bridge.status()["profile"], "legacy_oauth_paper")

    def test_status_does_not_call_configured_oauth_currently_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            status = bridge.status()
            self.assertTrue(status["configured"])
            self.assertTrue(status["authenticated"])
            self.assertTrue(status["mcp_discovered"])
            self.assertEqual(status["last_read_only_probe"]["status"], "not_run")
            self.assertFalse(status["currently_usable"])
            self.assertFalse(status["ready"])

    def test_natural_analysis_uses_verified_candle_route_and_base_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            candle = synthetic_bullish_klines()[-2]
            bridge.confirm_candle = Mock(return_value={
                "source": "binance_agent_os_mcp", "symbol": "BTCUSDT", "interval": "15m",
                "candle": candle, "observed_at": "2026-01-01T00:00:00Z", "elapsed_ms": 1,
                "candles": [candle, candle], "raw_candle_count": 3, "closed_candle_count": 2,
                "used_candle_count": 2, "discarded_open_candle_count": 1, "latest_closed_at": "2026-01-01T00:00:00Z", "freshness_seconds": 1,
                "mcp_server": "binance-marketdata", "mcp_tool_call": {"tool": "tool_execute"},
            })
            result = bridge.analyze_market("BTC")
            self.assertEqual(result["symbol"], "BTCUSDT")
            self.assertEqual(result["source"], "binance_agent_os_mcp")
            bridge.confirm_candle.assert_called_once_with("BTCUSDT")

    def test_analysis_malformed_response_denial_timeout_and_unavailable_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            with self.assertRaisesRegex(CodexBridgeError, "invalid fields"):
                bridge._validate_confirmation_payload({"symbol": "BTCUSDT"}, "BTCUSDT")
            denied = json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "binance-mcp-server", "tool": "tool_execute",
                "arguments": {"toolName": "spot.klines", "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                "status": "failed"}})
            with self.assertRaises(CodexBridgeError):
                bridge._verify_confirmation_events(denied, "BTCUSDT")
            with patch("spotguard.codex_bridge.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 90)):
                with self.assertRaisesRegex(CodexBridgeError, "timed out"):
                    bridge.confirm_candle("BTCUSDT")
            self.assertEqual(bridge.status()["last_read_only_probe"]["status"], "timeout")
            with patch("spotguard.codex_bridge.codex_available", return_value=False):
                with self.assertRaisesRegex(CodexBridgeError, "unavailable"):
                    bridge.confirm_candle("BTCUSDT")

    def test_persisted_probe_is_loaded_after_restart_and_stale_success_is_not_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            first = SpotGuard(settings)
            first.ledger.add_event("agent_os.probe", None, {
                "status": "succeeded", "detail": "verified", "reason": "success",
            })
            restarted = SpotGuard(settings)
            fresh = restarted.agent_os.status()
            self.assertEqual(fresh["last_read_only_probe"]["status"], "succeeded")
            self.assertTrue(fresh["currently_usable"])
            restarted.ledger.add_event("agent_os.probe", None, {
                "status": "succeeded", "detail": "stale", "reason": "success",
                "observed_at": isoformat(utcnow() - timedelta(seconds=301)),
            })
            stale = SpotGuard(settings).agent_os.status()
            self.assertFalse(stale["currently_usable"])

    def test_confirmation_argv_is_noninteractive_read_only_and_never_auto_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            with patch("spotguard.codex_bridge.subprocess.run") as run:
                run.side_effect = subprocess.TimeoutExpired("codex", 90)
                with self.assertRaises(CodexBridgeError):
                    bridge.confirm_candle("BTCUSDT")
            argv = run.call_args.args[0]
            self.assertIn("read-only", argv)
            self.assertIn("--ephemeral", argv)
            self.assertIn('mcp_servers.binance-marketdata.tools.tool_execute.approval_mode="approve"', argv)
            self.assertNotIn("-a", argv)

    def test_persisted_failed_probe_remains_not_usable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            service = SpotGuard(settings)
            service.ledger.add_event("agent_os.probe", None, {
                "status": "failed", "detail": "Agent OS probe failed: mcp_error",
                "reason": "mcp_error", "observed_at": isoformat(),
            })
            status = SpotGuard(settings).agent_os.status()
            self.assertEqual(status["last_read_only_probe"]["reason"], "mcp_error")
            self.assertFalse(status["currently_usable"])

    def test_result_without_verified_mcp_call_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory), mode="no-mcp"))
            with self.assertRaises(CodexBridgeError):
                bridge.review_market("BTCUSDT")

    def test_failed_meta_execution_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            stream = json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "binance-mcp-server",
                "tool": "tool_execute", "status": "failed",
                "error": {"message": "approval policy is never"}}})
            with self.assertRaises(CodexBridgeError):
                bridge._verify_events(stream, required_term="kline")

    def test_kline_subprocess_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            bridge = CodexAgentOSBridge(settings)
            with unittest.mock.patch("spotguard.codex_bridge.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("codex", 90)):
                with self.assertRaisesRegex(CodexBridgeError, "timed out"):
                    bridge.confirm_candle("BTCUSDT")

    def test_confirmation_wrong_tool_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            stream = json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "binance-mcp-server",
                "tool": "spot.tickerPrice", "arguments": {}, "status": "completed"}})
            with self.assertRaises(CodexBridgeError):
                bridge._verify_confirmation_events(stream, "BTCUSDT")

    def test_confirmation_one_logical_call_allows_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed", "result": {"content": []}}
            started = dict(item); started["status"] = "in_progress"
            stream = "\n".join((json.dumps({"type": "item.started", "item": started}),
                                  json.dumps({"type": "item.completed", "item": item}),
                                  json.dumps({"type": "turn.completed"})))
            evidence, _ = bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(evidence["arguments"]["limit"], 3)

    def test_confirmation_duplicate_lifecycle_completion_same_id_is_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed", "result": {"content": []}}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": item}),
                                  json.dumps({"type": "item.completed", "item": dict(item)}),
                                  json.dumps({"type": "turn.completed"})))
            bridge._verify_confirmation_events(stream, "BTCUSDT")

    def test_usage_is_provider_turn_total_and_not_lifecycle_sum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed", "result": {"content": []}}
            usage = {"input_tokens": 47550, "cached_input_tokens": 30848, "output_tokens": 492}
            stream = "\n".join((
                json.dumps({"type": "item.started", "item": {**item, "status": "in_progress"}}),
                json.dumps({"type": "item.completed", "item": item}),
                json.dumps({"type": "turn.completed", "usage": usage}),
            ))
            _, parsed = bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(parsed["input_tokens"], 47550)
            self.assertEqual(parsed["cached_input_tokens"], 30848)
            self.assertEqual(parsed["output_tokens"], 492)
            self.assertEqual(parsed["total_tokens"], 48042)
            self.assertEqual(parsed["usage_source"], "codex.turn.completed")
            self.assertIn("not summed", parsed["usage_semantics"])

    def test_repeated_cumulative_usage_is_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed"}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": item}),
                                  json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}),
                                  json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}})))
            _, parsed = bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(parsed["total_tokens"], 110)

    def test_missing_usage_is_explicitly_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed"}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": item}),
                                  json.dumps({"type": "turn.completed"})))
            _, parsed = bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertIsNone(parsed)

    def test_malformed_usage_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            item = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                    "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                    "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                    "status": "completed"}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": item}),
                                  json.dumps({"type": "turn.completed", "usage": {"input_tokens": "100", "output_tokens": 10}})))
            with self.assertRaisesRegex(CodexBridgeError, "usage fields") as raised:
                bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(raised.exception.reason, "malformed_response")

    def test_confirmation_two_distinct_calls_fail_with_cardinality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            def item(call_id: str) -> dict:
                return {"id": call_id, "type": "mcp_tool_call", "server": "binance-marketdata",
                        "tool": "tool_execute", "arguments": {"toolName": "spot.klines",
                        "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}},
                        "status": "completed", "result": {}}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": item("call-1")}),
                                  json.dumps({"type": "item.completed", "item": item("call-2")}),
                                  json.dumps({"type": "turn.completed"})))
            with self.assertRaisesRegex(CodexBridgeError, "logical MCP call") as raised:
                bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(raised.exception.reason, "mcp_call_cardinality")

    def test_confirmation_unrelated_mcp_call_fails_with_cardinality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            expected = {"toolName": "spot.klines", "arguments": {"symbol": "BTCUSDT", "interval": "15m", "limit": 3}}
            first = {"id": "call-1", "type": "mcp_tool_call", "server": "binance-marketdata",
                     "tool": "tool_execute", "arguments": expected, "status": "completed"}
            second = {"id": "call-2", "type": "mcp_tool_call", "server": "other-server",
                      "tool": "tool_execute", "arguments": expected, "status": "completed"}
            stream = "\n".join((json.dumps({"type": "item.completed", "item": first}),
                                  json.dumps({"type": "item.completed", "item": second}),
                                  json.dumps({"type": "turn.completed"})))
            with self.assertRaises(CodexBridgeError) as raised:
                bridge._verify_confirmation_events(stream, "BTCUSDT")
            self.assertEqual(raised.exception.reason, "mcp_call_cardinality")

    def test_confirmation_payload_symbol_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            candle = synthetic_bullish_klines()[-1]
            payload = {"symbol": "ETHUSDT", "interval": "15m", "candles": [candle.to_dict(), candle.to_dict()]}
            with self.assertRaisesRegex(CodexBridgeError, "symbol or interval"):
                bridge._validate_confirmation_payload(payload, "BTCUSDT")

    def test_stale_and_ohlc_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(self._settings(Path(directory)))
            expected = synthetic_bullish_klines()[-1]
            stale = expected.__class__(expected.open_time - 3600000, expected.open,
                expected.high, expected.low, expected.close, expected.volume,
                expected.close_time - 3600000)
            service.agent_os.confirm_candle = Mock(return_value={
                "candle": stale, "mcp_server": "binance-mcp-server",
                "mcp_tool_call": {}, "token_usage": 1, "elapsed_ms": 1})
            stale_result = service._confirm_prefilter_candle("BTCUSDT", stale)
            self.assertFalse(stale_result["matched"])
            self.assertIn("stale", stale_result["failure_reason"])
            for actual, reason in ((stale, "open time mismatch"),
                    (expected.__class__(expected.open_time, expected.open, expected.high + 1,
                        expected.low, expected.close, expected.volume, expected.close_time),
                     "OHLC mismatch")):
                service.agent_os.confirm_candle = Mock(return_value={
                    "candle": actual, "mcp_server": "binance-mcp-server",
                    "mcp_tool_call": {}, "token_usage": 1, "elapsed_ms": 1})
                result = service._confirm_prefilter_candle("BTCUSDT", expected)
                self.assertFalse(result["matched"])
                self.assertIn(reason, result["failure_reason"])

    def test_shell_tool_use_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory), mode="forbidden"))
            with self.assertRaises(CodexBridgeError):
                bridge.review_market("BTCUSDT")

    def test_symbol_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory), mode="mismatch"))
            with self.assertRaises(CodexBridgeError):
                bridge.review_market("BTCUSDT")

    def test_other_mcp_server_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory), mode="other-mcp"))
            with self.assertRaises(CodexBridgeError):
                bridge.review_market("BTCUSDT")

    def test_review_accepts_only_explicit_read_only_tool_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            for tool in ("tool_execute", "spot.order", "unknown.read", "wallet.balance"):
                stream = json.dumps({"type": "item.completed", "item": {
                    "type": "mcp_tool_call", "server": "binance-mcp-server",
                    "tool": tool, "status": "completed", "result": {}}})
                with self.assertRaises(CodexBridgeError):
                    bridge._verify_events(stream)

    def test_malformed_agent_os_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            for stream in ('{"type":"item.completed","item":"not-an-object"}',
                           '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"binance-mcp-server","status":"completed"}}'):
                with self.assertRaises(CodexBridgeError):
                    bridge._verify_events(stream)

    def test_agent_os_demo_then_review_creates_paper_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            service = SpotGuard(settings)
            demo = service.create_agent_os_demo_candidate("BTCUSDT", dry_run=True)
            candidate = demo["candidate"]
            close = float(candidate["price"])
            latest = Kline(candidate["candle_close_time"] - 899999, close, close + 1,
                            close - 1, close, 1, candidate["candle_close_time"])
            previous = Kline(latest.open_time - 900000, close - 1, close, close - 2,
                             close - 1, 1, latest.open_time - 1)
            oldest = Kline(previous.open_time - 900000, close - 2, close - 1,
                           close - 3, close - 2, 1, previous.open_time - 1)
            service.agent_os.review_candidate = Mock(return_value={
                "decision": "APPROVE", "reason": "fresh candidate data verified",
                "fresh_data_verified": True, "symbol": "BTCUSDT", "candidate_id": candidate["id"],
                "interval": "15m", "candle": latest, "candles": [oldest, previous, latest],
                "mcp_tool_calls": ["tool_execute"],
                "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "elapsed_ms": 10,
            })
            reviewed = service.review_candidate_with_agent_os(
                candidate["id"], dry_run=True, dispatch_source="telegram_direct"
            )
            self.assertEqual(reviewed["proposal"]["mode"], "paper")
            self.assertEqual(
                reviewed["market_review"]["mcp_tool_calls"], ["tool_execute"]
            )
            with service.ledger.connect() as connection:
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind IN ('agent_os.market_read', 'agent_os.ai_review')"
                ).fetchone()[0]
            self.assertEqual(event_count, 2)
            with service.ledger.connect() as connection:
                event = connection.execute(
                    "SELECT payload_json FROM events WHERE kind = 'agent_os.ai_review' ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertIn('"dispatch_source":"telegram_direct"', event[0])
            self.assertIn('"reviewer_mode":"isolated"', event[0])

    def test_live_review_skips_proposal_when_symbol_is_not_execution_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(self._settings(Path(directory)), scheduled_proposal_mode="live")
            service = SpotGuard(settings)
            candidate = service.create_demo_candidate("BTCUSDT", 60000)["candidate"]
            close = float(candidate["price"])
            latest = Kline(candidate["candle_close_time"] - 899999, close, close + 1,
                            close - 1, close, 1, candidate["candle_close_time"])
            service.agent_os.review_candidate = Mock(return_value={
                "decision": "APPROVE", "reason": "fresh", "fresh_data_verified": True,
                "symbol": "BTCUSDT", "candidate_id": candidate["id"], "interval": "15m",
                "candle": latest, "candles": [latest], "token_usage": {}, "elapsed_ms": 1,
            })
            readiness = {"execution_ready": False,
                         "blockers": ["symbol_protected_live_unsupported"],
                         "readiness_reasons": ["symbol_protected_live_unsupported"]}
            with patch.object(service, "live_status", return_value=readiness), patch.object(
                service, "create_proposal", side_effect=AssertionError("must not create proposal")
            ):
                result = service.review_candidate_with_agent_os(candidate["id"])
            self.assertEqual(result["proposal_status"], "SKIPPED_NOT_EXECUTION_READY")
            self.assertEqual(result["proposal_mode"], "live")
            self.assertIsNone(result["proposal"])
            self.assertEqual(result["proposal_blockers"], ["symbol_protected_live_unsupported"])
            self.assertEqual(service.ledger.active_proposal_count_read_only(), 0)

    def test_live_review_creates_proposal_only_after_ready_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(self._settings(Path(directory)), scheduled_proposal_mode="live")
            service = SpotGuard(settings)
            candidate = service.create_demo_candidate("BTCUSDT", 60000)["candidate"]
            close = float(candidate["price"])
            latest = Kline(candidate["candle_close_time"] - 899999, close, close + 1,
                            close - 1, close, 1, candidate["candle_close_time"])
            service.agent_os.review_candidate = Mock(return_value={
                "decision": "APPROVE", "reason": "fresh", "fresh_data_verified": True,
                "symbol": "BTCUSDT", "candidate_id": candidate["id"], "interval": "15m",
                "candle": latest, "candles": [latest], "token_usage": {}, "elapsed_ms": 1,
            })
            proposal = {"id": "p-ready", "mode": "live", "status": "PENDING"}
            with patch.object(service, "live_status", return_value={"execution_ready": True}), patch.object(
                service, "create_proposal", return_value={"proposal": proposal, "notification": None}
            ) as create:
                result = service.review_candidate_with_agent_os(candidate["id"])
            self.assertEqual(result["proposal"], proposal)
            create.assert_called_once()

    def test_ai_review_timeout_terminates_only_its_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(self._settings(root), codex=replace(self._settings(root).codex, timeout_seconds=1))
            bridge = CodexAgentOSBridge(settings)
            marker = root / "orphan-marker"
            launcher = root / "launcher.py"
            launcher.write_text(
                "import pathlib, subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', \"import pathlib,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('orphan')\", sys.argv[1]])\n"
                "time.sleep(30)\n", encoding="utf-8"
            )
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    bridge._run_ai_review([sys.executable, str(launcher), str(marker)], "")
                time.sleep(2.2)
                self.assertFalse(marker.exists())
                self.assertIsNone(unrelated.poll())
                next_review = bridge._run_ai_review(
                    [sys.executable, "-c", "print('next-review')"], ""
                )
                self.assertEqual(next_review.stdout.strip(), "next-review")
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_ai_review_stale_mismatch_and_failure_remain_typed_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(self._settings(Path(directory)))
            candidate = service.create_demo_candidate("BTCUSDT", 60000)["candidate"]
            close = float(candidate["price"])
            valid = Kline(candidate["candle_close_time"] - 899999, close, close + 1,
                           close - 1, close, 1, candidate["candle_close_time"])
            stale = Kline(valid.open_time - 900000, valid.open, valid.high, valid.low,
                          valid.close, valid.volume, valid.close_time - 900000)
            review = {"decision": "APPROVE", "reason": "fresh", "fresh_data_verified": True,
                      "symbol": "BTCUSDT", "candidate_id": candidate["id"], "interval": "15m",
                      "candle": stale, "token_usage": {}, "elapsed_ms": 1}
            service.agent_os.review_candidate = Mock(return_value=review)
            with self.assertRaises(CodexBridgeError) as stale:
                service.review_candidate_with_agent_os(candidate["id"])
            self.assertEqual(stale.exception.reason, "mismatched_candle")
            self.assertNotIsInstance(stale.exception, NameError)
            self.assertEqual(service.ledger.active_proposal_count_read_only(), 0)

            service.agent_os.review_candidate = Mock(side_effect=CodexBridgeError("stale", "stale_market_data"))
            with self.assertRaises(CodexBridgeError) as failed:
                service.review_candidate_with_agent_os(candidate["id"])
            self.assertEqual(failed.exception.reason, "stale_market_data")
            self.assertNotIsInstance(failed.exception, NameError)

    def test_three_raw_candles_discards_forming_candle_and_uses_two_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            observed = 2_000_000_000_000
            def row(open_time: int, close_time: int) -> dict:
                return {"open_time": open_time, "close_time": close_time, "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1"}
            payload = {"symbol": "BTCUSDT", "interval": "15m", "candles": [
                row(observed - 2_700_000, observed - 1_800_001),
                row(observed - 1_800_000, observed - 900_001),
                row(observed - 900_000, observed - 1),
            ]}
            candles, meta = bridge._validate_confirmation_payload(payload, "BTCUSDT", observed)
            self.assertEqual([item.open_time for item in candles], [observed - 2_700_000, observed - 1_800_000])
            self.assertEqual(meta["raw_candle_count"], 3)
            self.assertEqual(meta["closed_candle_count"], 2)
            self.assertEqual(meta["used_candle_count"], 2)
            self.assertEqual(meta["discarded_open_candle_count"], 1)

    def test_three_closed_uses_two_newest_and_close_grace_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            observed = 2_000_000_000_000
            rows = [{"open_time": observed - offset, "close_time": observed - offset + 899_999,
                     "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1"}
                    for offset in (2_700_000, 1_800_000, 900_000)]
            rows[-1]["close_time"] = observed - 2_001
            candles, meta = bridge._validate_confirmation_payload({"symbol":"BTCUSDT", "interval":"15m", "candles":rows}, "BTCUSDT", observed)
            self.assertEqual(len(candles), 2)
            self.assertEqual(meta["discarded_open_candle_count"], 0)
            boundary = [
                {"open_time": observed - 1_802_000, "close_time": observed - 902_001, "open":"10", "high":"12", "low":"9", "close":"11", "volume":"1"},
                {"open_time": observed - 902_000, "close_time": observed - 2_000, "open":"10", "high":"12", "low":"9", "close":"11", "volume":"1"},
                {"open_time": observed - 2_000, "close_time": observed + 897_999, "open":"10", "high":"12", "low":"9", "close":"11", "volume":"1"},
            ]
            with self.assertRaisesRegex(CodexBridgeError, "fewer than two closed"):
                bridge._validate_confirmation_payload({"symbol":"BTCUSDT", "interval":"15m", "candles":boundary}, "BTCUSDT", observed)

    def test_insufficient_malformed_future_duplicate_gap_and_stale_candles_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            observed = 2_000_000_000_000
            def row(open_time: int, close_time: int) -> dict:
                return {"open_time":open_time,"close_time":close_time,"open":"10","high":"12","low":"9","close":"11","volume":"1"}
            base = [row(observed-2_700_000, observed-1_800_001), row(observed-1_800_000, observed-900_001), row(observed-900_000, observed+1)]
            for rows in ([base[0], base[2], base[2]],
                         [base[0], base[0], base[2]],
                         [base[0], base[1], {key:value for key,value in base[2].items() if key != "close_time"}],
                         [row(observed-10_800_000, observed-9_900_001), row(observed-9_900_000, observed-9_000_001), row(observed-9_000_000, observed-8_100_001)]):
                with self.assertRaises(CodexBridgeError):
                    bridge._validate_confirmation_payload({"symbol":"BTCUSDT","interval":"15m","candles":rows}, "BTCUSDT", observed)

    def test_confirmation_event_requires_limit_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAgentOSBridge(self._settings(Path(directory)))
            event = {"type":"item.completed", "item":{"id":"one", "type":"mcp_tool_call", "server":"binance-marketdata", "tool":"tool_execute", "status":"completed", "arguments":{"toolName":"spot.klines","arguments":{"symbol":"BTCUSDT","interval":"15m","limit":2}}}}
            stream = json.dumps(event) + "\n" + json.dumps({"type":"turn.completed"})
            with self.assertRaisesRegex(CodexBridgeError, "unexpected arguments"):
                bridge._verify_confirmation_events(stream, "BTCUSDT")


if __name__ == "__main__":
    unittest.main()

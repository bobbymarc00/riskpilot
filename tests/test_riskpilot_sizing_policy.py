from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from spotguard.config import ConfigError, load_settings
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.live_execution import LiveExecutionAdapter
from spotguard.risk_policy.capital import (
    AssetValuation,
    UsageSnapshot,
    build_equity_snapshot,
)
from spotguard.risk_policy.evaluator import PolicyContext, evaluate_entry
from spotguard.risk_policy.limits import effective_reserve, limits_for
from spotguard.risk_policy.sizing import size_entry
from spotguard.service import SpotGuard
from spotguard.util import utcnow
from tests.helpers import config_dict


OWNER = "123456789"
MARKET = SpotMarketSnapshot(
    "BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
    Decimal("5"), Decimal("0.001"), "TRADING", 1,
)


def sizing_config(
    root: Path, *, initial: Decimal = Decimal("1000"),
    live_enabled: bool = False, backstop: bool = False,
) -> Path:
    raw = config_dict(root)
    raw["version"] = 2
    raw["paper"]["initial_balance_usdt"] = str(initial)
    raw["live"]["enabled"] = live_enabled
    raw["risk"].update({
        "risk_per_trade_pct": "0.005",
        "max_aggregate_open_risk_pct": "0.015",
        "daily_realized_loss_pct": "0.02",
        "weekly_realized_loss_pct": "0.05",
    })
    raw["capital"] = {
        "max_position_pct": "0.20",
        "max_total_exposure_pct": "0.60",
        "min_free_reserve_pct": "0.20",
    }
    raw["operations"] = {
        "max_open_positions": 5,
        "max_pending_live_proposals": 1,
    }
    raw["execution"] = {"max_equity_drift_pct": "0.02"}
    raw["sizing_policy"] = {
        "enabled": True,
        "schema_version": 2,
        "capital_basis": "mark_to_market_equity",
        "quote_asset": "USDT",
        "reference_equity_usdt": str(initial),
        "scaling_model": "equity_percentage_risk",
    }
    raw["absolute_safety_caps"] = {"enabled": backstop}
    path = root / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def policy_context(settings, equity_value: str, usage: UsageSnapshot | None = None):
    value = Decimal(equity_value)
    reserve = effective_reserve(settings, "paper", value)
    equity = build_equity_snapshot(
        mode="paper", quote_asset="USDT", free_quote=value,
        locked_quote=Decimal("0"), reserve_quote=reserve,
    )
    hard, effective = limits_for(settings, "paper", value)
    return PolicyContext(
        equity,
        usage or UsageSnapshot(Decimal("0"), Decimal("0"), Decimal("0"), 0, 0),
        hard,
        effective,
    )


class RiskPilotSizingPolicyTests(unittest.TestCase):
    def test_legacy_config_without_sizing_policy_keeps_hard_caps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_settings(path)
            self.assertFalse(settings.sizing_policy.configured)
            hard, effective = limits_for(settings, "paper", Decimal("30"))
            self.assertEqual(effective.max_entry_notional, hard.max_entry_notional)
            self.assertEqual(effective.max_total_open_exposure, Decimal("500"))

    def test_schema_v1_linear_policy_remains_migratable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["sizing_policy"] = {
                "enabled": True, "schema_version": 1,
                "capital_basis": "mark_to_market_equity",
                "quote_asset": "USDT", "reference_equity_usdt": "1000",
                "scaling_model": "linear_to_hard_cap",
                "reserve_quote_amount": "8",
            }
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_settings(path)
            _, small = limits_for(settings, "paper", Decimal("30"))
            self.assertEqual(small.max_entry_notional, Decimal("3"))
            self.assertEqual(effective_reserve(settings, "paper"), Decimal("8"))

    def test_schema_v2_requires_all_separated_percentage_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["sizing_policy"] = {
                "enabled": True, "schema_version": 2,
                "capital_basis": "mark_to_market_equity",
                "quote_asset": "USDT",
                "scaling_model": "equity_percentage_risk",
            }
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "capital must be an object"):
                load_settings(path)

    def test_percentage_limits_scale_down_and_up_without_default_backstop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            self.assertEqual(
                settings.sizing_policy.reference_equity_usdt, Decimal("1000")
            )
            expected = {
                "30": ("6", "18", "0.15", "0.45", "0.6", "1.5", "6"),
                "100": ("20", "60", "0.5", "1.5", "2", "5", "20"),
                "1000": ("200", "600", "5", "15", "20", "50", "200"),
                "20000": ("4000", "12000", "100", "300", "400", "1000", "4000"),
            }
            for equity, values in expected.items():
                with self.subTest(equity=equity):
                    _, limits = limits_for(settings, "paper", Decimal(equity))
                    actual = limits.to_dict()
                    self.assertEqual(
                        limits.equity_scale,
                        Decimal(equity) / Decimal("1000"),
                    )
                    self.assertEqual(
                        tuple(actual[key] for key in (
                            "max_entry_notional", "max_total_open_exposure",
                            "max_risk_per_position", "max_aggregate_open_risk",
                            "max_daily_realized_loss", "max_weekly_realized_loss",
                            "required_reserve",
                        )),
                        values,
                    )

    def test_optional_absolute_backstop_uses_existing_repo_caps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory), backstop=True))
            hard, limits = limits_for(settings, "live", Decimal("20000"))
            self.assertEqual(limits.max_entry_notional, hard.max_entry_notional)
            self.assertEqual(limits.max_total_open_exposure, hard.max_total_open_exposure)
            self.assertEqual(limits.max_risk_per_position, hard.max_risk_per_position)
            self.assertEqual(limits.max_aggregate_open_risk, hard.max_aggregate_open_risk)
            self.assertEqual(limits.max_daily_realized_loss, hard.max_daily_realized_loss)
            self.assertEqual(limits.max_weekly_realized_loss, hard.max_weekly_realized_loss)

    def test_schema_v2_reserve_never_inherits_legacy_live_floor(self) -> None:
        expected = {
            "30": Decimal("6"),
            "100": Decimal("20"),
            "1000": Decimal("200"),
            "20000": Decimal("4000"),
        }
        for backstop in (False, True):
            with self.subTest(absolute_safety_caps=backstop):
                with tempfile.TemporaryDirectory() as directory:
                    settings = load_settings(sizing_config(
                        Path(directory), backstop=backstop,
                    ))
                    self.assertEqual(
                        settings.live.min_free_reserve_usdt, Decimal("8")
                    )
                    for equity, reserve in expected.items():
                        with self.subTest(equity=equity):
                            _, limits = limits_for(
                                settings, "live", Decimal(equity)
                            )
                            self.assertEqual(limits.required_reserve, reserve)
                            self.assertEqual(
                                effective_reserve(
                                    settings, "live", Decimal(equity)
                                ),
                                reserve,
                            )

    def test_live_adapter_does_not_reapply_disabled_legacy_backstop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            canonical = {
                "mode": "live", "product": "SPOT", "side": "BUY",
                "order_type": "LIMIT", "symbol": "BTCUSDT",
                "quote_amount": "4000",
            }
            frozen = LiveExecutionAdapter(settings).freeze({
                "mode": "live", "canonical": canonical,
            })
            self.assertEqual(frozen["quote_amount"], "4000")

        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory), backstop=True))
            with self.assertRaisesRegex(Exception, "per-entry limit"):
                LiveExecutionAdapter(settings).freeze({
                    "mode": "live", "canonical": canonical,
                })

    def test_risk_based_sizing_starts_from_structural_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory), initial=Decimal("100")))
            context = policy_context(settings, "100")
            at_two_point_five = size_entry(
                context, entry_price=Decimal("100"), stop_price=Decimal("97.5")
            )
            at_one = size_entry(
                context, entry_price=Decimal("100"), stop_price=Decimal("99")
            )
            explicit_too_large = size_entry(
                context, entry_price=Decimal("100"), stop_price=Decimal("99"),
                requested_notional=Decimal("21"),
            )
            self.assertEqual(at_two_point_five.risk_budget, Decimal("0.500"))
            self.assertEqual(at_two_point_five.risk_based_notional, Decimal("20"))
            self.assertEqual(at_two_point_five.calculated_notional, Decimal("20.00000000"))
            self.assertEqual(at_one.risk_based_notional, Decimal("50.0"))
            self.assertEqual(at_one.calculated_notional, Decimal("20.00000000"))
            self.assertFalse(explicit_too_large.accepted)
            self.assertEqual(explicit_too_large.calculated_notional, Decimal("21"))
            self.assertIn("MAX_POSITION_EXCEEDED", explicit_too_large.reason_codes)

    def test_equity_drift_tolerance_only_auto_invalidates_large_decline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            service = SpotGuard(settings)
            service._validate_equity_drift(Decimal("100"), Decimal("99.4"))
            service._validate_equity_drift(Decimal("100"), Decimal("110"))
            with self.assertRaisesRegex(Exception, "EQUITY_DRIFT_EXCEEDED"):
                service._validate_equity_drift(Decimal("100"), Decimal("96"))

    def test_mark_to_market_asset_increases_equity_but_not_buying_power(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            equity = build_equity_snapshot(
                mode="paper", quote_asset="USDT",
                free_quote=Decimal("10"), locked_quote=Decimal("0"),
                reserve_quote=Decimal("6"),
                asset_valuations=[AssetValuation(
                    "BTC", "BTCUSDT", Decimal("0.2"), Decimal("100"), Decimal("20")
                )],
            )
            hard, effective = limits_for(settings, "paper", equity.equity)
            context = PolicyContext(
                equity,
                UsageSnapshot(Decimal("20"), Decimal("0.1"), Decimal("0"), 1, 1),
                hard,
                effective,
            )
            result = evaluate_entry(
                context, requested_notional=Decimal("5"),
                projected_exposure=Decimal("25"),
                projected_position_exposure=Decimal("5"),
                projected_position_risk=Decimal("0.1"),
                projected_aggregate_risk=Decimal("0.2"),
                resulting_economic_positions=2,
            )
            self.assertEqual(equity.equity, Decimal("30"))
            self.assertEqual(equity.available_buying_power, Decimal("4"))
            self.assertIn("MIN_FREE_RESERVE_VIOLATION", result.reason_codes)

    def test_open_usage_and_loss_gates_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            usage = UsageSnapshot(
                Decimal("590"), Decimal("14.9"), Decimal("20"), 5, 5,
                weekly_realized_loss=Decimal("50"),
            )
            result = evaluate_entry(
                policy_context(settings, "1000", usage),
                requested_notional=Decimal("20"),
                projected_exposure=Decimal("610"),
                projected_position_exposure=Decimal("20"),
                projected_position_risk=Decimal("0.2"),
                projected_aggregate_risk=Decimal("15.1"),
                resulting_economic_positions=6,
            )
            self.assertEqual(set(result.reason_codes), {
                "MAX_TOTAL_EXPOSURE_EXCEEDED",
                "MAX_AGGREGATE_RISK_EXCEEDED",
                "DAILY_LOSS_LIMIT_REACHED",
                "WEEKLY_LOSS_LIMIT_REACHED",
                "MAX_OPEN_POSITIONS_REACHED",
            })

    def test_partial_close_loss_counts_toward_daily_and_weekly_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            service = SpotGuard(settings)
            service.ledger.add_event(
                "paper.position_partially_closed", None, {"realized_pnl": "-0.40"}
            )
            self.assertEqual(
                service.ledger.daily_paper_realized_loss(
                    utcnow().date().isoformat()
                ), Decimal("0.40")
            )
            self.assertEqual(
                service.ledger.weekly_paper_realized_loss("2000-01-01T00:00:00Z"),
                Decimal("0.40"),
            )

    def test_reserve_and_binance_min_notional_fail_without_resize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("30")
            ))
            service = SpotGuard(settings)
            context, symbols = service._paper_policy_context()
            reserve_result = service._evaluate_paper_entry(
                context, symbols, "BTCUSDT", Decimal("25")
            )
            self.assertIn("MIN_FREE_RESERVE_VIOLATION", reserve_result.reason_codes)

            too_small = MARKET.__class__(
                "BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
                Decimal("7"), Decimal("0.001"), "TRADING", 1,
            )
            with patch("spotguard.service.fetch_spot_snapshot", return_value=too_small), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()
            ):
                with self.assertRaisesRegex(
                    Exception, "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE"
                ):
                    service.create_manual_buy_proposal("BTC", Decimal("6"))
            self.assertEqual(service.ledger.counts()["proposals"], 0)

    def test_v2_min_notional_uses_binance_filter_not_legacy_quote_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("30")
            ))
            service = SpotGuard(settings)
            low_exchange_minimum = MARKET.__class__(
                "BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
                Decimal("1"), Decimal("0.001"), "TRADING", 1,
            )
            with patch(
                "spotguard.service.fetch_spot_snapshot",
                return_value=low_exchange_minimum,
            ), patch(
                "spotguard.service.fetch_klines",
                return_value=synthetic_bullish_klines(),
            ):
                proposal = service.create_manual_buy_proposal(
                    "BTC", Decimal("3")
                )["proposal"]
            self.assertEqual(proposal["quote_amount"], "3")

    def test_equity_change_before_execute_rejects_exact_amount_without_resize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("30")
            ))
            service = SpotGuard(settings)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()
            ):
                proposal = service.create_manual_buy_proposal(
                    "BTC", Decimal("6")
                )["proposal"]
            original_canonical = proposal["canonical_json"]
            token = service.signer.approval_token(original_canonical)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                claim = service.claim(proposal["id"], token, OWNER, OWNER)
            with service.ledger.connect() as connection:
                connection.execute("UPDATE paper_account SET free_usdt='3' WHERE id=1")
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                with self.assertRaisesRegex(Exception, "EQUITY_DRIFT_EXCEEDED"):
                    service.execute_paper(proposal["id"], claim["lease"])
            rejected = service.ledger.get_proposal(proposal["id"])
            self.assertEqual(rejected["status"], "REJECTED")
            self.assertEqual(rejected["quote_amount"], "6")
            self.assertEqual(rejected["canonical_json"], original_canonical)
            self.assertEqual(service.ledger.list_paper_positions(True), [])

    def test_scanner_proposal_uses_risk_engine_size_before_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("100")
            ))
            service = SpotGuard(settings)
            candidate = service.create_demo_candidate(
                "BTCUSDT", Decimal("100")
            )["candidate"]
            result = service.create_proposal(
                candidate["id"], Decimal("99.9"), Decimal("100"), None,
                "deterministic sizing test",
            )
            proposal = result["proposal"]
            policy = proposal["canonical"]["policy_snapshot"]
            self.assertEqual(proposal["quote_amount"], "20")
            self.assertEqual(policy["sizing"]["calculated_notional"], "20")
            self.assertEqual(
                policy["proposal_terms"]["calculated_notional"], "20"
            )
            self.assertEqual(policy["proposal_terms"]["proposal_id"], proposal["id"])

    def test_conservative_equity_never_scales_old_proposal_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            _, increased = limits_for(
                settings, "paper", Decimal("1100"),
                effective_equity=Decimal("1000"),
            )
            _, decreased = limits_for(
                settings, "paper", Decimal("990"),
                effective_equity=Decimal("990"),
            )
            self.assertEqual(increased.max_entry_notional, Decimal("200"))
            self.assertEqual(decreased.max_entry_notional, Decimal("198.00"))

    def test_fresh_filter_change_rejects_without_resize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("30")
            ))
            service = SpotGuard(settings)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()
            ):
                proposal = service.create_manual_buy_proposal("BTC", Decimal("6"))["proposal"]
            original_canonical = proposal["canonical_json"]
            token = service.signer.approval_token(original_canonical)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                claim = service.claim(proposal["id"], token, OWNER, OWNER)
            changed_filter = MARKET.__class__(
                "BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
                Decimal("7"), Decimal("0.001"), "TRADING", 1,
            )
            with patch("spotguard.service.fetch_spot_snapshot", return_value=changed_filter):
                with self.assertRaisesRegex(Exception, "EXCHANGE_FILTER_FAILED"):
                    service.execute_paper(proposal["id"], claim["lease"])
            rejected = service.ledger.get_proposal(proposal["id"])
            self.assertEqual(rejected["quote_amount"], "6")
            self.assertEqual(rejected["canonical_json"], original_canonical)

    def test_paper_and_live_use_identical_percentage_calculator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory)))
            _, paper = limits_for(settings, "paper", Decimal("20000"))
            _, live = limits_for(settings, "live", Decimal("20000"))
            self.assertEqual(paper.to_dict(), live.to_dict())

    def test_live_open_position_is_equity_exposure_and_entry_based_stop_risk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(Path(directory), live_enabled=True))
            service = SpotGuard(settings)
            service.live_executor.read_spot_account = Mock(return_value={"balances": [
                {"asset": "USDT", "free": "900", "locked": "0"},
                {"asset": "BTC", "free": "1", "locked": "0"},
            ]})
            service.live_executor.read_open_spot_orders = Mock(return_value=[
                {"symbol": "BTCUSDT", "orderListId": 12, "type": "STOP_LOSS_LIMIT",
                 "origQty": "1", "stopPrice": "99"},
                {"symbol": "BTCUSDT", "orderListId": 12, "type": "TAKE_PROFIT_LIMIT",
                 "origQty": "1", "stopPrice": "120"},
            ])
            service.live_executor.read_spot_trades = Mock(return_value=[])
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                result = service._validate_live_entry_limits(
                    "ETHUSDT", Decimal("6"), Decimal("0.06"),
                    Decimal("0.06"), Decimal("100"),
                )
            snapshot = result["policy_snapshot"]
            self.assertEqual(snapshot["equity_snapshot"]["equity"], "999.9")
            self.assertEqual(snapshot["equity_snapshot"]["free_quote"], "900")
            self.assertEqual(
                Decimal(snapshot["equity_snapshot"]["available_buying_power"]),
                Decimal("700.02"),
            )
            self.assertEqual(snapshot["usage"]["open_exposure"], "99.9")
            self.assertEqual(snapshot["usage"]["aggregate_open_risk"], "7")

    def test_policy_explain_is_read_only_and_reports_structured_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(sizing_config(
                Path(directory), initial=Decimal("30")
            ))
            service = SpotGuard(settings)
            before = service.ledger.counts()
            result = service.policy_explain(
                mode="paper", symbol="BTC", quote_amount=Decimal("7"),
                risk_at_stop=Decimal("0.05"),
            )
            self.assertFalse(result["accepted"])
            self.assertEqual(result["effective_limits"]["max_entry_notional"], "6")
            self.assertEqual(result["remaining"]["exposure"], "18")
            self.assertIn("MAX_POSITION_EXCEEDED", result["reason_codes"])
            self.assertEqual(service.ledger.counts(), before)


if __name__ == "__main__":
    unittest.main()

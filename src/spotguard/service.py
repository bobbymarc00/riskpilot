from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import time
from typing import Any, Mapping

from . import __version__
from .presentation import detect_locale, error_text, localized, number, render, translate
from .codex_bridge import CodexAgentOSBridge, CodexBridgeError
from .config import Settings, openclaw_available
from .db import Ledger, LedgerError
from .indicators import analyze
from .live_execution import LiveExecutionAdapter
from .market import Kline, MarketError, SymbolValidationError, classify_spot_base_balance, fetch_1m_candles_since, fetch_klines, fetch_spot_snapshot, floor_to_step, load_fixture, scaled_synthetic_klines, synthetic_bullish_klines, validate_spot_symbol
from .policy import PolicyError, build_proposal, entry_policy_terms, execution_intent, validate_claim
from .risk_policy.capital import AssetValuation, UsageSnapshot, build_equity_snapshot
from .risk_policy.evaluator import PolicyContext, evaluate_entry
from .risk_policy.limits import (
    EffectiveLimits,
    absolute_daily_quote_ceiling,
    absolute_entry_ceiling,
    effective_reserve,
    limits_for,
)
from .risk_policy.sizing import SizingDecision, size_entry
from .risk_policy.snapshot import build_policy_snapshot, validate_policy_snapshot
from .paper import build_fill_risk, exit_values, validate_long_bracket
from .security import ApprovalSigner, LiveArm, SecurityError
from .strategy import Signal, evaluate
from .score_engine import MarketScore, SCORE_ENGINE_VERSION, score_market, score_snapshot
from .telegram import OpenClawMessenger, TelegramError, candidate_message, paper_close_message, proposal_message
from .util import bounded_text, canonical_json, decimal_string, isoformat, parse_time, utcnow, validate_simple_id


class SpotGuardError(RuntimeError):
    pass


class SpotGuard:
    def __init__(self, settings: Settings, *, locale: str | None = None) -> None:
        self.settings = settings
        self.ledger = Ledger(settings.database_path, settings.paper.initial_balance_usdt)
        if locale is not None and locale not in {"en", "id"}:
            raise ValueError("unsupported presentation locale")
        self.locale = locale or self.ledger.presentation_locale("chat", settings.telegram.chat_id) or settings.default_locale
        self.signer = ApprovalSigner(settings.state_dir)
        self.live_arm = LiveArm(settings.state_dir, self.signer)
        self.messenger = OpenClawMessenger(settings)
        self.agent_os = CodexAgentOSBridge(
            settings, probe_recorder=lambda payload: self.ledger.add_event("agent_os.probe", None, payload),
            last_probe=self.ledger.latest_event("agent_os.probe"),
        )
        self.live_executor = LiveExecutionAdapter(settings)
        self.paper_reconciliation = self.ledger.reconcile_paper_state()
        self.paper_migration = self._migrate_existing_paper_fills()

    @staticmethod
    def _raise_policy_rejection(result: Any, *, phase: str) -> None:
        if not result.accepted:
            raise PolicyError(
                f"{phase} policy rejected; create a new proposal: "
                + "; ".join(result.reasons)
            )

    @staticmethod
    def _raise_sizing_rejection(result: SizingDecision, *, phase: str) -> None:
        if not result.accepted:
            raise PolicyError(
                f"{phase} sizing rejected; exact proposal was not resized: "
                + "; ".join(result.reasons)
            )

    def _paper_symbol_usage(self, symbol: str) -> tuple[Decimal, Decimal]:
        rows = [
            row
            for row in self.ledger.list_paper_positions(open_only=True)
            if row.get("status", "OPEN") in {"OPEN", "CLOSING"}
            and row["symbol"] == symbol
        ]
        return (
            sum((Decimal(str(row["quote_spent"])) for row in rows), Decimal("0")),
            sum((Decimal(str(row["risk_amount"])) for row in rows), Decimal("0")),
        )

    def _active_proposal_limit(self, mode: str) -> int:
        limit = self.settings.risk.max_active_proposals
        if mode == "live":
            limit = min(limit, self.settings.live.max_pending_proposals)
            if self.settings.sizing_policy.percentage_based:
                assert self.settings.sizing_policy.operations is not None
                limit = min(
                    limit,
                    self.settings.sizing_policy.operations.max_pending_live_proposals,
                )
        return limit

    def _paper_policy_context(
        self, *, price_overrides: dict[str, Decimal] | None = None,
        effective_equity: Decimal | None = None,
    ) -> tuple[PolicyContext, set[str]]:
        """Build a PAPER capital snapshot without treating base assets as free USDT."""
        balance = self.ledger.paper_balance()
        positions = [
            row for row in self.ledger.list_paper_positions(open_only=True)
            if row.get("status", "OPEN") in {"OPEN", "CLOSING"}
        ]
        quote_asset = self.settings.risk.quote_asset
        valuations: list[AssetValuation] = []
        if self.settings.sizing_policy.enabled:
            grouped: dict[str, Decimal] = {}
            for row in positions:
                quantity = Decimal(str(row.get("net_quantity", "0")))
                if quantity <= 0:
                    raise PolicyError("PAPER position quantity is invalid for equity valuation")
                grouped[row["symbol"]] = grouped.get(row["symbol"], Decimal("0")) + quantity
            for symbol, quantity in sorted(grouped.items()):
                mark = (price_overrides or {}).get(symbol)
                if mark is None:
                    mark = fetch_spot_snapshot(self.settings, symbol).bid
                if not mark.is_finite() or mark <= 0:
                    raise PolicyError(f"PAPER mark price is invalid for {symbol}")
                asset = symbol[:-len(quote_asset)]
                valuations.append(AssetValuation(
                    asset=asset, symbol=symbol, quantity=quantity,
                    mark_price=mark, quote_value=quantity * mark,
                ))
            # PAPER base assets are inventory, not locked quote currency.
            locked_quote = Decimal("0")
        else:
            # Legacy configs retain their historical cost-basis balance view
            # and do not gain extra market reads merely by upgrading code.
            locked_quote = Decimal(str(balance.get("locked_usdt", "0")))
        raw_equity = (
            Decimal(str(balance["free_usdt"]))
            + locked_quote
            + sum((item.quote_value for item in valuations), Decimal("0"))
        )
        reserve_basis = (
            min(raw_equity, effective_equity)
            if effective_equity is not None else raw_equity
        )
        equity = build_equity_snapshot(
            mode="paper", quote_asset=quote_asset,
            free_quote=Decimal(str(balance["free_usdt"])),
            locked_quote=locked_quote,
            reserve_quote=effective_reserve(self.settings, "paper", reserve_basis),
            asset_valuations=valuations,
        )
        symbols = {row["symbol"] for row in positions}
        usage = UsageSnapshot(
            open_exposure=sum(
                (Decimal(str(row["quote_spent"])) for row in positions), Decimal("0")
            ),
            aggregate_open_risk=sum(
                (Decimal(str(row["risk_amount"])) for row in positions), Decimal("0")
            ),
            daily_realized_loss=self.ledger.daily_paper_realized_loss(
                utcnow().date().isoformat()
            ),
            economic_positions=max(
                len(symbols), int(balance.get("open_positions", len(symbols)))
            ),
            active_tranches=max(
                len(positions), int(balance.get("active_tranches", len(positions)))
            ),
            weekly_realized_loss=self.ledger.weekly_paper_realized_loss(
                isoformat(
                    utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                    - timedelta(days=utcnow().weekday())
                )
            ),
        )
        hard, effective = limits_for(
            self.settings, "paper", equity.equity,
            effective_equity=reserve_basis,
        )
        return PolicyContext(equity, usage, hard, effective), symbols

    def _evaluate_paper_entry(
        self,
        context: PolicyContext,
        symbols: set[str],
        symbol: str,
        requested: Decimal,
        *,
        projected_position_risk: Decimal | None = None,
        projected_aggregate_risk: Decimal | None = None,
        projected_exposure: Decimal | None = None,
        projected_position_exposure: Decimal | None = None,
        estimated_fee: Decimal = Decimal("0"),
    ) -> Any:
        return evaluate_entry(
            context,
            requested_notional=requested,
            projected_exposure=(
                projected_exposure
                if projected_exposure is not None
                else context.usage.open_exposure + requested
            ),
            projected_position_exposure=projected_position_exposure,
            projected_position_risk=projected_position_risk,
            projected_aggregate_risk=projected_aggregate_risk,
            resulting_economic_positions=(
                context.usage.economic_positions
                if symbol in symbols
                else context.usage.economic_positions + 1
            ),
            estimated_fee=estimated_fee,
        )

    def _attach_policy_snapshot(
        self, values: dict[str, Any], context: PolicyContext, evaluation: Any,
        sizing: SizingDecision | None = None,
        calculated_quantity: Decimal | None = None,
    ) -> None:
        self._raise_policy_rejection(evaluation, phase="proposal")
        snapshot = build_policy_snapshot(
            self.settings, values["mode"], context, evaluation, sizing,
            calculated_quantity=calculated_quantity,
        )
        canonical = values["canonical"]
        snapshot["proposal_terms"] = {
            "proposal_id": canonical["proposal_id"],
            "created_at": canonical["created_at"],
            "expires_at": canonical["expires_at"],
            "symbol": canonical["symbol"],
            "side": canonical["side"],
            "strategy_or_signal_reference": canonical.get("candidate_id"),
            "source": canonical.get("source"),
            "entry_price": canonical["entry_reference"],
            "stop_price": canonical["stop_reference"],
            "target_price": canonical["take_profit_reference"],
            "stop_distance_pct": (
                sizing.to_dict()["stop_distance_pct"] if sizing else None
            ),
            "risk_budget_at_proposal": (
                sizing.to_dict()["risk_budget"] if sizing else None
            ),
            "calculated_notional": canonical["quote_amount"],
            "calculated_quantity": (
                str(calculated_quantity) if calculated_quantity is not None else None
            ),
            "expected_risk": (
                sizing.to_dict()["expected_risk"] if sizing else None
            ),
        }
        canonical["policy_snapshot"] = snapshot
        values["canonical_json"] = canonical_json(values["canonical"])

    def _validate_stored_policy_snapshot(self, proposal: dict[str, Any]) -> None:
        stored = proposal["canonical"].get("policy_snapshot")
        if stored is None and not self.settings.sizing_policy.enabled:
            # Pending proposals created by older code remain claimable under
            # unchanged legacy hard caps. New proposals always carry a snapshot.
            return
        try:
            validate_policy_snapshot(stored, self.settings, proposal["mode"])
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc
        evaluation = stored.get("evaluation", {})
        if str(evaluation.get("requested_notional")) != str(
            proposal["canonical"].get("quote_amount")
        ):
            raise PolicyError("immutable policy snapshot notional does not match proposal")
        if self.settings.sizing_policy.percentage_based:
            sizing = stored.get("sizing")
            if (not isinstance(sizing, dict) or sizing.get("accepted") is not True
                    or str(sizing.get("calculated_notional"))
                    != str(proposal["canonical"].get("quote_amount"))):
                raise PolicyError(
                    "immutable policy sizing does not match exact proposal action"
                )
            captured_quantity = stored.get("proposal_state", {}).get(
                "calculated_quantity"
            )
            canonical_quantity = proposal["canonical"].get(
                "quantity",
                proposal["canonical"].get(
                    "gross_reference_quantity",
                    proposal["canonical"].get("reference_quantity"),
                ),
            )
            if (captured_quantity is not None and canonical_quantity is not None
                    and Decimal(str(captured_quantity))
                    != Decimal(str(canonical_quantity))):
                raise PolicyError(
                    "immutable policy quantity does not match exact proposal action"
                )
            terms = stored.get("proposal_terms")
            expected_terms = {
                "proposal_id": proposal["id"],
                "symbol": proposal["symbol"],
                "entry_price": proposal["canonical"].get("entry_reference"),
                "stop_price": proposal["canonical"].get("stop_reference"),
                "target_price": proposal["canonical"].get("take_profit_reference"),
                "calculated_notional": proposal["canonical"].get("quote_amount"),
            }
            if (not isinstance(terms, dict)
                    or any(str(terms.get(key)) != str(value)
                           for key, value in expected_terms.items())):
                raise PolicyError(
                    "APPROVAL_INVALID: immutable policy terms do not match proposal"
                )

    def _policy_reject(self, proposal: dict[str, Any], exc: Exception) -> None:
        reason = bounded_text(str(exc), 500) or type(exc).__name__
        current = self.ledger.get_proposal(proposal["id"])
        if current["status"] in {"PENDING", "EXECUTING"}:
            self.ledger.reject_proposal_by_policy(proposal["id"], reason)
        raise PolicyError(
            f"proposal rejected by refreshed policy; create a new proposal: {reason}"
        ) from exc

    def _proposal_equity(self, proposal: dict[str, Any]) -> Decimal | None:
        if not self.settings.sizing_policy.percentage_based:
            return None
        stored = proposal["canonical"].get("policy_snapshot")
        try:
            value = Decimal(str(stored["proposal_state"]["equity_at_proposal"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise PolicyError(
                "REVALIDATION_FAILED: proposal is missing immutable equity evidence"
            ) from exc
        if not value.is_finite() or value <= 0:
            raise PolicyError(
                "REVALIDATION_FAILED: proposal equity evidence is invalid"
            )
        return value

    def _validate_equity_drift(
        self, proposal_equity: Decimal | None, current_equity: Decimal
    ) -> None:
        if proposal_equity is None:
            return
        assert self.settings.sizing_policy.execution is not None
        adverse_drift = max(
            Decimal("0"), (proposal_equity - current_equity) / proposal_equity
        )
        if adverse_drift > self.settings.sizing_policy.execution.max_equity_drift_pct:
            raise PolicyError(
                "EQUITY_DRIFT_EXCEEDED: current equity fell "
                f"{adverse_drift * Decimal('100'):f}% from the immutable proposal snapshot"
            )

    def _revalidate_paper_entry_policy(
        self, proposal: dict[str, Any], *, phase: str,
        price_overrides: dict[str, Decimal] | None = None,
        projected_position_risk: Decimal | None = None,
        projected_aggregate_risk: Decimal | None = None,
        projected_exposure: Decimal | None = None,
    ) -> tuple[PolicyContext, Any]:
        self._validate_stored_policy_snapshot(proposal)
        proposal_equity = self._proposal_equity(proposal)
        context, symbols = self._paper_policy_context(
            price_overrides=price_overrides,
            effective_equity=proposal_equity,
        )
        self._validate_equity_drift(proposal_equity, context.equity.equity)
        canonical = proposal["canonical"]
        requested = Decimal(str(canonical["quote_amount"]))
        if self.settings.sizing_policy.percentage_based:
            candidate = self.ledger.get_candidate(proposal["candidate_id"])
            if not candidate["metrics"].get("paper_demo"):
                approval_market = fetch_spot_snapshot(
                    self.settings, proposal["symbol"]
                )
                approval_quantity = floor_to_step(
                    requested / approval_market.ask, approval_market.step_size
                )
                if (approval_quantity <= 0
                        or approval_quantity * approval_market.ask
                        < approval_market.min_notional):
                    raise PolicyError(
                        "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE: refreshed Binance filters reject the exact immutable proposal; amount was not increased"
                    )
            existing_exposure, existing_risk = self._paper_symbol_usage(
                proposal["symbol"]
            )
            sizing = size_entry(
                context,
                entry_price=Decimal(str(canonical["entry_reference"])),
                stop_price=Decimal(str(canonical["stop_reference"])),
                existing_position_exposure=existing_exposure,
                existing_position_risk=existing_risk,
                requested_notional=requested,
                fee_buffer_rate=self.settings.risk.paper_fee_pct / Decimal("100"),
            )
            if not sizing.accepted:
                raise PolicyError(
                    "REVALIDATION_FAILED: ACCOUNT_STATE_CHANGED: immutable proposal no longer fits refreshed account state; "
                    + "; ".join(sizing.reasons)
                )
        if projected_position_risk is None:
            value = canonical.get("projected_risk_at_stop")
            projected_position_risk = Decimal(str(value)) if value is not None else None
        if projected_aggregate_risk is None:
            stored = canonical.get("policy_snapshot", {}).get("evaluation", {})
            at_creation = stored.get("projected_aggregate_risk")
            usage_creation = canonical.get("policy_snapshot", {}).get("usage", {}).get(
                "aggregate_open_risk"
            )
            if at_creation is not None and usage_creation is not None:
                incremental = max(
                    Decimal("0"), Decimal(str(at_creation)) - Decimal(str(usage_creation))
                )
                projected_aggregate_risk = context.usage.aggregate_open_risk + incremental
        result = self._evaluate_paper_entry(
            context, symbols, proposal["symbol"], requested,
            projected_position_risk=projected_position_risk,
            projected_aggregate_risk=projected_aggregate_risk,
            projected_exposure=projected_exposure,
            projected_position_exposure=(
                self._paper_symbol_usage(proposal["symbol"])[0] + requested
            ),
            estimated_fee=(
                requested * self.settings.risk.paper_fee_pct / Decimal("100")
                if self.settings.sizing_policy.percentage_based
                else Decimal("0")
            ),
        )
        self._raise_policy_rejection(result, phase=phase)
        return context, result

    def select_locale(self, text: str, explicit: str | None = None) -> str:
        self.locale = explicit or detect_locale(text, self.ledger.presentation_locale("chat", self.settings.telegram.chat_id), self.settings.default_locale)
        self.ledger.remember_chat_locale(self.settings.telegram.chat_id, self.locale)
        return self.locale

    @localized
    def analyze_market(self, symbol: str, quote_amount: Decimal | None = None) -> dict[str, Any]:
        return self._analyze_one(symbol, quote_amount)

    @localized
    def compare_markets(self, symbols: list[str] | None = None,
                        quote_amount: Decimal | None = None) -> dict[str, Any]:
        targets = symbols or list(self.settings.market.symbols)
        normalized = [self.agent_os._normalize_symbol(symbol) for symbol in targets]
        if len(set(normalized)) != len(normalized):
            raise ValueError("comparison symbols must be unique")
        rows = [self._analyze_one(symbol, quote_amount) for symbol in normalized]
        rows.sort(key=lambda row: (-Decimal(str(row["native_signal_score"])), row["symbol"]))
        execution_eligible = [row for row in rows if row["execution_eligibility"]["eligible"]]
        eligible_candidates = [row for row in execution_eligible if row["candidate_eligible"] and
                               row["agent_os_confirmation"]["matched"]]
        winner = rows[0] if rows else None
        alternative = eligible_candidates[0] if eligible_candidates else None
        return {
            "ranking": rows,
            "market_score_ranking": rows,
            "execution_eligible_ranking": execution_eligible,
            "eligible_candidates": eligible_candidates,
            "metric": "native_signal_score",
            "highest_market_score_symbol": winner["symbol"] if winner else None,
            "highest_market_score_ineligibility_reason": (
                self._combined_ineligibility_reason(winner) if winner and winner not in eligible_candidates else None
            ),
            "highest_scoring_eligible_candidate": alternative["symbol"] if alternative else None,
            "proposal_created": False,
            "profit_guarantee": False,
        }

    @staticmethod
    def _combined_ineligibility_reason(row: dict[str, Any]) -> str | None:
        if not row["candidate_eligible"]:
            return row["candidate_ineligibility_reason"]
        if not row["agent_os_confirmation"]["matched"]:
            return row["agent_os_confirmation"].get("failure_reason") or "Agent OS confirmation did not match"
        return row["execution_eligibility"].get("blocking_reason")

    def _analyze_one(self, symbol: str, quote_amount: Decimal | None) -> dict[str, Any]:
        symbol = self.agent_os._normalize_symbol(symbol)
        validation = validate_spot_symbol(self.settings, symbol)
        klines = fetch_klines(self.settings, symbol, validated=True)
        scored = score_market(self.settings, symbol, klines)
        with self.agent_os.without_probe_audit():
            confirmation = self._confirm_prefilter_candle(symbol, klines[-1], record_event=False)
        amount = quote_amount if quote_amount is not None else self.settings.risk.default_order_size_usdt
        amount_source = "explicit" if quote_amount is not None else "configured default_order_size_usdt"
        execution = self._hypothetical_paper_eligibility(
            scored, amount, validation, Decimal(str(klines[-1].close))
        )
        previous, candle = klines[-2], klines[-1]
        previous_close = Decimal(str(previous.close)); current_close = Decimal(str(candle.close))
        current_open = Decimal(str(candle.open)); current_high = Decimal(str(candle.high)); current_low = Decimal(str(candle.low))
        short = {
            "closed_candle_change": str(current_close - previous_close),
            "closed_candle_change_pct": str((current_close - previous_close) / previous_close * Decimal("100")),
            "open_to_close_change": str(current_close - current_open),
            "open_to_close_change_pct": str((current_close - current_open) / current_open * Decimal("100")),
            "high_low_range": str(current_high - current_low),
            "local_support": str(min(current_low, Decimal(str(previous.low)))),
            "local_resistance": str(max(current_high, Decimal(str(previous.high)))),
            "latest_volume": str(Decimal(str(candle.volume))),
        }
        open_position = any(row["symbol"] == symbol for row in self.ledger.list_paper_positions(open_only=True))
        canonical = scored.to_dict()
        return {
            "source": "binance-public-rest-prefilter",
            "score_source": "Binance closed-candle prefilter",
            "confirmation_source": "Binance Agent OS",
            "access_mode": "read_only_account_and_market",
            "symbol": symbol,
            "interval": self.settings.market.interval,
            "lookback": len(klines),
            "score_engine_version": SCORE_ENGINE_VERSION,
            "native_signal_score": scored.native_total_score,
            "score": scored.native_total_score,
            "score_components": canonical["components"],
            "threshold_result": canonical["threshold_result"],
            "market_signal_classification": scored.market_signal_classification,
            "signal": scored.market_signal_classification,
            "candidate_eligible": scored.candidate_eligible,
            "candidate_ineligibility_reason": scored.candidate_ineligibility_reason,
            "metrics": scored.snapshot.to_dict(),
            "indicators": short,
            "candle": candle.to_dict(),
            "latest_closed_at": isoformat(datetime.fromtimestamp(candle.close_time / 1000, timezone.utc)),
            "freshness_seconds": max(0, int(time.time() - candle.close_time / 1000)),
            "agent_os_confirmation": confirmation,
            "hypothetical_order_amount_usdt": str(amount),
            "hypothetical_order_amount_source": amount_source,
            "execution_eligibility": execution,
            "paper_position_open": open_position,
            "paper_position_disclosure": "OPEN" if open_position else "NONE",
            "proposal_created": False,
            "score_audit": {"score_engine_version": SCORE_ENGINE_VERSION,
                            "score_source": "Binance closed-candle prefilter",
                            "confirmation_source": "Binance Agent OS"},
        }

    def _hypothetical_paper_eligibility(self, scored: MarketScore, amount: Decimal,
                                        validation: dict[str, Any], price: Decimal) -> dict[str, Any]:
        try:
            context, _ = self._ensure_paper_entry_available(
                scored.symbol, amount, read_only=True
            )
            step = Decimal(validation["market_step_size"])
            minimum = Decimal(validation["min_notional"])
            quantity = floor_to_step(amount / price, step)
            spend = quantity * price
            if quantity <= 0 or spend < minimum:
                minimum_units = (minimum / price / step).to_integral_value(rounding=ROUND_CEILING)
                required = minimum_units * step * price
                raise PolicyError(
                    f"requested {amount} USDT is below the current minimum notional request of {required:f} USDT after downward quantity-step rounding; amount was not increased"
                )
            terms = entry_policy_terms(
                self.settings, candidate_price=price,
                atr_value=Decimal(str(scored.snapshot.atr_14)),
                bid_reference=price, ask_reference=price,
                quote_amount=amount, mode="paper",
            )
            risk_references = {"entry_reference": str(terms["entry_reference"]),
                               "stop_reference": str(terms["stop_reference"])}
            plan = build_fill_risk(self.settings, risk_references, price, quantity)
            projection = self._paper_risk_projection(
                scored.symbol, price, plan, bid=price, ask=price,
                reward_risk=terms["reward_risk"],
                effective_limits=context.effective_limits,
            )
            existing = [row for row in self.ledger.list_paper_positions(True)
                        if row["symbol"] == scored.symbol]
            return {"eligible": True, "blocking_reason": None,
                    "hypothetical_only": True, "scale_in": bool(existing),
                    "projected_exposure_usdt": str(sum((Decimal(row["quote_spent"]) for row in self.ledger.list_paper_positions(True)), Decimal("0")) + spend),
                    "projected_position_risk_usdt": str(projection["new_position_risk"]),
                    "projected_aggregate_risk_usdt": str(projection["projected_aggregate_risk"]),
                    "effective_limits": context.effective_limits.to_dict(),
                    "minimum_notional_usdt": str(minimum), "quantity_step": str(step)}
        except (PolicyError, ValueError, KeyError) as exc:
            return {"eligible": False, "blocking_reason": str(exc), "hypothetical_only": True,
                    "scale_in": any(row["symbol"] == scored.symbol for row in self.ledger.list_paper_positions(True)),
                    "minimum_notional_usdt": validation.get("min_notional"),
                    "quantity_step": validation.get("market_step_size")}

    def _migrate_existing_paper_fills(self) -> dict[str, Any]:
        migrated: list[str] = []
        with self.ledger.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE mode='paper' AND status='EXECUTED' AND execution_status='FILLED' AND execution_summary_json IS NOT NULL"
            ).fetchall()
        for raw in rows:
            proposal = self.ledger._decode_proposal(raw, include_private=True)
            if proposal is None or proposal["canonical"].get("source") != "manual-paper-test":
                continue
            with self.ledger.connect() as connection:
                if connection.execute("SELECT 1 FROM paper_positions WHERE proposal_id=?", (proposal["id"],)).fetchone():
                    continue
            summary = proposal.get("execution_summary")
            required = ("average_fill_price", "gross_base_quantity", "net_base_quantity",
                        "actual_paper_spend", "simulated_fee", "timestamp")
            if not isinstance(summary, dict) or any(key not in summary for key in required):
                raise LedgerError(f"paper fill {proposal['id']} is ambiguous and cannot be backfilled")
            fill = Decimal(str(summary["average_fill_price"]))
            gross = Decimal(str(summary["gross_base_quantity"]))
            plan = build_fill_risk(self.settings, proposal, fill, gross)
            if Decimal(plan["net_base_quantity"]) != Decimal(str(summary["net_base_quantity"])):
                raise LedgerError(f"paper fill {proposal['id']} net quantity is ambiguous")
            if Decimal(plan["quote_spent"]) != Decimal(str(summary["actual_paper_spend"])):
                raise LedgerError(f"paper fill {proposal['id']} spend is ambiguous")
            position = {"id": f"pp-{proposal['id'][2:]}", "proposal_id": proposal["id"],
                "symbol": proposal["symbol"], "entry_reference": proposal["entry_reference"],
                "opened_at": str(summary["timestamp"]), **plan}
            fee_usdt = Decimal(plan["entry_fee_base"]) * fill
            if self.ledger.backfill_paper_position(position, fee_usdt):
                migrated.append(position["id"])
        return {"migrated": migrated}

    def _paper_risk_projection(self, symbol: str, fill_price: Decimal,
                               plan: dict[str, str], *, bid: Decimal | None = None,
                               ask: Decimal | None = None, reward_risk: Decimal | None = None,
                               effective_limits: EffectiveLimits | None = None) -> dict[str, Decimal | int]:
        rows = [row for row in self.ledger.list_paper_positions(open_only=True) if row.get("status", "OPEN") in {"OPEN", "CLOSING"}]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["symbol"], []).append(row)

        fee_rate = self.settings.risk.paper_fee_pct / Decimal("100")
        slippage_rate = self.settings.paper.slippage_pct / Decimal("100")
        def economic_risk(items: list[dict[str, Any]]) -> Decimal:
            quantity = sum((Decimal(row["net_quantity"]) for row in items), Decimal("0"))
            if quantity <= 0:
                return Decimal("0")
            cost = sum((Decimal(row["quote_spent"]) for row in items), Decimal("0"))
            stop = max(Decimal(row["final_stop"]) for row in items)
            entry = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in items), Decimal("0")) / quantity
            if stop >= entry:
                raise PolicyError("invalid persisted PAPER bracket: stop is not below aggregate average entry; position requires repair")
            stop_net = quantity * stop * (Decimal("1") - slippage_rate) * (Decimal("1") - fee_rate)
            risk = cost - stop_net
            if risk <= 0:
                raise PolicyError("invalid PAPER risk: downside loss must be positive")
            return risk

        existing = grouped.get(symbol, [])
        new_quantity = Decimal(plan["net_base_quantity"])
        new_stop = Decimal(plan["final_stop"])
        if existing:
            old_quantity = sum((Decimal(row["net_quantity"]) for row in existing), Decimal("0"))
            old_value = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in existing), Decimal("0"))
            projected_quantity = old_quantity + new_quantity
            projected_entry = (old_value + fill_price * new_quantity) / projected_quantity
            effective_stop = max(Decimal(row["final_stop"]) for row in existing)
            ratio = reward_risk or self.settings.risk.min_reward_risk
            projected_target = projected_entry + ratio * (projected_entry - effective_stop)
            validate_long_bracket(projected_entry, effective_stop, projected_target, ratio, bid=bid, ask=ask)
            projected_cost = sum((Decimal(row["quote_spent"]) for row in existing), Decimal("0")) + Decimal(plan["quote_spent"])
            stop_net = projected_quantity * effective_stop * (Decimal("1") - slippage_rate) * (Decimal("1") - fee_rate)
            new_position_risk = projected_cost - stop_net
            if new_position_risk <= 0:
                raise PolicyError("invalid PAPER risk: aggregate downside loss must be positive")
        else:
            old_quantity = Decimal("0"); projected_quantity = new_quantity
            projected_entry = fill_price; effective_stop = new_stop
            new_position_risk = Decimal(plan["risk_amount"])
            projected_target = Decimal(plan["final_target"])
            validate_long_bracket(projected_entry, effective_stop, projected_target,
                                  reward_risk or self.settings.risk.min_reward_risk, bid=bid, ask=ask)
        current_aggregate = sum((economic_risk(items) for items in grouped.values()), Decimal("0"))
        prior_symbol_risk = economic_risk(existing) if existing else Decimal("0")
        projected_aggregate = current_aggregate - prior_symbol_risk + new_position_risk
        result: dict[str, Decimal | int] = {
            "current_aggregate_risk": current_aggregate,
            "prior_symbol_risk": prior_symbol_risk,
            "new_position_risk": new_position_risk,
            "projected_aggregate_risk": projected_aggregate,
            "projected_quantity": projected_quantity,
            "projected_entry": projected_entry,
            "effective_stop": effective_stop,
            "projected_target": projected_target,
            "existing_quantity": old_quantity,
            "existing_tranches": len(existing),
        }
        limit = (effective_limits.max_aggregate_open_risk if effective_limits
                 else self.settings.paper.max_aggregate_risk_usdt)
        position_limit = (effective_limits.max_risk_per_position if effective_limits
                          else self.settings.paper.max_risk_per_position_usdt)
        if new_position_risk > position_limit or projected_aggregate > limit:
            raise PolicyError("PAPER risk rejected: current aggregate risk "
                f"{current_aggregate:f} USDT; new economic-position risk {new_position_risk:f} USDT; "
                f"projected aggregate risk {projected_aggregate:f} USDT; configured aggregate limit {limit:f} USDT (effective); "
                f"configured per-position limit {position_limit:f} USDT (effective)")
        return result

    def _ensure_paper_entry_available(self, symbol: str | None = None,
                                      requested: Decimal | None = None,
                                      *, read_only: bool = False) -> tuple[PolicyContext, set[str]]:
        context, symbols = self._paper_policy_context()
        active_tranches = context.usage.active_tranches
        if active_tranches >= self.settings.paper.max_active_tranches:
            raise PolicyError(f"active PAPER tranche limit reached ({self.settings.paper.max_active_tranches})")
        if not read_only:
            self.ledger.expire_stale_active_proposals()
        active_proposals = (self.ledger.active_proposal_count_read_only() if read_only
                            else self.ledger.active_proposal_count())
        if active_proposals >= self.settings.risk.max_active_proposals:
            raise PolicyError("maximum active proposal count has been reached")
        has_symbol_buy = (self.ledger.has_active_paper_buy_read_only(symbol) if read_only and symbol
                          else self.ledger.has_active_paper_buy(symbol) if symbol else False)
        if symbol and has_symbol_buy:
            raise PolicyError(f"a pending or processing paper BUY already exists for {symbol}")
        if symbol is None and len(symbols) >= self.settings.paper.max_economic_positions:
            raise PolicyError(f"economic PAPER position/distinct-symbol limit reached ({self.settings.paper.max_economic_positions})")
        if (symbol and symbol not in symbols
                and context.usage.economic_positions >= context.effective_limits.max_economic_positions):
            raise PolicyError(
                "economic PAPER position/distinct-symbol limit reached "
                f"({context.effective_limits.max_economic_positions})"
            )
        if symbol and requested is not None:
            result = self._evaluate_paper_entry(context, symbols, symbol, requested)
            self._raise_policy_rejection(result, phase="proposal")
        elif context.usage.aggregate_open_risk >= context.effective_limits.max_aggregate_open_risk:
            raise PolicyError("paper aggregate open risk limit is exhausted")
        day = utcnow().date().isoformat()
        if self.ledger.successful_paper_entries(day) >= self.settings.paper.max_successful_entries_per_utc_day:
            raise PolicyError(f"daily PAPER entry quota reached ({self.settings.paper.max_successful_entries_per_utc_day} successful BUY fills per UTC day)")
        if (not self.settings.sizing_policy.percentage_based
                and context.equity.available_buying_power
                < self.settings.risk.min_quote_amount):
            raise PolicyError("insufficient free paper USDT")
        return context, symbols

    def scan(
        self,
        symbols: list[str] | None = None,
        notify: bool = False,
        fixture: Path | None = None,
        synthetic: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        paper_monitor = self.monitor_paper_positions(notify=notify, dry_run=dry_run)
        requested = symbols or list(self.settings.market.symbols)[:20]
        unknown = [symbol for symbol in requested if symbol not in self.settings.market.symbols]
        if unknown:
            raise SpotGuardError(f"symbols are not configured: {', '.join(unknown)}")
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        ranked: list[tuple[Signal, list[Kline], dict[str, Any]]] = []
        for symbol in requested:
            try:
                if fixture:
                    klines = load_fixture(fixture, shift_to_now=True)
                    source = f"fixture:{fixture.name}"
                elif synthetic:
                    klines = synthetic_bullish_klines()
                    source = "synthetic-paper-demo"
                else:
                    prefilter_started = time.monotonic()
                    klines = fetch_klines(self.settings, symbol)
                    source = "binance-public-rest-prefilter"
                    self.ledger.add_event("binance_api.prefilter", None, {
                        "symbol": symbol, "interval": self.settings.market.interval,
                        "requested_candles": 61, "closed_candles": len(klines),
                        "latest_open_time": klines[-1].open_time,
                        "latest_close_time": klines[-1].close_time,
                        "observed_at": isoformat(),
                        "elapsed_ms": int((time.monotonic() - prefilter_started) * 1000),
                        "source": "https://api.binance.com/api/v3/klines",
                        "score_engine_version": SCORE_ENGINE_VERSION,
                    })
                snapshot = analyze(klines)
                scored = score_snapshot(self.settings, symbol, snapshot, klines[-2].close)
                # Keep the established scheduler decision call boundary intact;
                # evaluate delegates to the same rules represented by scored.
                signal = evaluate(self.settings, symbol, snapshot)
                record: dict[str, Any] = {"symbol": symbol, "source": source,
                    "score": snapshot.score, "native_signal_score": scored.native_total_score,
                    "metrics": snapshot.to_dict(), "candidate": None,
                    "created": False, "notification": None, "confirmation": None,
                    "score_engine_version": SCORE_ENGINE_VERSION,
                    "score_components": [item.to_dict() for item in scored.components],
                    "threshold_result": scored.to_dict()["threshold_result"],
                    "market_signal_classification": scored.market_signal_classification,
                    "candidate_eligible": scored.candidate_eligible,
                    "candidate_ineligibility_reason": scored.candidate_ineligibility_reason,
                    "score_audit": {"score_engine_version": SCORE_ENGINE_VERSION,
                        "score_source": "Binance closed-candle prefilter",
                        "confirmation_source": "Binance Agent OS"}}
                results.append(record)
                if signal:
                    cooldown_since = isoformat(utcnow() - timedelta(
                        minutes=self.settings.market.candidate_cooldown_minutes))
                    if not self.ledger.has_recent_candidate(symbol, signal.side, cooldown_since):
                        ranked.append((signal, klines, record))
                    else:
                        record["confirmation"] = {"matched": False,
                            "failure_reason": "per-symbol candidate cooldown/deduplication"}
            except Exception as exc:
                item = {"symbol": symbol, "error": str(exc), "type": type(exc).__name__}
                errors.append(item)
                if "HTTP 418" in str(exc) or "HTTP 429" in str(exc):
                    self.ledger.add_event("scan.circuit_breaker", symbol,
                        {"reason": str(exc), "scanned": len(results)})
                    break
                if isinstance(exc, SymbolValidationError):
                    self.ledger.add_event("symbol.validation_rejected", symbol, item)

        ranked.sort(key=lambda item: (item[0].score, item[0].candle_close_time), reverse=True)
        capacity_full = (self.settings.mode == "paper" and
            int(self.ledger.paper_balance().get("active_tranches", self.ledger.paper_balance().get("open_positions", 0))) >= self.settings.paper.max_active_tranches)
        daily_entries = self.ledger.successful_paper_entries(utcnow().date().isoformat())
        quota_full = self.settings.mode == "paper" and daily_entries >= self.settings.paper.max_successful_entries_per_utc_day
        if ranked and (capacity_full or quota_full):
            reason = (f"active PAPER tranche limit reached ({self.settings.paper.max_active_tranches})" if capacity_full else f"daily PAPER entry quota reached ({self.settings.paper.max_successful_entries_per_utc_day})")
            for _, _, record in ranked:
                record["confirmation"] = {"matched": False, "failure_reason": reason}
            self.ledger.add_event("scan.entry_skipped", None, {"reason": reason,
                "ranked_symbols": [item[0].symbol for item in ranked], "agent_os_invoked": False})
        elif ranked:
            selected, klines, selected_record = ranked[0]
            for lower, _, record in ranked[1:]:
                record["confirmation"] = {"matched": False,
                    "failure_reason": f"lower-ranked than {selected.symbol}; deferred this scan"}
            if not fixture and not synthetic:
                confirmation = self._confirm_prefilter_candle(selected.symbol, klines[-1])
                selected_record["confirmation"] = confirmation
                if not confirmation["matched"]:
                    errors.append({"symbol": selected.symbol,
                        "error": confirmation["failure_reason"], "type": "AgentOSConfirmationError"})
                    selected = None
                else:
                    selected = replace(selected, metrics={**selected.metrics,
                        "agent_os_confirmed": True,
                        "agent_os_confirmation_open_time": klines[-1].open_time,
                        "prefilter_candle": klines[-1].to_dict()})
            else:
                selected = replace(selected, metrics={**selected.metrics,
                    "paper_demo": True, "explicit_test_source": selected_record["source"]})
            if selected is not None:
                candidate, created = self.ledger.create_candidate(
                    selected, self.settings.market.candidate_ttl_minutes)
                selected_record["candidate"] = candidate
                selected_record["created"] = created
                if notify and created:
                    selected_record["notification"] = self.notify_candidate(candidate["id"], dry_run=dry_run)
        return {"ok": not errors, "mode": self.settings.mode, "results": results,
            "errors": errors, "paper_monitor": paper_monitor,
            "ranking": [{"symbol": item[0].symbol, "score": item[0].score,
                "candle_close_time": item[0].candle_close_time} for item in ranked]}

    def symbols_status(self) -> dict[str, Any]:
        enabled, rejected = [], []
        for symbol in self.settings.market.symbols:
            try:
                status = validate_spot_symbol(self.settings, symbol)
                enabled.append(status)
                self.ledger.add_event("symbol.validation_passed", symbol, status)
            except Exception as exc:
                item = {"symbol": symbol, "status": "REJECTED", "reason": str(exc)}
                rejected.append(item)
                self.ledger.add_event("symbol.validation_rejected", symbol, item)
        return {"configured": list(self.settings.market.symbols), "enabled": enabled,
            "rejected": rejected, "validated_at": isoformat()}

    def smart_radar(self, limit: int = 5) -> dict[str, Any]:
        """Return the scanner's persisted observation-only ranking.

        This deliberately does not contact Binance, mutate scanner state, or
        invoke candidate/proposal/execution code.
        """
        if not 1 <= limit <= 10:
            raise SpotGuardError("radar limit must be between 1 and 10")
        path = self.settings.state_dir / "smart-scanner" / "top-radar.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SpotGuardError("Smart Radar has no verified local snapshot yet") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("generated_at"), str) or not isinstance(raw.get("rows"), list):
            raise SpotGuardError("Smart Radar local snapshot is invalid")
        required = {"symbol", "lane", "potential_score", "label", "configured", "core_score", "momentum_score", "hype_score", "ret_5m_pct", "ret_15m_pct", "spread_pct"}
        rows = [row for row in raw["rows"] if isinstance(row, dict) and required <= set(row)]
        if len(rows) != len(raw["rows"]):
            raise SpotGuardError("Smart Radar local snapshot is invalid")
        rows = rows[:limit]
        if self.locale == "id":
            lines = ["📡 RISKPILOT TOP RADAR POTENSI", f"Snapshot: {raw['generated_at']} · Universe: {raw.get('active_count', '?')} aktif"]
            for index, row in enumerate(rows, 1):
                native = "belum ada candle tertutup" if row.get("native_score") is None else f"{row['native_score']}/100 ({row.get('native_interval')})"
                lines.append(
                    f"#{index} {row['symbol']} · {row['label']} · {row['lane']}\n"
                    f"Potensi: {row['potential_score']}/100 · Native: {native}\n"
                    f"Core {row['core_score']} · Momentum {row['momentum_score']} · Hype {row['hype_score']}\n"
                    f"5m {row['ret_5m_pct']:+.3f}% · 15m {row['ret_15m_pct']:+.3f}% · spread {row['spread_pct']:.3f}%"
                )
            lines.append("Observasi read-only lintas lane; bukan kandidat, proposal, atau instruksi entry. AI REVIEW hanya muncul bila flow kandidat 15m yang ada lolos semua validasi.")
        else:
            lines = ["📡 RISKPILOT TOP POTENTIAL RADAR", f"Snapshot: {raw['generated_at']} · Universe: {raw.get('active_count', '?')} active"]
            for index, row in enumerate(rows, 1):
                native = "no closed-candle score yet" if row.get("native_score") is None else f"{row['native_score']}/100 ({row.get('native_interval')})"
                lines.append(
                    f"#{index} {row['symbol']} · {row['label']} · {row['lane']}\n"
                    f"Potential: {row['potential_score']}/100 · Native: {native}\n"
                    f"Core {row['core_score']} · Momentum {row['momentum_score']} · Hype {row['hype_score']}\n"
                    f"5m {row['ret_5m_pct']:+.3f}% · 15m {row['ret_15m_pct']:+.3f}% · spread {row['spread_pct']:.3f}%"
                )
            lines.append("Cross-lane read-only observation; not a candidate, proposal, or entry instruction. AI REVIEW appears only when the existing 15m candidate flow passes every validation.")
        return {"top_radar": rows, "generated_at": raw["generated_at"], "presentation": {"locale": self.locale, "text": "\n\n".join(lines)}}

    def _confirm_prefilter_candle(self, symbol: str, expected: Kline,
                                  *, record_event: bool = True) -> dict[str, Any]:
        started = time.monotonic()
        evidence: dict[str, Any] = {
            "symbol": symbol, "interval": self.settings.market.interval,
            "expected_open_time": expected.open_time, "matched": False,
            "observed_at": isoformat(), "tool_name": "spot.klines",
            "failure_reason": None,
        }
        try:
            result = self.agent_os.confirm_candle(symbol)
            actual = result["candle"]
            now_ms = int(time.time() * 1000)
            if actual.open_time != expected.open_time:
                raise SpotGuardError("Agent OS confirmation candle open time mismatch")
            if actual.close_time < now_ms - (30 * 60 * 1000):
                raise SpotGuardError("Agent OS confirmation candle is stale")
            expected_ohlc = (expected.open, expected.high, expected.low, expected.close)
            actual_ohlc = (actual.open, actual.high, actual.low, actual.close)
            if actual_ohlc != expected_ohlc:
                raise SpotGuardError("Agent OS confirmation candle OHLC mismatch")
            evidence.update({"matched": True, "actual_open_time": actual.open_time,
                "mcp_server": result["mcp_server"], "mcp_tool_call": result["mcp_tool_call"],
                "token_usage": result["token_usage"], "agent_elapsed_ms": result["elapsed_ms"]})
        except Exception as exc:
            evidence["failure_reason"] = bounded_text(str(exc), "failure_reason", maximum=300)
        evidence["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        if record_event:
            evidence["score_engine_version"] = SCORE_ENGINE_VERSION
            self.ledger.add_event("agent_os.confirmation", None, evidence)
        return evidence

    def notify_candidate(self, candidate_id: str, dry_run: bool = False) -> dict[str, Any]:
        candidate = self.ledger.get_candidate(candidate_id)
        message, buttons = candidate_message(candidate, locale=self.locale)
        delivery = self.messenger.send(message, buttons, dry_run=dry_run)
        if delivery.delivered:
            self.ledger.mark_candidate_notified(candidate_id)
        return delivery.to_dict()

    def backup_and_repair_paper_bracket(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if symbol not in self.settings.market.symbols:
            raise PolicyError(f"symbol is not allowlisted: {symbol}")
        backup = self.ledger.backup(f"pre-bracket-repair-{symbol.lower()}")
        rows = [row for row in self.ledger.list_paper_positions(open_only=True) if row["symbol"] == symbol and row["status"] == "OPEN"]
        if not rows:
            raise PolicyError(f"no open PAPER economic position exists for {symbol}")
        quantity = sum((Decimal(row["net_quantity"]) for row in rows), Decimal("0"))
        aggregate_entry = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in rows), Decimal("0")) / quantity
        current_stop = max(Decimal(row["final_stop"]) for row in rows)
        current_target = min(Decimal(row["final_target"]) for row in rows)
        try:
            validate_long_bracket(aggregate_entry, current_stop, current_target, self.settings.risk.min_reward_risk)
        except PolicyError:
            pass
        else:
            raise PolicyError("paper bracket is already valid; repair refused")
        last_valid_stop: Decimal | None = None
        for row in sorted(rows, key=lambda item: (item["opened_at"], item["id"])):
            proposal = self.ledger.get_proposal(row["proposal_id"], include_private=True)
            summary = proposal.get("execution_summary") or {}
            economic = summary.get("economic_position") if isinstance(summary, dict) else None
            entry = Decimal(str((economic or {}).get("weighted_average_entry", summary.get("average_fill_price", row["average_fill_price"]))))
            stop = Decimal(str((economic or {}).get("final_stop", summary.get("stop_loss", row["final_stop"]))))
            target = Decimal(str((economic or {}).get("final_target", summary.get("take_profit", row["final_target"]))))
            try:
                validate_long_bracket(entry, stop, target, Decimal(proposal["reward_risk"]))
            except (PolicyError, KeyError, ValueError):
                break
            last_valid_stop = stop
        if last_valid_stop is None:
            raise PolicyError("no audited valid pre-scale-in stop could be derived; repair refused")
        target = aggregate_entry + self.settings.risk.min_reward_risk * (aggregate_entry - last_valid_stop)
        market = fetch_spot_snapshot(self.settings, symbol)
        validate_long_bracket(aggregate_entry, last_valid_stop, target, self.settings.risk.min_reward_risk, bid=market.bid, ask=market.ask)
        fee_rate = self.settings.risk.paper_fee_pct / Decimal("100")
        slippage_rate = self.settings.paper.slippage_pct / Decimal("100")
        cost = sum((Decimal(row["quote_spent"]) for row in rows), Decimal("0"))
        risk = cost - quantity * last_valid_stop * (Decimal("1") - slippage_rate) * (Decimal("1") - fee_rate)
        if risk <= 0 or risk > self.settings.paper.max_risk_per_position_usdt:
            raise PolicyError("repaired PAPER bracket violates the per-position risk limit")
        other_risk = Decimal("0")
        all_rows = self.ledger.list_paper_positions(open_only=True)
        for other_symbol in {row["symbol"] for row in all_rows if row["symbol"] != symbol}:
            group = [row for row in all_rows if row["symbol"] == other_symbol]
            other_qty = sum((Decimal(row["net_quantity"]) for row in group), Decimal("0"))
            other_entry = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in group), Decimal("0")) / other_qty
            other_stop = max(Decimal(row["final_stop"]) for row in group)
            other_target = min(Decimal(row["final_target"]) for row in group)
            validate_long_bracket(other_entry, other_stop, other_target, self.settings.risk.min_reward_risk)
            other_cost = sum((Decimal(row["quote_spent"]) for row in group), Decimal("0"))
            other_risk += other_cost - other_qty * other_stop * (Decimal("1") - slippage_rate) * (Decimal("1") - fee_rate)
        if other_risk + risk > self.settings.paper.max_aggregate_risk_usdt:
            raise PolicyError("repaired PAPER bracket violates the aggregate risk limit")
        snapshot = [{key: row[key] for key in ("id", "status", "net_quantity", "quote_spent", "average_fill_price", "final_stop", "final_target", "risk_amount")} for row in sorted(rows, key=lambda item: (item["opened_at"], item["id"]))]
        expected = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()
        repaired = self.ledger.repair_paper_bracket(symbol, expected, last_valid_stop, target, risk)
        return {"backup": str(backup), **repaired}

    def backup_and_reset_paper(self) -> dict[str, Any]:
        backup = self.ledger.backup("pre-paper-reset")
        try:
            result = self.ledger.reset_paper_account(Decimal("1000"), "hackathon demo reset")
        except Exception:
            # The consistent backup remains recoverable even when reset refuses.
            raise
        return {"backup": str(backup), **result}

    @localized
    def paper_balance_status(self) -> dict[str, Any]:
        raw = self.ledger.paper_balance()
        free = Decimal(raw["free_usdt"]); locked = Decimal(raw["locked_usdt"])
        return {"initial_reset_balance_usdt": format(Decimal(raw["initial_balance_usdt"]), "f"),
            "free_usdt": format(free, "f"), "locked_cost_basis_usdt": format(locked, "f"),
            "current_ledger_balance_usdt": format(free + locked, "f"),
            "realized_pnl_usdt": format(Decimal(raw["realized_pnl"]), "f"),
            "paid_fees_usdt": format(Decimal(raw["paid_fees_usdt"]), "f"),
            "assets": raw["assets"], "open_positions": raw["open_positions"],
            "active_tranches": raw["active_tranches"], "updated_at": raw["updated_at"]}

    @localized
    def paper_status(self) -> dict[str, Any]:
        tranches = self.ledger.list_paper_positions(open_only=True)
        grouped: list[dict[str, Any]] = []
        for symbol in sorted({row["symbol"] for row in tranches}):
            rows = [row for row in tranches if row["symbol"] == symbol]
            quantity = sum((Decimal(row["net_quantity"]) for row in rows), Decimal("0"))
            weighted = (sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in rows), Decimal("0")) / quantity) if quantity else Decimal("0")
            grouped.append({"symbol": symbol, "status": "OPEN", "economic_position_count": 1,
                "active_tranche_count": len(rows), "net_quantity": str(quantity),
                "weighted_average_entry": str(weighted),
                "quote_spent": str(sum((Decimal(row["quote_spent"]) for row in rows), Decimal("0"))),
                "final_stop": str(max(Decimal(row["final_stop"]) for row in rows)),
                "final_target": rows[0]["final_target"],
                "risk_amount": str(sum((Decimal(row["risk_amount"]) for row in rows), Decimal("0"))),
                "tranche_ids": [row["id"] for row in rows]})
        return {"balance": self.paper_balance_status(), "positions": grouped, "tranches": tranches,
                "economic_position_count": len(grouped), "active_tranche_count": len(tranches)}

    def monitor_paper_positions(self, notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        results, errors = [], []
        open_rows = self.ledger.list_paper_positions(open_only=True)
        for symbol in sorted({row["symbol"] for row in open_rows if row["status"] == "OPEN"}):
            rows = [row for row in open_rows if row["symbol"] == symbol and row["status"] == "OPEN"]
            representative = rows[0]
            try:
                quantity = sum((Decimal(row["net_quantity"]) for row in rows), Decimal("0"))
                if quantity <= 0:
                    raise PolicyError("invalid persisted PAPER quantity; position requires repair")
                aggregate_entry = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in rows), Decimal("0")) / quantity
                stop = max(Decimal(row["final_stop"]) for row in rows)
                target = min(Decimal(row["final_target"]) for row in rows)
                validate_long_bracket(aggregate_entry, stop, target, self.settings.risk.min_reward_risk)
                start_ms = int(min(parse_time(row["last_checked_at"]) for row in rows).timestamp() * 1000) + 1
                ending = int(time.time() * 1000)
                candles = fetch_1m_candles_since(self.settings, symbol, start_ms, ending)
                if not candles:
                    raise MarketError("no complete 1-minute candles are available since last check")
                trigger = reason = None
                for candle in candles:
                    opened, high, low = Decimal(str(candle.open)), Decimal(str(candle.high)), Decimal(str(candle.low))
                    if opened <= stop:
                        trigger, reason = opened, "STOP_GAP"
                    elif low <= stop and high >= target:
                        trigger, reason = stop, "STOP_BOTH_TOUCHED"
                    elif low <= stop:
                        trigger, reason = stop, "STOP_LOSS"
                    elif opened >= target or high >= target:
                        trigger, reason = target, "TAKE_PROFIT"
                    if reason:
                        break
                if reason:
                    values = {row["id"]: exit_values(self.settings, Decimal(row["net_quantity"]), trigger) for row in rows}
                    closed_rows = self.ledger.close_paper_symbol(symbol, reason, values)
                    balance = self.ledger.paper_balance(); delivery = None
                    total_fee = sum((Decimal(row["exit_fee"]) for row in closed_rows), Decimal("0"))
                    total_gross = sum((Decimal(row["gross_proceeds"]) for row in closed_rows), Decimal("0"))
                    total_net = sum((Decimal(row["net_proceeds"]) for row in closed_rows), Decimal("0"))
                    total_pnl = sum((Decimal(row["realized_pnl"]) for row in closed_rows), Decimal("0"))
                    if notify:
                        text = translate("position.auto_close", self.locale, symbol=symbol, reason=reason,
                            exit_price=number(trigger, self.locale), fee=number(total_fee, self.locale),
                            gross=number(total_gross, self.locale), net=number(total_net, self.locale),
                            pnl=number(total_pnl, self.locale), free=number(balance["free_usdt"], self.locale))
                        delivery = self.messenger.send_text(text, dry_run=dry_run).to_dict()
                    results.append({"position": closed_rows[0], "closed_tranches": closed_rows,
                        "balance": balance, "notification": delivery})
                else:
                    checked_at = isoformat(datetime.fromtimestamp(candles[-1].close_time / 1000, timezone.utc))
                    for row in rows:
                        self.ledger.record_position_check(row["id"], checked_at,
                            {"candles": len(candles), "last_close": str(candles[-1].close)})
                    results.append({"position_id": representative["id"], "symbol": symbol,
                        "status": "OPEN", "tranches": len(rows), "candles": len(candles)})
            except Exception as exc:
                invalid_bracket = "invalid PAPER bracket" in str(exc) or "invalid persisted PAPER" in str(exc)
                first = (self.ledger.record_invalid_bracket_once(representative["id"], str(exc)) if invalid_bracket
                         else self.ledger.record_exit_incomplete_once(representative["id"], str(exc)))
                delivery = None
                if notify and first:
                    alert_key = "position.alert.bracket" if invalid_bracket else "position.alert.exit"
                    delivery = self.messenger.send_text(
                        translate(alert_key, self.locale, identifier=representative["id"], error=error_text(exc, self.locale)),
                        dry_run=dry_run).to_dict()
                errors.append({"position_id": representative["id"], "symbol": symbol,
                    "error": str(exc), "alert": delivery})
        return {"ok": not errors, "results": results, "errors": errors}

    @localized
    def create_paper_close_proposal(self, position_id: str, sender_id: str, chat_id: str,
                                    percentage: Decimal = Decimal("100"),
                                    notify: bool = False, dry_run: bool = False,
                                    close_quantity: Decimal | None = None,
                                    close_quote_amount: Decimal | None = None) -> dict[str, Any]:
        self._validate_owner(sender_id)
        if chat_id != self.settings.telegram.chat_id:
            raise SecurityError("paper close chat does not match")
        selectors = sum(value is not None for value in (close_quantity, close_quote_amount))
        if selectors > 1:
            raise PolicyError("paper close must use exactly one selector")
        if percentage <= 0 or percentage > 100:
            raise PolicyError("paper close percentage must be greater than 0 and at most 100")
        position = self.ledger.get_paper_position(validate_simple_id(position_id, "position_id"))
        if position["status"] != "OPEN":
            raise PolicyError("paper position is not open")
        symbol=position["symbol"]; tranches=[r for r in self.ledger.list_paper_positions(True) if r["symbol"]==symbol and r["status"]=="OPEN"]
        if not tranches: raise PolicyError("paper economic position is not open")
        market=fetch_spot_snapshot(self.settings,symbol)
        aggregate=sum((Decimal(r["net_quantity"]) for r in tranches),Decimal("0"))
        selector = "percentage"
        if close_quantity is not None:
            selector = "quantity"
            if close_quantity <= 0 or close_quantity % market.step_size != 0:
                raise PolicyError("close quantity must be positive and match the exchange quantity step")
            if close_quantity >= aggregate:
                raise PolicyError("quantity close would be a full close; use 'close all SYMBOL positions'")
            proposed_quantity = close_quantity
            percentage = proposed_quantity / aggregate * Decimal("100")
        elif close_quote_amount is not None:
            selector = "quote"
            if close_quote_amount <= 0:
                raise PolicyError("close quote value must be greater than 0 USD")
            proposed_quantity = floor_to_step(close_quote_amount / market.bid, market.step_size)
            if proposed_quantity >= aggregate:
                raise PolicyError("quote close would be a full close; use 'close all SYMBOL positions'")
            percentage = proposed_quantity / aggregate * Decimal("100")
        else:
            proposed_quantity = aggregate if percentage == 100 else floor_to_step(aggregate*percentage/Decimal("100"),market.step_size)
        close_quantity = proposed_quantity
        remaining=aggregate-close_quantity
        # A percentage request is rounded down to the exchange LOT_SIZE.  Keep
        # the user's requested percentage distinct from the executable result.
        actual_executed_percentage = close_quantity / aggregate * Decimal("100")
        close_notional=close_quantity*market.bid
        if close_quantity<=0 or close_notional<market.min_notional:
            minimum_units=(market.min_notional/market.bid/market.step_size).to_integral_value(rounding=ROUND_CEILING)
            minimum_quote=minimum_units*market.step_size*market.bid
            raise PolicyError(f"partial close is below the current minimum notional request of {minimum_quote:f} USDT after downward step rounding")
        if percentage<100 and (remaining<market.step_size or remaining*market.bid<market.min_notional):
            raise PolicyError("partial close would leave untradeable dust; request a 100% close")
        total_cost=sum((Decimal(r["quote_spent"]) for r in tranches),Decimal("0")); avg_cost=total_cost/aggregate
        fee_rate=self.settings.risk.paper_fee_pct/Decimal("100"); gross=close_notional; fee=gross*fee_rate; net=gross-fee
        close_id=f"pc-{secrets.token_hex(6)}"; economic_id=f"pe-{hashlib.sha256(symbol.encode()).hexdigest()[:12]}"
        payload={"schema":"spotguard.paper-close.v2","close_id":close_id,"mode":"paper","symbol":symbol,
            "economic_position_id":economic_id,"position_id":position_id,"requested_percentage":str(percentage),
            "actual_executed_percentage":str(actual_executed_percentage),
            "selector": selector,
            "aggregate_quantity":str(aggregate),"close_quantity":str(close_quantity),"remaining_quantity":str(remaining),
            "average_cost":str(avg_cost),"reference_bid":str(market.bid),"step_size":str(market.step_size),
            "min_notional":str(market.min_notional),"estimated_gross_proceeds":str(gross),
            "estimated_fee_asset":"USDT","estimated_fee_usdt":str(fee),"estimated_net_proceeds":str(net),
            "estimated_realized_pnl":str(net-avg_cost*close_quantity),"owner_id":sender_id,"chat_id":chat_id,
            "nonce":secrets.token_urlsafe(12)}
        payload_json=canonical_json(payload); token=self.signer.approval_token(payload_json); code=self.signer.paper_confirmation_code(payload_json)
        values={"id":close_id,"position_id":position_id,"token_hash":self.signer.token_hash(token),
            "code_hash":self.signer.token_hash(code),"owner_id":sender_id,"chat_id":chat_id,
            "payload_json":payload_json,"payload_hash":hashlib.sha256(payload_json.encode()).hexdigest(),
            "requested_percentage":str(percentage),"close_quantity":str(close_quantity),
            "reference_bid":str(market.bid),"ttl_seconds":self.settings.paper.close_proposal_ttl_seconds}
        values["locale"] = self.locale
        close=self.ledger.create_paper_close_proposal(values); notification=None
        if notify:
            message,buttons=paper_close_message(payload,close,token,code, locale=self.locale)
            notification=self.messenger.send(message,buttons,dry_run=dry_run).to_dict()
        return {"close_proposal":close,"close":payload,"notification":notification}

    @localized
    def approve_paper_close(self, close_id: str, sender_id: str, chat_id: str,
                            token: str | None = None, code: str | None = None) -> dict[str, Any]:
        self._validate_owner(sender_id); close=self.ledger.get_paper_close_proposal(close_id,True)
        if token is None and code is None: raise SecurityError("paper close approval requires a callback token or confirmation code")
        payload_json=close.get("payload_json")
        if not payload_json or hashlib.sha256(payload_json.encode()).hexdigest()!=close.get("payload_hash"):
            raise SecurityError("paper close immutable payload is invalid")
        payload=__import__("json").loads(payload_json)
        if payload.get("mode")!="paper" or payload.get("owner_id")!=sender_id or payload.get("chat_id")!=chat_id:
            raise SecurityError("paper close payload binding is invalid")
        token_hash=self.signer.token_hash(token) if token else close["token_hash"]; code_hash=self.signer.token_hash(code) if code else None
        market=fetch_spot_snapshot(self.settings,payload["symbol"]); reference=Decimal(payload["reference_bid"])
        drift=abs(market.bid-reference)/reference*Decimal("100")
        if drift>self.settings.market.max_entry_drift_pct:
            self.ledger.terminalize_paper_close(close_id,"fresh price drift exceeds configured limit")
            raise PolicyError("fresh close price drift exceeds configured limit; proposal terminalized")
        current=sum((Decimal(r["net_quantity"]) for r in self.ledger.list_paper_positions(True) if r["symbol"]==payload["symbol"]),Decimal("0"))
        close_qty=Decimal(payload["close_quantity"]); remaining=current-close_qty
        full_close = Decimal(payload["requested_percentage"]) == Decimal("100")
        if current!=Decimal(payload["aggregate_quantity"]) or close_qty<=0 or close_qty>current or (not full_close and close_qty%market.step_size!=0):
            self.ledger.terminalize_paper_close(close_id,"economic position or exchange filters changed")
            raise PolicyError("paper economic position or quantity filters changed; proposal terminalized")
        if close_qty*market.bid<market.min_notional or (remaining>0 and (remaining<market.step_size or remaining*market.bid<market.min_notional)):
            self.ledger.terminalize_paper_close(close_id,"notional or residual dust filter changed")
            raise PolicyError("close or residual now fails exchange filters; proposal terminalized")
        self.ledger.claim_paper_close(close_id,token_hash,code_hash,sender_id,chat_id)
        try:
            result=self.ledger.execute_paper_close(close_id,market.bid,self.settings.risk.paper_fee_pct/Decimal("100"),self.settings.paper.slippage_pct/Decimal("100"),market.step_size)
        except Exception:
            self.ledger.finish_paper_close_proposal(close_id,"FAILED"); raise
        response={"ok":True,"message":"APPROVE PAPER CLOSE accepted; simulated aggregate close completed","close":result,
            "closed_tranches":result["closed_tranches"],"balance":self.ledger.paper_balance()}
        if result["closed_tranches"]:
            response["position"]=self.ledger.get_paper_position(result["closed_tranches"][0])
        return response

    @localized
    def approve_paper_close_by_position(self, position_id: str, code: str,
                                        sender_id: str, chat_id: str) -> dict[str, Any]:
        close = self.ledger.get_active_paper_close_for_position(position_id, True)
        return self.approve_paper_close(close["id"], sender_id, chat_id, code=code)

    @localized
    def reject_paper_close(self, close_id: str, token: str,
                           sender_id: str, chat_id: str) -> dict[str, Any]:
        self._validate_owner(sender_id)
        return self.ledger.reject_paper_close(close_id, self.signer.token_hash(token), sender_id, chat_id)

    @localized
    def reject_paper_close_by_position(self, position_id: str, code: str,
                                       sender_id: str, chat_id: str) -> dict[str, Any]:
        self._validate_owner(sender_id)
        close = self.ledger.get_active_paper_close_for_position(position_id, True)
        return self.ledger.reject_paper_close(close["id"], close["token_hash"], sender_id, chat_id,
                                              code_hash=self.signer.token_hash(code))

    @localized
    def create_manual_buy_proposal(self, symbol: str, quote_amount: Decimal, live: bool = False, notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        paper_context: PolicyContext | None = None
        paper_symbols: set[str] = set()
        raw_symbol = symbol.upper()
        symbol = raw_symbol if raw_symbol.endswith(self.settings.risk.quote_asset) else raw_symbol + self.settings.risk.quote_asset
        if symbol not in self.settings.market.symbols:
            raise SpotGuardError(f"symbol is not allowlisted: {symbol}")
        if live:
            if symbol not in self.settings.live.allowed_symbols:
                raise SecurityError("symbol is not enabled for live Spot intent")
            if not self.settings.live.enabled:
                raise SecurityError("LIVE_NOT_ENABLED: live trading is disabled")
            arm = self.live_arm.status()
            if not arm.armed:
                raise SecurityError("LIVE_NOT_ARMED: live trading is not armed on the VPS")
            if getattr(arm, "scope", "FULL") == "EXIT_ONLY":
                raise SecurityError("LIVE recovery arm is EXIT_ONLY; new LIVE entries are forbidden")
            if quote_amount <= 0:
                raise PolicyError("LIVE quote amount must be positive")
            ceiling = absolute_entry_ceiling(self.settings, "live")
            if ceiling is not None and quote_amount > ceiling:
                raise PolicyError(
                    f"requested amount {quote_amount} USDT exceeds configured maximum {decimal_string(ceiling)} USDT"
                )
            readiness = self.live_status(check_symbols=True)
            if not readiness["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")
            proposal_mode, source, ttl = "live", "manual-live", self.settings.live.approval_ttl_seconds
        else:
            if self.settings.mode != "paper":
                raise SecurityError("manual paper tests are only available when mode == paper")
            if quote_amount <= 0:
                raise PolicyError("PAPER quote amount must be positive")
            ceiling = absolute_entry_ceiling(self.settings, "paper")
            if ceiling is not None and quote_amount > ceiling:
                raise PolicyError(
                    f"requested amount {quote_amount} USDT exceeds configured maximum {decimal_string(ceiling)} USDT"
                )
            proposal_mode, source, ttl = "paper", "manual-paper-test", None
            paper_context, paper_symbols = self._ensure_paper_entry_available(
                symbol, quote_amount
            )
        market = fetch_spot_snapshot(self.settings, symbol)
        quantity = floor_to_step(quote_amount / market.ask, market.step_size)
        spend = quantity * market.ask
        if quantity <= 0 or spend < market.min_notional:
            minimum_units = (market.min_notional / market.ask / market.step_size).to_integral_value(rounding=ROUND_CEILING)
            minimum_quote = minimum_units * market.step_size * market.ask
            raise PolicyError(
                "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE: "
                f"requested {quote_amount} USDT is below Binance minimum notional request {minimum_quote:f} USDT after downward quantity-step rounding; amount was not increased"
            )
        klines = fetch_klines(self.settings, symbol)
        snapshot = analyze(klines)
        fingerprint = hashlib.sha256(f"{source}:{symbol}:{time.time_ns()}:{secrets.token_hex(4)}".encode()).hexdigest()
        signal = Signal(candidate_id=f"c-{fingerprint[:12]}", fingerprint=fingerprint, symbol=symbol, interval=self.settings.market.interval, side="BUY", score=snapshot.score, price=float(market.ask), candle_close_time=klines[-1].close_time, reasons=("manual test intent; signal score explicitly bypassed",), metrics={**snapshot.to_dict(), "agent_os_confirmed": False, "manual_signal_bypass": True, "source": source, "exchange_min_notional": str(market.min_notional), "quantity_step": str(market.step_size)})
        candidate, _ = self.ledger.create_candidate(signal, self.settings.market.candidate_ttl_minutes)
        values = build_proposal(self.settings, candidate, market.bid, market.ask, quote_amount, "Manual intent; signal-score requirement bypassed only.", proposal_mode=proposal_mode, source=source, ttl_seconds=ttl)
        if not live:
            assert paper_context is not None
            existing_exposure, existing_risk = self._paper_symbol_usage(symbol)
            sizing: SizingDecision | None = None
            if self.settings.sizing_policy.percentage_based:
                sizing = size_entry(
                    paper_context,
                    entry_price=Decimal(values["entry_reference"]),
                    stop_price=Decimal(values["stop_reference"]),
                    existing_position_exposure=existing_exposure,
                    existing_position_risk=existing_risk,
                    requested_notional=quote_amount,
                    fee_buffer_rate=self.settings.risk.paper_fee_pct / Decimal("100"),
                )
                self._raise_sizing_rejection(sizing, phase="proposal")
            reference_plan = build_fill_risk(self.settings, values, market.ask, quantity)
            projection = self._paper_risk_projection(
                symbol, market.ask, reference_plan, bid=market.bid, ask=market.ask,
                reward_risk=Decimal(values["reward_risk"]),
                effective_limits=paper_context.effective_limits,
            )
            values["canonical"].update({
                "projected_risk_at_stop": str(projection["new_position_risk"]),
                "projected_aggregate_risk": str(projection["projected_aggregate_risk"]),
                "reference_quantity": reference_plan["net_base_quantity"],
                "gross_reference_quantity": reference_plan["gross_base_quantity"],
                "fee_estimate_base": reference_plan["entry_fee_base"],
                "fee_estimate_asset": symbol[:-4],
                "fee_estimate_usdt": str(Decimal(reference_plan["entry_fee_base"]) * market.ask),
            })
            values["canonical_json"] = canonical_json(values["canonical"])
            existing = [row for row in self.ledger.list_paper_positions(True) if row["symbol"] == symbol]
            if existing:
                fee_rate = self.settings.risk.paper_fee_pct / Decimal("100")
                old_qty = Decimal(projection["existing_quantity"])
                weighted = Decimal(projection["projected_entry"])
                projected_qty = Decimal(projection["projected_quantity"])
                projected_stop = Decimal(projection["effective_stop"])
                projected_target = Decimal(projection["projected_target"])
                values["canonical"].update({"scale_in": True, "existing_quantity": str(old_qty),
                    "new_proposed_tranche_quantity": reference_plan["net_base_quantity"],
                    "existing_average_entry": str(sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in existing), Decimal("0")) / old_qty),
                    "projected_weighted_average_entry": str(weighted),
                    "existing_exposure": str(sum((Decimal(row["quote_spent"]) for row in existing), Decimal("0"))),
                    "projected_exposure": str(sum((Decimal(row["quote_spent"]) for row in existing), Decimal("0")) + spend),
                    "projected_stop": str(projected_stop), "projected_target": str(projected_target),
                    "projected_risk_at_stop": str(projection["new_position_risk"]),
                    "projected_aggregate_risk": str(projection["projected_aggregate_risk"]),
                    "active_tranche_count": len(existing), "projected_tranche_count": len(existing) + 1,
                    "fee_estimate_base": reference_plan["entry_fee_base"],
                    "fee_estimate_asset": symbol[:-4],
                    "fee_estimate_usdt": str(Decimal(reference_plan["entry_fee_base"]) * market.ask)})
                values["canonical_json"] = canonical_json(values["canonical"])

            evaluation = self._evaluate_paper_entry(
                paper_context, paper_symbols, symbol, quote_amount,
                projected_position_risk=Decimal(str(projection["new_position_risk"])),
                projected_aggregate_risk=Decimal(str(projection["projected_aggregate_risk"])),
                projected_position_exposure=existing_exposure + spend,
                estimated_fee=(
                    sizing.estimated_fee_buffer if sizing is not None else Decimal("0")
                ),
            )
            self._attach_policy_snapshot(
                values, paper_context, evaluation, sizing,
                Decimal(str(reference_plan["gross_base_quantity"])),
            )

        if live:
            self._prepare_live_entry_proposal(values, quote_amount, market, readiness["execution_ready"])
        token = self.signer.approval_token(values["canonical_json"])
        code = self.signer.paper_confirmation_code(values["canonical_json"]) if values["mode"] == "paper" else None
        values["locale"] = self.locale
        proposal = self.ledger.create_proposal(
            values, self.signer.token_hash(token),
            self._active_proposal_limit(proposal_mode),
            self.signer.token_hash(code) if code else None,
        )
        self.ledger.add_event("manual.proposal", proposal["id"], {"source": source, "symbol": symbol, "quote_amount": str(quote_amount)})
        result = {"proposal": proposal, "notification": None}
        if notify:
            try:
                result["notification"] = self.notify_proposal(
                    proposal["id"], dry_run=dry_run, token=token, confirmation_code=code)
            except TelegramError as exc:
                result["notification"] = {"delivered": False, "transport": "failed",
                    "controls_present": False, "error": bounded_text(str(exc), 300)}
                result["delivery_message"] = "PAPER proposal exists, but Telegram notification delivery failed; use the safe resend command."
                self.ledger.add_event("proposal.notification_failed", proposal["id"], {"reason_type": type(exc).__name__})
            else:
                controls = result["notification"].get("payload", {}).get("presentation", {}).get("blocks", [{}])[0].get("buttons", [])
                result["notification"]["controls_present"] = len(controls) >= 2
                result["delivery_message"] = ("Approval controls delivered." if result["notification"].get("delivered") and len(controls) >= 2
                    else "PAPER proposal exists; notification was not delivered.")
        return result

    def _prepare_live_entry_proposal(
        self, values: dict[str, Any], quote_amount: Decimal,
        market: Any, execution_ready: bool, *, auto_size: bool = False,
    ) -> None:
        """Attach the immutable, exchange-aligned LIVE terms every entry needs.

        Both manual and scheduled/candidate entries call this one function so
        the latter cannot create a LIVE proposal that skips risk snapshot or
        protected OTOCO terms.
        """
        canonical = values["canonical"]
        symbol = str(canonical["symbol"])
        if getattr(self.live_arm.status(), "scope", "FULL") == "EXIT_ONLY":
            raise SecurityError("LIVE recovery arm is EXIT_ONLY; new LIVE entries are forbidden")
        if symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("symbol is not enabled for live Spot intent")
        ceiling = absolute_entry_ceiling(self.settings, "live")
        if quote_amount <= 0 or (
            ceiling is not None and quote_amount > ceiling
        ):
            raise PolicyError("LIVE quote amount exceeds the applicable entry limit")
        market_ask = Decimal(str(market.ask))
        slippage_cap_pct = self.settings.live.entry_slippage_cap_pct
        theoretical_cap = market_ask * (Decimal("1") + slippage_cap_pct / Decimal("100"))
        # Floor, never ceil: the exchange-aligned LIMIT must never exceed the
        # configured percentage cap. A BUY LIMIT at/above the observed ask is
        # marketable while still bounding the worst executable price.
        limit_price = floor_to_step(theoretical_cap, market.price_tick_size)
        if limit_price < market_ask:
            raise PolicyError(
                "LIVE slippage cap is too tight for the exchange tick; create a new proposal"
            )
        stop_price = floor_to_step(Decimal(values["stop_reference"]), market.price_tick_size)
        reward_risk = Decimal(values["reward_risk"])
        target_price = limit_price + reward_risk * (limit_price - stop_price)
        if stop_price <= 0 or not stop_price < limit_price < target_price:
            raise PolicyError("LIVE tick-aligned bracket is invalid")
        if auto_size and self.settings.sizing_policy.percentage_based:
            preview = self._validate_live_entry_limits(
                symbol,
                limit_price,
                Decimal("1"),
                limit_price - stop_price,
                market.bid,
                explain=True,
                auto_size=True,
            )
            quote_amount = Decimal(str(preview["calculated_notional"]))
            if quote_amount <= 0:
                raise PolicyError(
                    "REVALIDATION_FAILED: risk engine found no positive LIVE capacity"
                )
            values["quote_amount"] = str(quote_amount)
            canonical["quote_amount"] = str(quote_amount)
        live_quantity = floor_to_step(quote_amount / limit_price, market.step_size)
        if live_quantity <= 0 or live_quantity * limit_price < market.min_notional:
            raise PolicyError(
                "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE: requested LIVE amount is below Binance minimum notional after rounding; amount will not be increased"
            )
        pending_quantity = floor_to_step(
            live_quantity * (Decimal("1") - self.settings.risk.paper_fee_pct / Decimal("100")), market.step_size,
        )
        if pending_quantity <= 0:
            raise PolicyError("LIVE protective quantity is zero after fee and LOT_SIZE rounding")
        risk_at_stop = live_quantity * (limit_price - stop_price)
        if (absolute_entry_ceiling(self.settings, "live") is not None
                and risk_at_stop > self.settings.live.max_risk_per_position_usdt):
            raise PolicyError(f"LIVE risk at stop exceeds {self.settings.live.max_risk_per_position_usdt} USDT")
        projection = self._validate_live_entry_limits(symbol, quote_amount, live_quantity, risk_at_stop, market.bid)
        values.update({"entry_reference": str(limit_price), "stop_reference": str(stop_price),
                       "take_profit_reference": str(target_price)})
        canonical.update({"entry_reference": str(limit_price), "entry_limit_price": str(limit_price),
            "entry_market_ask": str(market_ask),
            "entry_slippage_cap_pct": str(slippage_cap_pct),
            "entry_slippage_cap_price": str(limit_price),
            "entry_execution_style": "MARKETABLE_LIMIT_HARD_CAP",
            "stop_reference": str(stop_price), "take_profit_reference": str(target_price),
            "quantity": str(live_quantity), "pending_quantity": str(pending_quantity),
            "price_tick_size": str(market.price_tick_size), "market_step_size": str(market.step_size),
            "risk_at_stop": str(risk_at_stop), "fee_estimate": str(quote_amount * self.settings.risk.paper_fee_pct / Decimal("100")),
            "protection": "OPO_WITH_PENDING_SELL_OCO_REQUIRED",
            "execution_ready_at_creation": execution_ready, **projection})
        policy_snapshot = canonical.get("policy_snapshot")
        if self.settings.sizing_policy.percentage_based and not isinstance(
            policy_snapshot, dict
        ):
            raise PolicyError(
                "REVALIDATION_FAILED: LIVE policy evaluator returned no immutable snapshot"
            )
        sizing_snapshot = (
            policy_snapshot.get("sizing") or {}
            if isinstance(policy_snapshot, dict) else {}
        )
        proposal_terms = {
            "proposal_id": canonical["proposal_id"],
            "created_at": canonical["created_at"],
            "expires_at": canonical["expires_at"],
            "symbol": symbol,
            "side": "BUY",
            "strategy_or_signal_reference": canonical.get("candidate_id"),
            "source": canonical.get("source"),
            "entry_price": str(limit_price),
            "market_ask_at_proposal": str(market_ask),
            "entry_slippage_cap_pct": str(slippage_cap_pct),
            "entry_slippage_cap_price": str(limit_price),
            "entry_execution_style": "MARKETABLE_LIMIT_HARD_CAP",
            "stop_price": str(stop_price),
            "target_price": str(target_price),
            "stop_distance_pct": sizing_snapshot.get("stop_distance_pct"),
            "risk_budget_at_proposal": sizing_snapshot.get("risk_budget"),
            "calculated_notional": str(quote_amount),
            "calculated_quantity": str(live_quantity),
            "expected_risk": str(risk_at_stop),
        }
        if isinstance(policy_snapshot, dict):
            policy_snapshot["proposal_terms"] = proposal_terms
        values["canonical_json"] = canonical_json(canonical)

    def _validate_live_entry_limits(self, symbol: str, quote_amount: Decimal,
                                    quantity: Decimal, risk_at_stop: Decimal,
                                    bid: Decimal, *, explain: bool = False,
                                    proposal_equity: Decimal | None = None,
                                    auto_size: bool = False) -> dict[str, Any]:
        """Fail closed on independently read LIVE account/order/trade evidence.

        Existing holdings require complete balance, mark-price, and protective
        OCO evidence. Missing evidence blocks a new entry instead of silently
        under-reporting exposure or stop-risk.
        """
        account = self.live_executor.read_spot_account()
        orders = self.live_executor.read_open_spot_orders()
        quote_asset = self.settings.risk.quote_asset

        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for order in orders:
            list_id = order.get("orderListId")
            if (order.get("symbol") in self.settings.live.allowed_symbols
                    and isinstance(list_id, int) and list_id > 0):
                grouped.setdefault((str(order["symbol"]), list_id), []).append(order)
        active_tranches = len(grouped)
        active_symbols = {entry_symbol for entry_symbol, _ in grouped}
        if active_tranches + 1 > self.settings.live.max_active_tranches:
            raise PolicyError(
                f"LIVE active tranche limit reached ({self.settings.live.max_active_tranches})"
            )

        protected_quantities: dict[str, Decimal] = {}
        snapshots: dict[str, Any] = {}
        balance_classifications: list[dict[str, Any]] = []
        risk_by_symbol: dict[str, Decimal] = {}
        exposure_by_symbol: dict[str, Decimal] = {}
        existing_exposure = Decimal("0")
        for (existing_symbol, _), legs in grouped.items():
            validated = self.live_executor.validate_active_protective_oco(legs, existing_symbol)
            snapshot = fetch_spot_snapshot(self.settings, existing_symbol)
            snapshots[existing_symbol] = snapshot
            protected_quantity = validated["quantity"]
            protected_quantities[existing_symbol] = (
                protected_quantities.get(existing_symbol, Decimal("0"))
                + protected_quantity
            )
            marked_exposure = protected_quantity * snapshot.bid
            existing_exposure += marked_exposure
            exposure_by_symbol[existing_symbol] = (
                exposure_by_symbol.get(existing_symbol, Decimal("0"))
                + marked_exposure
            )
            protective_stop = validated["stop"]
            protective_target = validated["target"]
            ratio = self.settings.risk.min_reward_risk
            inferred_entry = (
                protective_target + ratio * protective_stop
            ) / (Decimal("1") + ratio)
            if not protective_stop < inferred_entry < protective_target:
                self._mark_live_epoch_reconcile("protected entry basis is not reconstructable")
                raise SecurityError(
                    "LIVE protected entry basis cannot be reconstructed; reconciliation required"
                )
            risk_by_symbol[existing_symbol] = (
                risk_by_symbol.get(existing_symbol, Decimal("0"))
                + protected_quantity * (inferred_entry - protective_stop)
            )

        free_quote = Decimal("0")
        locked_quote = Decimal("0")
        held_by_symbol: dict[str, Decimal] = {}
        for balance in account["balances"]:
            asset = str(balance["asset"])
            free = Decimal(str(balance["free"]))
            locked = Decimal(str(balance.get("locked", "0")))
            if min(free, locked) < 0 or not free.is_finite() or not locked.is_finite():
                raise SecurityError("LIVE Spot balance snapshot is invalid")
            if asset == quote_asset:
                free_quote += free
                locked_quote += locked
                continue
            held = free + locked
            if held <= 0:
                continue
            matching_symbol = next(
                (candidate for candidate in self.settings.live.allowed_symbols
                 if candidate == asset + quote_asset), None
            )
            if matching_symbol is None:
                if self.settings.sizing_policy.enabled:
                    self._mark_live_epoch_reconcile(f"unvalued Spot asset {asset}")
                    raise SecurityError(
                        f"LIVE Spot asset {asset} cannot be valued in configured {quote_asset}; reconciliation required"
                    )
                continue
            held_by_symbol[matching_symbol] = held
            filters = validate_spot_symbol(self.settings, matching_symbol, live=True)
            if matching_symbol not in snapshots:
                snapshots[matching_symbol] = fetch_spot_snapshot(self.settings, matching_symbol)
            balance_state = classify_spot_base_balance(held, filters, snapshots[matching_symbol].bid)
            balance_classifications.append({"symbol": matching_symbol, "balance": format(held, "f"), **balance_state})
            if not balance_state["tradable"]:
                held_by_symbol.pop(matching_symbol, None)
                continue
            if held != protected_quantities.get(matching_symbol, Decimal("0")):
                self._mark_live_epoch_reconcile(f"unexplained balance for {matching_symbol}")
                raise SecurityError(
                    "LIVE base balance is not fully protected by an auditable OCO; reconciliation required"
                )

        for protected_symbol, protected_quantity in protected_quantities.items():
            if held_by_symbol.get(protected_symbol, Decimal("0")) != protected_quantity:
                self._mark_live_epoch_reconcile(f"unexplained balance for {protected_symbol}")
                raise SecurityError(
                    "LIVE OCO quantity does not match Spot balance; reconciliation required"
                )

        valuations: list[AssetValuation] = []
        for held_symbol, held in sorted(held_by_symbol.items()):
            snapshot = snapshots.get(held_symbol)
            if snapshot is None:
                snapshot = fetch_spot_snapshot(self.settings, held_symbol)
                snapshots[held_symbol] = snapshot
            valuations.append(AssetValuation(
                asset=held_symbol[:-len(quote_asset)], symbol=held_symbol,
                quantity=held, mark_price=snapshot.bid,
                quote_value=held * snapshot.bid,
            ))

        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        accounting_ok, daily_loss, weekly_loss, accounting_reason = self._live_session_accounting()
        if not accounting_ok:
            raise SecurityError(accounting_reason or "RISKPILOT_SESSION_PNL_INCOMPLETE")
        entries_today = self.ledger.committed_live_executions(day_start.date().isoformat())
        if entries_today >= self.settings.live.max_successful_entries_per_utc_day:
            raise PolicyError("LIVE daily entry quota reached "
                              f"({self.settings.live.max_successful_entries_per_utc_day})")
        raw_equity = (
            free_quote
            + locked_quote
            + sum((item.quote_value for item in valuations), Decimal("0"))
        )
        effective_equity = (
            min(raw_equity, proposal_equity)
            if proposal_equity is not None else raw_equity
        )
        equity = build_equity_snapshot(
            mode="live", quote_asset=quote_asset,
            free_quote=free_quote, locked_quote=locked_quote,
            reserve_quote=effective_reserve(
                self.settings, "live", effective_equity
            ),
            asset_valuations=valuations,
        )
        current_aggregate_risk = sum(risk_by_symbol.values(), Decimal("0"))
        usage = UsageSnapshot(
            open_exposure=existing_exposure,
            aggregate_open_risk=current_aggregate_risk,
            daily_realized_loss=daily_loss,
            economic_positions=len(active_symbols),
            active_tranches=active_tranches,
            weekly_realized_loss=weekly_loss,
        )
        hard, effective = limits_for(
            self.settings, "live", equity.equity,
            effective_equity=effective_equity,
        )
        context = PolicyContext(equity, usage, hard, effective)
        self._validate_equity_drift(proposal_equity, equity.equity)
        sizing: SizingDecision | None = None
        if self.settings.sizing_policy.percentage_based and quantity > 0:
            sizing_entry = quote_amount / quantity
            sizing_stop = sizing_entry - risk_at_stop / quantity
            sizing = size_entry(
                context,
                entry_price=sizing_entry,
                stop_price=sizing_stop,
                existing_position_exposure=exposure_by_symbol.get(
                    symbol, Decimal("0")
                ),
                existing_position_risk=risk_by_symbol.get(symbol, Decimal("0")),
                requested_notional=None if auto_size else quote_amount,
                fee_buffer_rate=self.settings.risk.paper_fee_pct / Decimal("100"),
            )
            if not sizing.accepted and not explain:
                raise PolicyError(
                    "REVALIDATION_FAILED: ACCOUNT_STATE_CHANGED: immutable LIVE amount was not resized; "
                    + "; ".join(sizing.reasons)
                )
            if auto_size:
                original_notional = quote_amount
                quote_amount = sizing.calculated_notional
                risk_at_stop = (
                    risk_at_stop * quote_amount / original_notional
                    if original_notional > 0 else Decimal("0")
                )
        projected_exposure = existing_exposure + quote_amount
        projected_position_exposure = (
            exposure_by_symbol.get(symbol, Decimal("0")) + quote_amount
        )
        projected_position_risk = risk_by_symbol.get(symbol, Decimal("0")) + risk_at_stop
        projected_aggregate_risk = current_aggregate_risk + risk_at_stop
        evaluation = evaluate_entry(
            context,
            requested_notional=quote_amount,
            projected_exposure=projected_exposure,
            projected_position_exposure=projected_position_exposure,
            projected_position_risk=projected_position_risk,
            projected_aggregate_risk=projected_aggregate_risk,
            resulting_economic_positions=len(active_symbols | {symbol}),
            estimated_fee=(
                quote_amount * self.settings.risk.paper_fee_pct / Decimal("100")
                if self.settings.sizing_policy.percentage_based
                else Decimal("0")
            ),
        )
        if not explain:
            self._raise_policy_rejection(evaluation, phase="LIVE entry")
        projected_free = free_quote - quote_amount
        return {"live_risk_snapshot_verified": True,
                "projected_free_balance": str(projected_free),
                "projected_total_exposure": str(projected_exposure),
                "projected_position_risk": str(projected_position_risk),
                "projected_aggregate_risk": str(projected_aggregate_risk),
                "live_entries_today": entries_today,
                "live_daily_realized_loss": format(daily_loss, "f"),
                "live_weekly_realized_loss": format(weekly_loss, "f"),
                "account_trade_history_available": False,
                "account_global_realized_loss_verified": False,
                "account_global_realized_loss_reason": "ACCOUNT_TRADE_HISTORY_CAPABILITY_UNAVAILABLE",
                "realized_loss_scope": "riskpilot_session",
                "riskpilot_session_accounting_verified": accounting_ok,
                "existing_base_balance_classifications": balance_classifications,
                "active_live_tranches": active_tranches,
                "active_live_economic_positions": len(active_symbols),
                "calculated_notional": str(quote_amount),
                "policy_snapshot": build_policy_snapshot(
                    self.settings, "live", context, evaluation, sizing,
                    calculated_quantity=quantity,
                )}

    @staticmethod
    def _is_live_buy_entry(canonical: dict[str, Any]) -> bool:
        """Identify every protected LIVE BUY entry independently of its source."""
        try:
            return (canonical.get("product") == "SPOT" and canonical.get("side") == "BUY"
                    and canonical.get("order_type") in {"LIMIT", "LIMIT_MAKER"}
                    and Decimal(str(canonical.get("quote_amount", "0"))) > 0)
        except (ArithmeticError, ValueError):
            return False

    def _revalidate_live_buy_entry(self, proposal: dict[str, Any]) -> None:
        canonical = proposal["canonical"]
        if not self._is_live_buy_entry(canonical):
            return
        self._validate_stored_policy_snapshot(proposal)
        required = ("quote_amount", "quantity", "risk_at_stop", "entry_limit_price")
        if any(key not in canonical for key in required):
            raise SecurityError("LIVE BUY proposal is missing immutable risk terms")
        market = fetch_spot_snapshot(self.settings, proposal["symbol"])
        quantity = Decimal(str(canonical["quantity"]))
        entry = Decimal(str(canonical["entry_limit_price"]))
        cap = Decimal(str(canonical.get("entry_slippage_cap_price", canonical["entry_limit_price"])))
        if cap != entry:
            raise SecurityError("LIVE BUY immutable slippage cap does not match entry LIMIT")
        if market.ask > cap:
            raise PolicyError(
                "SLIPPAGE_CAP_EXCEEDED: fresh ask is above the approved marketable LIMIT; "
                "no order was submitted, request a requote"
            )
        stop = Decimal(str(canonical["stop_reference"]))
        target = Decimal(str(canonical["take_profit_reference"]))
        if (quantity <= 0 or quantity % market.step_size != 0
                or entry % market.price_tick_size != 0
                or stop % market.price_tick_size != 0
                or target % market.price_tick_size != 0):
            raise PolicyError(
                "EXCHANGE_FILTER_FAILED: fresh Binance quantity/price filters no longer match immutable proposal"
            )
        if quantity * entry < market.min_notional:
            raise PolicyError(
                "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE: fresh Binance minimum notional rejects immutable proposal; amount was not increased"
            )
        self._validate_live_entry_limits(
            proposal["symbol"], Decimal(str(canonical["quote_amount"])),
            quantity, Decimal(str(canonical["risk_at_stop"])), market.bid,
            proposal_equity=self._proposal_equity(proposal),
        )

    @localized
    def create_live_partial_exit_proposal(self, symbol: str, percentage: Decimal,
                                          notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Create a dormant owner-approved OCO cancel → partial sell → OCO re-arm plan."""
        raw_symbol = symbol.upper()
        symbol = raw_symbol if raw_symbol.endswith(self.settings.risk.quote_asset) else raw_symbol + self.settings.risk.quote_asset
        if not Decimal("0") < percentage <= Decimal("100"):
            raise PolicyError("live partial exit percentage must be greater than 0 and at most 100")
        if symbol not in self.settings.market.symbols or symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("symbol is not enabled for live Spot intent")
        if not self.settings.live.enabled or not self.live_arm.status().armed:
            raise SecurityError("live trading is disabled or not armed on the VPS")
        if getattr(self.live_arm.status(), "scope", "FULL") != "EXIT_ONLY":
            readiness = self.live_status(check_symbols=True, symbols=[symbol])
            if not readiness["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")

        grouped: dict[int, list[dict[str, Any]]] = {}
        for order in self.live_executor.read_open_spot_orders():
            list_id = order.get("orderListId")
            if order.get("symbol") == symbol and isinstance(list_id, int) and list_id > 0:
                grouped.setdefault(list_id, []).append(order)
        if len(grouped) != 1:
            raise PolicyError("exactly one active two-leg Spot OCO protection list is required for a live partial exit")
        order_list_id, legs = next(iter(grouped.items()))
        exchange = validate_spot_symbol(self.settings, symbol)
        step = Decimal(exchange["market_step_size"])
        tick = Decimal(exchange["price_tick_size"])
        try:
            validated = self.live_executor.validate_active_protective_oco(legs, symbol, tick=tick)
        except SecurityError as exc:
            raise PolicyError(str(exc)) from exc
        order_list_id = validated["order_list_id"]
        protected_quantity = validated["quantity"]
        stop_leg, target_leg = validated["stop_leg"], validated["target_leg"]
        market = fetch_spot_snapshot(self.settings, symbol)
        sell_quantity = protected_quantity if percentage == Decimal("100") else floor_to_step(protected_quantity * percentage / Decimal("100"), step)
        remaining_quantity = protected_quantity - sell_quantity
        if sell_quantity <= 0 or sell_quantity * market.bid < Decimal(exchange["min_notional"]):
            raise PolicyError("requested partial exit is below Binance minimum notional after LOT_SIZE rounding")
        if percentage < Decimal("100") and (remaining_quantity <= 0 or remaining_quantity * market.bid < Decimal(exchange["min_notional"])):
            raise PolicyError("partial exit would leave unprotectable dust; use sell all instead")
        stop, target = validated["stop"], validated["target"]
        if tick <= 0 or stop <= 0 or target <= stop or stop % tick or target % tick:
            raise PolicyError("active OCO bracket is not valid for safe re-arm")

        fingerprint = hashlib.sha256(f"manual-live-partial-exit:{symbol}:{order_list_id}:{percentage}:{time.time_ns()}".encode()).hexdigest()
        signal = Signal(candidate_id=f"c-{fingerprint[:12]}", fingerprint=fingerprint, symbol=symbol,
            interval=self.settings.market.interval, side="BUY", score=0, price=float(market.bid),
            candle_close_time=int(time.time() * 1000), reasons=("manual live partial exit; approval required",),
            metrics={"manual_live_partial_exit": True, "order_list_id": order_list_id, "percentage": str(percentage)})
        candidate, _ = self.ledger.create_candidate(signal, self.settings.market.candidate_ttl_minutes)
        now = utcnow(); proposal_id = f"p-{secrets.token_hex(6)}"
        canonical = {"schema": "spotguard.order.v1", "proposal_id": proposal_id, "candidate_id": candidate["id"],
            "product": "SPOT", "symbol": symbol, "side": "SELL", "order_type": "PARTIAL_EXIT",
            "quote_asset": self.settings.risk.quote_asset, "quote_amount": "0", "entry_reference": str(market.bid),
            "stop_reference": str(stop), "take_profit_reference": str(target), "reward_risk": "0",
            "order_list_id": order_list_id, "cancel_order_id": stop_leg["orderId"],
            "protected_order_ids": sorted(row["orderId"] for row in legs if isinstance(row.get("orderId"), int)),
            "percentage": str(percentage), "protected_quantity": str(protected_quantity),
            "sell_quantity": str(sell_quantity), "remaining_quantity": str(remaining_quantity),
            "market_step_size": str(step), "price_tick_size": str(tick),
            "mode": "live", "source": "manual-live-partial-exit", "approval_owner_id": self.settings.openclaw.telegram_owner_id,
            "approval_chat_id": self.settings.telegram.chat_id, "approval_nonce": secrets.token_urlsafe(12),
            "created_at": isoformat(now), "expires_at": isoformat(now + timedelta(seconds=self.settings.live.approval_ttl_seconds))}
        values = {"id": proposal_id, "candidate_id": candidate["id"], "symbol": symbol, "side": "BUY", "product": "SPOT",
            "order_type": "MARKET", "quote_amount": "0", "entry_reference": str(market.bid), "stop_reference": str(stop),
            "take_profit_reference": str(target), "reward_risk": "0", "rationale": "Cancel exact OCO, sell requested percentage, then re-arm unchanged TP/SL; approval required.",
            "mode": "live", "source": "manual-live-partial-exit", "created_at": canonical["created_at"], "expires_at": canonical["expires_at"],
            "canonical": canonical, "canonical_json": canonical_json(canonical), "locale": self.locale}
        token = self.signer.approval_token(values["canonical_json"])
        proposal = self.ledger.create_proposal(values, self.signer.token_hash(token), self.settings.risk.max_active_proposals)
        self.ledger.add_event("manual.live_partial_exit.proposal", proposal["id"], {"symbol": symbol, "percentage": str(percentage), "sell_quantity": str(sell_quantity), "remaining_quantity": str(remaining_quantity), "order_list_id": order_list_id})
        result: dict[str, Any] = {"proposal": proposal, "notification": None}
        if notify:
            result["notification"] = self.notify_proposal(proposal["id"], dry_run=dry_run, token=token)
        return result

    @localized
    def create_live_restore_protection_proposal(self, symbol: str, notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Restore the last approved TP/SL bracket to the current free Spot balance."""
        raw = symbol.upper(); symbol = raw if raw.endswith(self.settings.risk.quote_asset) else raw + self.settings.risk.quote_asset
        if symbol not in self.settings.live.allowed_symbols or not self.settings.live.enabled or not self.live_arm.status().armed:
            raise SecurityError("live protection restore is disabled or not armed")
        if getattr(self.live_arm.status(), "scope", "FULL") != "EXIT_ONLY":
            if not self.live_status(check_symbols=True, symbols=[symbol])["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")
        if any(row.get("symbol") == symbol and isinstance(row.get("orderListId"), int) and row["orderListId"] > 0 for row in self.live_executor.read_open_spot_orders()):
            raise PolicyError("active Spot OCO protection already exists; restore was not created")
        prior = next((row["canonical"] for row in self.ledger.list_proposals(100)
                      if row["symbol"] == symbol and row["mode"] == "live" and row["status"] in {"EXECUTED", "RECONCILE"} and row.get("canonical", {}).get("stop_reference") not in {None, "0"}
                      and row.get("canonical", {}).get("take_profit_reference") not in {None, "0"}), None)
        if prior is None:
            raise PolicyError("no prior approved TP/SL bracket is available; specify a new protected entry instead")
        exchange = validate_spot_symbol(self.settings, symbol); market = fetch_spot_snapshot(self.settings, symbol)
        step=Decimal(exchange["market_step_size"]); tick=Decimal(exchange["price_tick_size"])
        base=symbol[:-len(self.settings.risk.quote_asset)]; account=self.live_executor.read_spot_account()
        quantity=floor_to_step(next((Decimal(row["free"]) for row in account["balances"] if row["asset"] == base), Decimal("0")), step)
        stop=Decimal(str(prior["stop_reference"])); target=Decimal(str(prior["take_profit_reference"]))
        if quantity <= 0 or quantity * market.bid < Decimal(exchange["min_notional"]): raise PolicyError("free Spot balance is below Binance minimum for protection")
        if not stop < market.bid < target or stop % tick or target % tick: raise PolicyError("prior TP/SL bracket is no longer safe at the current market price")
        now=utcnow(); proposal_id=f"p-{secrets.token_hex(6)}"; fingerprint=hashlib.sha256(f"manual-live-set-protection:{symbol}:{time.time_ns()}".encode()).hexdigest()
        signal=Signal(candidate_id=f"c-{fingerprint[:12]}",fingerprint=fingerprint,symbol=symbol,interval=self.settings.market.interval,side="BUY",score=0,price=float(market.bid),candle_close_time=int(time.time()*1000),reasons=("restore live TP/SL; approval required",),metrics={"manual_live_set_protection":True})
        candidate,_=self.ledger.create_candidate(signal,self.settings.market.candidate_ttl_minutes)
        canonical={"schema":"spotguard.order.v1","proposal_id":proposal_id,"candidate_id":candidate["id"],"product":"SPOT","symbol":symbol,"side":"SELL","order_type":"OCO_PROTECTION","quote_asset":self.settings.risk.quote_asset,"quote_amount":"0","entry_reference":str(market.bid),"stop_reference":str(stop),"take_profit_reference":str(target),"reward_risk":"0","quantity":str(quantity),"market_step_size":str(step),"price_tick_size":str(tick),"mode":"live","source":"manual-live-set-protection","approval_owner_id":self.settings.openclaw.telegram_owner_id,"approval_chat_id":self.settings.telegram.chat_id,"approval_nonce":secrets.token_urlsafe(12),"created_at":isoformat(now),"expires_at":isoformat(now+timedelta(seconds=self.settings.live.approval_ttl_seconds))}
        values={"id":proposal_id,"candidate_id":candidate["id"],"symbol":symbol,"side":"BUY","product":"SPOT","order_type":"MARKET","quote_amount":"0","entry_reference":str(market.bid),"stop_reference":str(stop),"take_profit_reference":str(target),"reward_risk":"0","rationale":"Restore last approved TP/SL to current free Spot balance; approval required.","mode":"live","source":"manual-live-set-protection","created_at":canonical["created_at"],"expires_at":canonical["expires_at"],"canonical":canonical,"canonical_json":canonical_json(canonical),"locale":self.locale}
        token=self.signer.approval_token(values["canonical_json"]); proposal=self.ledger.create_proposal(values,self.signer.token_hash(token),self.settings.risk.max_active_proposals)
        result={"proposal":proposal,"notification":None}
        if notify: result["notification"]=self.notify_proposal(proposal["id"],dry_run=dry_run,token=token)
        return result

    @localized
    def create_live_cancel_protection_proposal(self, symbol: str, notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        raw_symbol = symbol.upper()
        symbol = raw_symbol if raw_symbol.endswith(self.settings.risk.quote_asset) else raw_symbol + self.settings.risk.quote_asset
        if symbol not in self.settings.live.allowed_symbols or not self.settings.live.enabled or not self.live_arm.status().armed:
            raise SecurityError("live cancel protection is disabled or not armed")
        if getattr(self.live_arm.status(), "scope", "FULL") != "EXIT_ONLY":
            readiness = self.live_status(check_symbols=True, symbols=[symbol])
            if not readiness["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")
        grouped: dict[int, list[dict[str, Any]]] = {}
        for order in self.live_executor.read_open_spot_orders():
            if order.get("symbol") == symbol and isinstance(order.get("orderListId"), int) and order["orderListId"] > 0:
                grouped.setdefault(order["orderListId"], []).append(order)
        if len(grouped) != 1:
            raise PolicyError("exactly one active protection order list is required before it can be cancelled")
        order_list_id, legs = next(iter(grouped.items()))
        try:
            validated = self.live_executor.validate_active_protective_oco(legs, symbol)
        except SecurityError as exc:
            raise PolicyError(str(exc)) from exc
        order_list_id = validated["order_list_id"]
        anchor = validated["stop_leg"]
        fingerprint = hashlib.sha256(f"manual-live-cancel-protection:{symbol}:{order_list_id}:{time.time_ns()}".encode()).hexdigest()
        signal = Signal(candidate_id=f"c-{fingerprint[:12]}", fingerprint=fingerprint, symbol=symbol, interval=self.settings.market.interval,
            side="BUY", score=0, price=0.0, candle_close_time=int(time.time() * 1000), reasons=("manual live OCO cancellation; approval required",),
            metrics={"manual_live_cancel_protection": True, "order_list_id": order_list_id})
        candidate, _ = self.ledger.create_candidate(signal, self.settings.market.candidate_ttl_minutes)
        now = utcnow(); proposal_id = f"p-{secrets.token_hex(6)}"
        canonical = {"schema": "spotguard.order.v1", "proposal_id": proposal_id, "candidate_id": candidate["id"], "product": "SPOT",
            "symbol": symbol, "side": "CANCEL", "order_type": "CANCEL_OCO", "quote_asset": self.settings.risk.quote_asset,
            "quote_amount": "0", "entry_reference": "0", "stop_reference": "0", "take_profit_reference": "0", "reward_risk": "0",
            "order_list_id": order_list_id, "cancel_order_id": anchor["orderId"], "protected_order_ids": sorted(row["orderId"] for row in legs if isinstance(row.get("orderId"), int)),
            "mode": "live", "source": "manual-live-cancel-protection", "approval_owner_id": self.settings.openclaw.telegram_owner_id,
            "approval_chat_id": self.settings.telegram.chat_id, "approval_nonce": secrets.token_urlsafe(12), "created_at": isoformat(now),
            "expires_at": isoformat(now + timedelta(seconds=self.settings.live.approval_ttl_seconds))}
        values = {"id": proposal_id, "candidate_id": candidate["id"], "symbol": symbol, "side": "BUY", "product": "SPOT", "order_type": "CANCEL_OCO",
            "quote_amount": "0", "entry_reference": "0", "stop_reference": "0", "take_profit_reference": "0", "reward_risk": "0",
            "rationale": "Cancel the exact active live XRP protection order list; approval required.", "mode": "live", "source": "manual-live-cancel-protection",
            "created_at": canonical["created_at"], "expires_at": canonical["expires_at"], "canonical": canonical, "canonical_json": canonical_json(canonical), "locale": self.locale}
        token = self.signer.approval_token(values["canonical_json"])
        proposal = self.ledger.create_proposal(values, self.signer.token_hash(token), self.settings.risk.max_active_proposals)
        self.ledger.add_event("manual.live_cancel_protection.proposal", proposal["id"], {"symbol": symbol, "order_list_id": order_list_id})
        result: dict[str, Any] = {"proposal": proposal, "notification": None}
        if notify:
            result["notification"] = self.notify_proposal(proposal["id"], dry_run=dry_run, token=token)
        return result

    @localized
    def create_live_close_all_proposal(self, symbol: str, notify: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Create one dormant, owner-confirmed close of the currently free Spot balance."""
        raw_symbol = symbol.upper()
        symbol = raw_symbol if raw_symbol.endswith(self.settings.risk.quote_asset) else raw_symbol + self.settings.risk.quote_asset
        if symbol not in self.settings.market.symbols or symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("symbol is not enabled for live Spot intent")
        if not self.settings.live.enabled or not self.live_arm.status().armed:
            raise SecurityError("live trading is disabled or not armed on the VPS")
        # Keep the legacy explicit close-all command safe too: if the asset is
        # OCO-locked, it must use the protected 100% exit, never mistake dust
        # as the whole position.
        if any(row.get("symbol") == symbol and isinstance(row.get("orderListId"), int) and row["orderListId"] > 0
               for row in self.live_executor.read_open_spot_orders()):
            return self.create_live_partial_exit_proposal(symbol, Decimal("100"), notify=notify, dry_run=dry_run)
        if getattr(self.live_arm.status(), "scope", "FULL") != "EXIT_ONLY":
            readiness = self.live_status(check_symbols=True, symbols=[symbol])
            if not readiness["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")
        exchange = validate_spot_symbol(self.settings, symbol)
        market = fetch_spot_snapshot(self.settings, symbol)
        quote_asset = self.settings.risk.quote_asset
        base_asset = symbol[:-len(quote_asset)]
        account = self.live_executor.read_spot_account()
        free = next((Decimal(row["free"]) for row in account["balances"] if row["asset"] == base_asset), Decimal("0"))
        step = Decimal(exchange["market_step_size"])
        quantity = floor_to_step(free, step)
        estimated_quote = quantity * market.bid
        if quantity <= 0 or estimated_quote < Decimal(exchange["min_notional"]):
            raise PolicyError("free live balance is below Binance minimum after LOT_SIZE rounding; no close proposal was created")
        klines = fetch_klines(self.settings, symbol)
        snapshot = analyze(klines)
        fingerprint = hashlib.sha256(f"manual-live-close:{symbol}:{time.time_ns()}:{secrets.token_hex(4)}".encode()).hexdigest()
        signal = Signal(candidate_id=f"c-{fingerprint[:12]}", fingerprint=fingerprint, symbol=symbol,
            interval=self.settings.market.interval, side="BUY", score=snapshot.score, price=float(market.bid),
            candle_close_time=klines[-1].close_time, reasons=("manual live close; free Spot balance only",),
            metrics={**snapshot.to_dict(), "manual_live_close": True, "base_asset": base_asset})
        candidate, _ = self.ledger.create_candidate(signal, self.settings.market.candidate_ttl_minutes)
        now = utcnow()
        proposal_id = f"p-{secrets.token_hex(6)}"
        canonical = {
            "schema": "spotguard.order.v1", "proposal_id": proposal_id, "candidate_id": candidate["id"],
            "product": "SPOT", "symbol": symbol, "side": "SELL", "order_type": "MARKET",
            "quote_asset": quote_asset, "quote_amount": "0", "estimated_quote_amount": str(estimated_quote),
            "entry_reference": str(market.bid), "stop_reference": "0", "take_profit_reference": "0", "reward_risk": "0",
            "quantity": str(quantity), "market_step_size": str(step), "base_asset": base_asset,
            "mode": "live", "source": "manual-live-close", "approval_owner_id": self.settings.openclaw.telegram_owner_id,
            "approval_chat_id": self.settings.telegram.chat_id, "approval_nonce": secrets.token_urlsafe(12),
            "created_at": isoformat(now), "expires_at": isoformat(now + timedelta(seconds=self.settings.live.approval_ttl_seconds)),
        }
        values = {"id": proposal_id, "candidate_id": candidate["id"], "symbol": symbol, "side": "BUY", "product": "SPOT",
            "order_type": "MARKET", "quote_amount": "0", "entry_reference": str(market.bid), "stop_reference": "0",
            "take_profit_reference": "0", "reward_risk": "0", "rationale": "Manual live close of rounded free Spot balance; approval required.",
            "mode": "live", "source": "manual-live-close", "created_at": canonical["created_at"], "expires_at": canonical["expires_at"],
            "canonical": canonical, "canonical_json": canonical_json(canonical), "locale": self.locale}
        token = self.signer.approval_token(values["canonical_json"])
        proposal = self.ledger.create_proposal(values, self.signer.token_hash(token), self.settings.risk.max_active_proposals)
        self.ledger.add_event("manual.live_close.proposal", proposal["id"], {"symbol": symbol, "quantity": str(quantity), "base_asset": base_asset})
        result: dict[str, Any] = {"proposal": proposal, "notification": None}
        if notify:
            result["notification"] = self.notify_proposal(proposal["id"], dry_run=dry_run, token=token)
        return result

    def create_demo_candidate(
        self,
        symbol: str,
        entry_reference: Decimal,
        notify: bool = False,
        dry_run: bool = False,
        verified_agent_os: bool = False,
    ) -> dict[str, Any]:
        if self.settings.mode != "paper":
            raise SecurityError("Agent OS seeded demo candidates are only allowed in paper mode")
        symbol = symbol.upper()
        if symbol not in self.settings.market.symbols:
            raise SpotGuardError(f"symbol is not allowlisted: {symbol}")
        if entry_reference <= 0:
            raise SpotGuardError("entry reference must be positive")
        klines = scaled_synthetic_klines(float(entry_reference))
        snapshot = analyze(klines)
        signal = evaluate(self.settings, symbol, snapshot)
        if signal is None:
            raise SpotGuardError("paper demo metrics did not pass the configured candidate policy")
        signal = replace(
            signal,
            reasons=signal.reasons
            + ("verified Agent OS PAPER demo" if verified_agent_os else "synthetic/manual PAPER demo; not Agent OS confirmed",),
            metrics={
                **signal.metrics,
                "paper_demo": True,
                "price_source": "binance-agent-os" if verified_agent_os else "synthetic-manual", "agent_os_confirmed": verified_agent_os,
            },
        )
        candidate, created = self.ledger.create_candidate(
            signal, self.settings.market.candidate_ttl_minutes
        )
        delivery = None
        if notify and created:
            delivery = self.notify_candidate(candidate["id"], dry_run=dry_run)
        return {"candidate": candidate, "created": created, "notification": delivery}

    def create_agent_os_demo_candidate(
        self,
        symbol: str,
        notify: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        market_review = self.agent_os.review_market(symbol)
        candidate_result = self.create_demo_candidate(
            symbol,
            Decimal(market_review["last_price"]),
            notify=False,
            verified_agent_os=True,
        )
        self._record_agent_os_read(
            candidate_result["candidate"]["id"], "demo-seed", market_review
        )
        if notify and candidate_result["created"]:
            candidate_result["notification"] = self.notify_candidate(
                candidate_result["candidate"]["id"], dry_run=dry_run
            )
        return {"market_review": market_review, **candidate_result}

    def review_candidate_with_agent_os(
        self,
        candidate_id: str,
        notify: bool = False,
        dry_run: bool = False,
        dispatch_source: str = "cli",
    ) -> dict[str, Any]:
        candidate = self.ledger.get_candidate(candidate_id)
        if candidate["status"] != "ACTIVE":
            raise SpotGuardError(f"candidate must be ACTIVE, not {candidate['status']}")
        metrics = candidate.get("metrics") or {}
        review_input = {
            "candidate_id": candidate["id"], "symbol": candidate["symbol"],
            "interval": candidate["interval"], "side": candidate["side"],
            "scanner_score": candidate["score"], "scanner_reasons": candidate["reasons"],
            "close_reference": str(candidate["price"]),
            "ema_fast": str(metrics.get("ema_fast", "")),
            "ema_slow": str(metrics.get("ema_slow", "")),
            "rsi": str(metrics.get("rsi_14", "")), "atr": str(metrics.get("atr_14", "")),
            "atr_pct": str(metrics.get("atr_pct", "")),
            "momentum": str(metrics.get("momentum_3_pct", "")),
            "volume_ratio": str(metrics.get("volume_ratio_20", "")),
            "breakout": bool(metrics.get("breakout_20", False)),
            "candidate_candle_close_time": candidate["candle_close_time"],
            "reference_candle": metrics.get("prefilter_candle"),
            "score_engine_version": metrics.get("score_engine_version", SCORE_ENGINE_VERSION),
            "score_provenance": "Binance closed-candle prefilter",
            "risk_policy_facts": {"mode": self.settings.scheduled_proposal_mode,
                                   "min_reward_risk": str(self.settings.risk.min_reward_risk),
                                   "max_stop_distance_pct": str(self.settings.risk.max_stop_distance_pct)},
        }
        review: dict[str, Any] | None = None
        retry_used = False
        try:
            for attempt in range(2):
                try:
                    review = self.agent_os.review_candidate(review_input)
                    break
                except Exception as exc:
                    if getattr(exc, "reason", None) == "empty_final" and attempt == 0:
                        retry_used = True
                        continue
                    raise
            if review is None:
                raise SpotGuardError("AI review returned no decision")
            actual = review["candle"]
            if actual.close_time != candidate["candle_close_time"]:
                raise CodexBridgeError("AI review fresh candle is stale or mismatched", "mismatched_candle")
            expected_close = Decimal(str(candidate["price"]))
            if Decimal(str(actual.close)) != expected_close:
                raise CodexBridgeError("AI review fresh candle close mismatches candidate", "mismatched_candle")
            reference = metrics.get("prefilter_candle")
            if isinstance(reference, dict):
                for field in ("open_time", "open", "high", "low", "close", "volume", "close_time"):
                    if field not in reference:
                        raise CodexBridgeError("AI review reference candle is incomplete", "mismatched_candle")
                if actual.open_time != int(reference["open_time"]) or actual.close_time != int(reference["close_time"]):
                    raise CodexBridgeError("AI review fresh candle timestamp mismatches scanner", "mismatched_candle")
                for field, actual_value in (("open", actual.open), ("high", actual.high),
                                            ("low", actual.low), ("close", actual.close),
                                            ("volume", actual.volume)):
                    if Decimal(str(actual_value)) != Decimal(str(reference[field])):
                        raise CodexBridgeError("AI review fresh candle OHLCV mismatches scanner", "mismatched_candle")
            if not review["fresh_data_verified"]:
                raise CodexBridgeError("AI review did not verify fresh data", "stale_market_data")
            self.ledger.add_event("agent_os.ai_review", candidate_id, {
                "symbol": candidate["symbol"], "decision": review["decision"],
                "fresh_data_verified": True, "retry_used": retry_used,
                "dispatch_source": dispatch_source, "reviewer_mode": "isolated",
                "failure_category": None, "token_usage": review.get("token_usage"),
                "input_tokens": (review.get("token_usage") or {}).get("input_tokens"),
                "cached_input_tokens": (review.get("token_usage") or {}).get("cached_input_tokens"),
                "input_prompt_tokens_estimate": (review.get("token_usage") or {}).get("prompt_tokens_estimate"),
                "output_tokens": (review.get("token_usage") or {}).get("output_tokens"),
                "output_tokens_estimate": (review.get("token_usage") or {}).get("output_tokens_estimate"),
                "total_tokens": (review.get("token_usage") or {}).get("total_tokens"),
                "agent_latency_ms": review.get("elapsed_ms"), "tool_latency_ms": review.get("elapsed_ms"),
            })
        except Exception as exc:
            category = getattr(exc, "reason", None) or (
                "timeout" if "timed out" in str(exc).lower() else
                "mismatched_candle" if "mismatch" in str(exc).lower() else "review_failure")
            self.ledger.add_event("agent_os.ai_review", candidate_id, {
                "symbol": candidate["symbol"], "decision": None,
                "fresh_data_verified": False, "retry_used": retry_used,
                "dispatch_source": dispatch_source, "reviewer_mode": "isolated",
                "failure_category": category, "token_usage": None, "agent_latency_ms": None,
                "input_prompt_tokens_estimate": None, "output_tokens": None,
                "total_tokens": None, "tool_latency_ms": None,
            })
            raise
        assert review is not None
        review_output = self._serialize_agent_os_review(review)
        if review["decision"] != "APPROVE":
            return {"market_review": review_output, "proposal": None,
                    "review_decision": review["decision"], "retry_used": retry_used}
        # A completed AI REVIEW is a read-only result.  LIVE proposal
        # eligibility is deliberately a later, optional stage: an exchange
        # capability change for this symbol must not discard the verified
        # review or make the deterministic Telegram command appear to fail.
        proposal_mode = self.settings.scheduled_proposal_mode
        if proposal_mode == "live":
            readiness = self.live_status(
                check_symbols=True, symbols=[candidate["symbol"]]
            )
            if not readiness["execution_ready"]:
                return self._skipped_live_proposal_result(review_output, retry_used, readiness)
        try:
            proposal = self.create_proposal(
                candidate_id,
                Decimal(str(actual.close)), Decimal(str(actual.close)),
                None,
                review["reason"],
                notify=notify,
                dry_run=dry_run,
            )
        except SecurityError as exc:
            # Readiness may change between the preflight and proposal build.
            # Only that known proposal-eligibility failure is non-fatal to a
            # successfully completed read-only review.
            if (proposal_mode == "live"
                    and str(exc) == "scheduled LIVE proposal mode is not execution-ready"):
                readiness = self.live_status(
                    check_symbols=True, symbols=[candidate["symbol"]]
                )
                return self._skipped_live_proposal_result(review_output, retry_used, readiness)
            raise
        return {"market_review": review_output, "review_decision": review["decision"],
                "retry_used": retry_used, **proposal}

    @staticmethod
    def _serialize_agent_os_review(review: dict[str, Any]) -> dict[str, Any]:
        """Expose only JSON primitives from the Agent OS review boundary."""
        def value(item: Any) -> Any:
            if isinstance(item, Kline):
                return item.to_dict()
            if isinstance(item, list):
                return [value(child) for child in item]
            if isinstance(item, dict):
                return {key: value(child) for key, child in item.items()}
            return item
        return value(review)

    @staticmethod
    def _skipped_live_proposal_result(
        review: dict[str, Any], retry_used: bool, readiness: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a successful review without weakening the LIVE gate."""
        blockers = list(dict.fromkeys(
            [str(item) for item in readiness.get("blockers", [])]
            + [str(item) for item in readiness.get("readiness_reasons", [])]
        ))
        return {
            "market_review": review,
            "proposal": None,
            "proposal_mode": "live",
            "proposal_status": "SKIPPED_NOT_EXECUTION_READY",
            "proposal_blockers": blockers or ["live_execution_not_ready"],
            "review_decision": review["decision"],
            "retry_used": retry_used,
        }

    def _record_agent_os_read(
        self, entity_id: str, stage: str, market_review: dict[str, Any]
    ) -> None:
        self.ledger.add_event(
            "agent_os.market_read",
            entity_id,
            {
                "stage": stage,
                "symbol": market_review["symbol"],
                "best_bid": market_review["best_bid"],
                "best_ask": market_review["best_ask"],
                "last_price": market_review["last_price"],
                "observed_at": market_review["observed_at"],
                "mcp_server": market_review["mcp_server"],
                "mcp_tool_calls": market_review["mcp_tool_calls"],
                "execution_mode": "paper",
            },
        )

    @localized
    def create_proposal(
        self,
        candidate_id: str,
        bid_reference: Decimal,
        ask_reference: Decimal,
        quote_amount: Decimal | None,
        rationale: str,
        notify: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        proposal_mode = self.settings.scheduled_proposal_mode
        candidate_preview = self.ledger.get_candidate(candidate_id)
        paper_context: PolicyContext | None = None
        paper_symbols: set[str] = set()
        requested_amount = quote_amount
        initial_amount = (
            requested_amount
            if requested_amount is not None
            else (
                None
                if self.settings.sizing_policy.percentage_based
                else self.settings.risk.default_quote_amount
            )
        )
        if proposal_mode == "paper":
            paper_context, paper_symbols = self._ensure_paper_entry_available(
                candidate_preview["symbol"],
                initial_amount,
            )
        elif not self.live_status()["execution_ready"]:
            raise SecurityError("scheduled LIVE proposal mode is not execution-ready")
        if self.ledger.active_proposal_count() >= self._active_proposal_limit(
            proposal_mode
        ):
            raise SpotGuardError("maximum active proposal count has been reached")
        candidate = self.ledger.get_candidate(candidate_id)
        if not candidate["metrics"].get("agent_os_confirmed") and not candidate["metrics"].get("paper_demo"):
            raise SpotGuardError("candidate lacks successful Agent OS confirmation")
        sizing: SizingDecision | None = None
        amount = initial_amount or self.settings.risk.min_quote_amount
        if proposal_mode == "paper" and self.settings.sizing_policy.percentage_based:
            assert paper_context is not None
            preliminary = entry_policy_terms(
                self.settings,
                candidate_price=candidate["price"],
                atr_value=candidate["metrics"]["atr_14"],
                bid_reference=bid_reference,
                ask_reference=ask_reference,
                quote_amount=amount,
                mode="paper",
            )
            existing_exposure, existing_risk = self._paper_symbol_usage(
                candidate["symbol"]
            )
            sizing = size_entry(
                paper_context,
                entry_price=preliminary["entry_reference"],
                stop_price=preliminary["stop_reference"],
                existing_position_exposure=existing_exposure,
                existing_position_risk=existing_risk,
                requested_notional=requested_amount,
                fee_buffer_rate=self.settings.risk.paper_fee_pct / Decimal("100"),
            )
            self._raise_sizing_rejection(sizing, phase="proposal")
            amount = sizing.calculated_notional
        reference_quantity_override: Decimal | None = None
        proposal_exchange_minimum: Decimal | None = None
        proposal_exchange_step: Decimal | None = None
        if proposal_mode == "paper" and self.settings.sizing_policy.percentage_based:
            if candidate["metrics"].get("paper_demo"):
                proposal_exchange_minimum = self.settings.risk.min_quote_amount
                proposal_exchange_step = Decimal("0.00000001")
            else:
                proposal_market = fetch_spot_snapshot(
                    self.settings, candidate["symbol"]
                )
                proposal_exchange_minimum = proposal_market.min_notional
                proposal_exchange_step = proposal_market.step_size
            reference_quantity_override = floor_to_step(
                amount / ask_reference, proposal_exchange_step
            )
            if (reference_quantity_override <= 0
                    or reference_quantity_override * ask_reference
                    < proposal_exchange_minimum):
                raise PolicyError(
                    "MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE: Binance minimum notional exceeds the safe risk-derived size after downward LOT_SIZE rounding; amount was not increased"
                )
        proposal_values = build_proposal(
            self.settings,
            candidate,
            bid_reference,
            ask_reference,
            amount,
            rationale,
            proposal_mode=proposal_mode,
            source="deterministic-signal",
        )
        if proposal_mode == "paper":
            assert paper_context is not None
            reference_quantity = (
                reference_quantity_override
                if reference_quantity_override is not None
                else amount / ask_reference
            )
            reference_plan = build_fill_risk(
                self.settings, proposal_values, ask_reference, reference_quantity
            )
            projection = self._paper_risk_projection(
                candidate["symbol"], ask_reference, reference_plan,
                bid=bid_reference, ask=ask_reference,
                reward_risk=Decimal(proposal_values["reward_risk"]),
                effective_limits=paper_context.effective_limits,
            )
            proposal_values["canonical"].update({
                "projected_risk_at_stop": str(projection["new_position_risk"]),
                "projected_aggregate_risk": str(projection["projected_aggregate_risk"]),
                "reference_quantity": reference_plan["net_base_quantity"],
                "gross_reference_quantity": reference_plan["gross_base_quantity"],
                "exchange_min_notional": (
                    str(proposal_exchange_minimum)
                    if proposal_exchange_minimum is not None else None
                ),
                "quantity_step": (
                    str(proposal_exchange_step)
                    if proposal_exchange_step is not None else None
                ),
            })
            evaluation = self._evaluate_paper_entry(
                paper_context, paper_symbols, candidate["symbol"], amount,
                projected_position_risk=Decimal(str(projection["new_position_risk"])),
                projected_aggregate_risk=Decimal(str(projection["projected_aggregate_risk"])),
                projected_position_exposure=(
                    self._paper_symbol_usage(candidate["symbol"])[0]
                    + Decimal(str(reference_plan["quote_spent"]))
                ),
                estimated_fee=(
                    sizing.estimated_fee_buffer if sizing is not None else Decimal("0")
                ),
            )
            self._attach_policy_snapshot(
                proposal_values,
                paper_context,
                evaluation,
                sizing,
                Decimal(str(reference_plan["gross_base_quantity"])),
            )
        else:
            # Scheduled/candidate LIVE proposals receive the same fresh public
            # quote, exchange rounding, account snapshot, and OTOCO invariant
            # as a manually requested LIVE entry.
            market = fetch_spot_snapshot(self.settings, candidate["symbol"])
            readiness = self.live_status(check_symbols=True, symbols=[candidate["symbol"]])
            if not readiness["execution_ready"]:
                raise SecurityError("scheduled LIVE proposal mode is not execution-ready")
            self._prepare_live_entry_proposal(
                proposal_values, amount,
                market, readiness["execution_ready"],
                auto_size=(
                    requested_amount is None
                    and self.settings.sizing_policy.percentage_based
                ),
            )
        token = self.signer.approval_token(proposal_values["canonical_json"])
        code = self.signer.paper_confirmation_code(proposal_values["canonical_json"]) if proposal_values["mode"] == "paper" else None
        proposal_values["locale"] = self.locale
        proposal = self.ledger.create_proposal(
            proposal_values,
            self.signer.token_hash(token),
            self._active_proposal_limit(proposal_mode),
            self.signer.token_hash(code) if code else None,
        )
        result: dict[str, Any] = {"proposal": proposal, "notification": None}
        if notify:
            result["notification"] = self.notify_proposal(
                proposal["id"], dry_run=dry_run, token=token, confirmation_code=code
            )
        return result

    @localized
    def resend_paper_proposal(self, proposal_id: str, sender_id: str, chat_id: str,
                              dry_run: bool = False) -> dict[str, Any]:
        self._validate_owner(sender_id)
        if chat_id != self.settings.telegram.chat_id:
            raise SecurityError("paper resend chat does not match")
        proposal = self.ledger.get_proposal(validate_simple_id(proposal_id, "proposal_id"))
        if proposal["mode"] != "paper" or proposal["status"] != "PENDING":
            raise PolicyError("only a PENDING PAPER proposal can be resent")
        try:
            notification = self.notify_proposal(proposal_id, dry_run=dry_run)
        except TelegramError as exc:
            self.ledger.add_event("proposal.notification_failed", proposal_id, {"reason_type": type(exc).__name__, "resend": True})
            return {"proposal": proposal, "notification": {"delivered": False, "transport": "failed",
                "controls_present": False, "error": bounded_text(str(exc), 300)},
                "message": "PAPER proposal still exists; Telegram notification resend failed."}
        controls = notification.get("payload", {}).get("presentation", {}).get("blocks", [{}])[0].get("buttons", [])
        notification["controls_present"] = len(controls) >= 2
        return {"proposal": proposal, "notification": notification,
            "message": "Approval controls delivered." if notification.get("delivered") and len(controls) >= 2 else "PAPER proposal exists; notification was not delivered."}

    @localized
    def notify_proposal(self, proposal_id: str, dry_run: bool = False,
                        token: str | None = None,
                        confirmation_code: str | None = None) -> dict[str, Any]:
        proposal = self.ledger.get_proposal(proposal_id, include_private=True)
        if proposal["status"] != "PENDING":
            raise SpotGuardError(f"only PENDING proposals can be notified, not {proposal['status']}")
        # The creation path passes the credentials generated before insertion.
        # Explicit re-notification may deterministically recover them, but it may
        # never replace or disagree with the immutable hashes in the ledger.
        token = token or self.signer.approval_token(proposal["canonical_json"])
        if not secrets.compare_digest(self.signer.token_hash(token), proposal["approval_token_hash"]):
            raise SecurityError("stored proposal approval credential is inconsistent")
        code = confirmation_code
        if proposal["mode"] == "paper":
            code = code or self.signer.paper_confirmation_code(proposal["canonical_json"])
            if not isinstance(code, str) or not secrets.compare_digest(
                    self.signer.token_hash(code), proposal.get("confirmation_code_hash") or ""):
                raise SecurityError("stored paper confirmation credential is inconsistent")
        elif code is not None:
            raise SecurityError("text confirmation credential is paper-only")
        message, buttons = proposal_message(proposal, token, code, locale=self.locale)
        delivery = self.messenger.send(message, buttons, dry_run=dry_run)
        if delivery.delivered:
            self.ledger.mark_proposal_notified(proposal_id)
        return delivery.to_dict()

    def _enforce_paper_daily_entry_quota(self, proposal: dict[str, Any]) -> None:
        if proposal["mode"] != "paper":
            return
        day = utcnow().date().isoformat()
        if self.ledger.successful_paper_entries(day) >= self.settings.paper.max_successful_entries_per_utc_day:
            if proposal["status"] in {"PENDING", "EXECUTING"}:
                self.ledger.terminalize_paper_proposal(proposal["id"], f"daily PAPER entry quota reached ({self.settings.paper.max_successful_entries_per_utc_day})")
            raise PolicyError(f"daily PAPER entry quota reached ({self.settings.paper.max_successful_entries_per_utc_day}); proposal terminalized")

    def _validate_paper_confirmation(self, proposal_id: str, code: str, sender_id: str, chat_id: str) -> tuple[dict[str, Any], str]:
        self.ledger.expire_stale_active_proposals()
        actor = self._validate_owner(sender_id)
        if chat_id != self.settings.telegram.chat_id:
            raise SecurityError("paper confirmation chat does not match")
        proposal = self.ledger.get_proposal(proposal_id, include_private=True)
        if proposal["status"] != "PENDING" or parse_time(proposal["expires_at"]) <= utcnow():
            raise SecurityError(
                "PROPOSAL_EXPIRED: APPROVAL_EXPIRED: paper proposal is not pending or has expired"
            )
        if proposal["canonical"].get("approval_owner_id") != sender_id or proposal["canonical"].get("approval_chat_id") != chat_id:
            raise SecurityError("paper confirmation does not match proposal ownership")
        if proposal["mode"] != "paper" or proposal["canonical"].get("mode") != "paper":
            raise SecurityError("text confirmation is paper-only")
        stored = proposal.get("confirmation_code_hash")
        if not isinstance(stored, str) or not secrets.compare_digest(self.signer.token_hash(code), stored):
            raise SecurityError("paper confirmation code is invalid")
        expected = self.signer.paper_confirmation_code(proposal["canonical_json"])
        if not secrets.compare_digest(expected, code):
            raise SecurityError("paper confirmation does not match the canonical proposal")
        return proposal, actor

    @localized
    def paper_text_approve(self, proposal_id: str, code: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        try:
            return self._paper_text_approve(proposal_id, code, sender_id, chat_id)
        except Exception as exc:
            self.ledger.add_event("approval.rejected", proposal_id, {
                "method": "paper_confirmation_code", "reason_type": type(exc).__name__
            })
            raise

    def _paper_text_approve(self, proposal_id: str, code: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        proposal, _ = self._validate_paper_confirmation(proposal_id, code, sender_id, chat_id)
        self._enforce_paper_daily_entry_quota(proposal)
        daily = self.ledger.daily_committed_quote(utcnow().date().isoformat())
        try:
            validate_claim(self.settings, proposal, daily)
            self._revalidate_paper_entry_policy(proposal, phase="approval")
        except Exception as exc:
            if proposal["status"] == "PENDING":
                self._policy_reject(proposal, exc)
            raise
        lease, lease_hash = self.signer.new_lease()
        lease_expires = isoformat(utcnow() + timedelta(seconds=self.settings.risk.execution_lease_seconds))
        self.ledger.claim_proposal_by_confirmation_code(
            proposal_id, self.signer.token_hash(code), f"telegram:{sender_id}",
            lease_hash, lease_expires, utcnow().date().isoformat(),
            str(self.settings.risk.max_daily_quote),
        )
        fill = self.execute_paper(proposal_id, lease)
        return {"ok": True, "message": "APPROVE PAPER accepted; simulated fill completed", "proposal": fill}

    @localized
    def paper_text_reject(self, proposal_id: str, code: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        proposal, actor = self._validate_paper_confirmation(proposal_id, code, sender_id, chat_id)
        token = self.signer.approval_token(proposal["canonical_json"])
        rejected = self.ledger.reject_proposal(proposal_id, self.signer.token_hash(token), actor)
        return {"ok": True, "message": "Paper proposal rejected", "proposal": rejected}

    def _validate_owner(self, sender_id: str) -> str:
        if sender_id != self.settings.openclaw.telegram_owner_id:
            raise SecurityError("approval sender is not the configured Telegram owner")
        return f"telegram:{sender_id}"

    @localized
    def reject(self, proposal_id: str, token: str, sender_id: str, chat_id: str | None = None) -> dict[str, Any]:
        self.ledger.expire_stale_active_proposals()
        actor = self._validate_owner(sender_id)
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal["status"] == "EXPIRED":
            raise SecurityError(
                "PROPOSAL_EXPIRED: APPROVAL_EXPIRED: proposal approval window expired"
            )
        effective_chat = chat_id or self.settings.telegram.chat_id
        if effective_chat != self.settings.telegram.chat_id or proposal["canonical"].get("approval_chat_id") != effective_chat or proposal["canonical"].get("approval_owner_id") != sender_id:
            raise SecurityError("rejection ownership or chat binding does not match")
        if not self.signer.verify_approval(proposal["canonical_json"], token):
            raise SecurityError("APPROVAL_INVALID: approval token does not match the proposal")
        return self.ledger.reject_proposal(proposal_id, self.signer.token_hash(token), actor)

    @localized
    def claim(self, proposal_id: str, token: str, sender_id: str, chat_id: str | None = None) -> dict[str, Any]:
        self.ledger.expire_stale_active_proposals()
        actor = self._validate_owner(sender_id)
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal["status"] == "EXPIRED":
            raise SecurityError(
                "PROPOSAL_EXPIRED: APPROVAL_EXPIRED: proposal approval window expired"
            )
        effective_chat = chat_id or self.settings.telegram.chat_id
        if effective_chat != self.settings.telegram.chat_id or proposal["canonical"].get("approval_chat_id") != effective_chat or proposal["canonical"].get("approval_owner_id") != sender_id:
            raise SecurityError("approval ownership or chat binding does not match")
        if not self.signer.verify_approval(proposal["canonical_json"], token):
            raise SecurityError("APPROVAL_INVALID: approval token does not match the proposal")
        self._enforce_paper_daily_entry_quota(proposal)
        daily = self.ledger.daily_committed_quote(utcnow().date().isoformat())
        try:
            validate_claim(self.settings, proposal, daily)
            if proposal["mode"] == "paper":
                self._revalidate_paper_entry_policy(proposal, phase="approval")
            else:
                arm = self.live_arm.status()
                if getattr(arm, "scope", "FULL") == "EXIT_ONLY":
                    self._validate_exit_only_approval(proposal, arm)
                else:
                    # Re-check the exact pair at the normal LIVE entry gate.
                    readiness = self.live_status(
                        check_symbols=True, symbols=[proposal["symbol"]]
                    )
                    if not readiness["execution_ready"]:
                        raise SecurityError(
                            "live approval fails closed; readiness blockers: "
                            + ", ".join(readiness["blockers"])
                        )
                    self._revalidate_live_buy_entry(proposal)
        except Exception as exc:
            if proposal["status"] == "PENDING":
                self._policy_reject(proposal, exc)
            raise
        lease, lease_hash = self.signer.new_lease()
        lease_expires = isoformat(
            utcnow() + timedelta(seconds=self.settings.risk.execution_lease_seconds)
        )
        claimed = self.ledger.claim_proposal(
            proposal_id,
            self.signer.token_hash(token),
            actor,
            lease_hash,
            lease_expires,
            utcnow().date().isoformat(),
            (
                str(absolute_daily_quote_ceiling(self.settings))
                if absolute_daily_quote_ceiling(self.settings) is not None
                else None
            ),
        )
        return {
            "proposal_id": proposal_id,
            "mode": claimed["mode"],
            "lease": lease,
            "lease_expires_at": lease_expires,
            "execution_intent": execution_intent(self.settings, claimed),
        }

    def _verify_execution_lease(self, proposal_id: str, lease: str) -> tuple[dict[str, Any], str]:
        proposal = self.ledger.get_proposal(proposal_id, include_private=True)
        stored_hash = proposal.get("execution_lease_hash")
        if proposal["status"] != "EXECUTING" or not isinstance(stored_hash, str):
            raise SecurityError("proposal does not have an active execution lease")
        expiry = proposal.get("execution_lease_expires_at")
        if not isinstance(expiry, str) or parse_time(expiry) <= utcnow():
            raise SecurityError("execution lease has expired; do not retry automatically")
        if not self.signer.verify_lease(lease, stored_hash):
            raise SecurityError("execution lease is invalid")
        return proposal, stored_hash

    @localized
    def execute_paper(self, proposal_id: str, lease: str) -> dict[str, Any]:
        if self.settings.mode != "paper":
            raise SecurityError("paper executor is unavailable while mode is live")
        proposal, lease_hash = self._verify_execution_lease(proposal_id, lease)
        try:
            return self._execute_claimed_paper(proposal, lease_hash)
        except Exception as exc:
            current = self.ledger.get_proposal(proposal_id, include_private=True)
            if current["status"] == "EXECUTING":
                reason = bounded_text(str(exc), 500) or type(exc).__name__
                if self.settings.sizing_policy.enabled:
                    self.ledger.reject_proposal_by_policy(proposal_id, reason)
                else:
                    self.ledger.fail_execution(proposal_id, lease_hash, reason)
            raise

    @localized
    def execute_live(self, proposal_id: str, lease: str) -> dict[str, Any]:
        proposal, lease_hash = self._verify_execution_lease(proposal_id, lease)
        if not self.settings.live.enabled:
            self.ledger.fail_execution(proposal_id, lease_hash, "live execution is disabled locally")
            raise SecurityError("LIVE_NOT_ENABLED: live executor is disabled locally")
        arm = self.live_arm.status()
        if not arm.armed:
            self.ledger.fail_execution(proposal_id, lease_hash, "live arm expired before executor invocation")
            raise SecurityError("LIVE_NOT_ARMED: live trading is not armed on the VPS")
        if proposal["mode"] != "live":
            raise SecurityError("live executor cannot execute a paper proposal")
        if getattr(arm, "scope", "FULL") == "EXIT_ONLY":
            self._validate_exit_only_approval(proposal, arm)
        try:
            self._revalidate_live_buy_entry(proposal)
        except Exception as exc:
            # No write has started. A stale balance/equity/risk snapshot is a
            # normal policy rejection, never an ambiguous execution outcome.
            self._policy_reject(proposal, exc)
        try:
            if proposal["canonical"].get("source") == "manual-live-cancel-protection":
                list_id = proposal["canonical"]["order_list_id"]
                current = self.live_executor.read_open_spot_orders()
                if not any(row.get("symbol") == proposal["symbol"] and row.get("orderListId") == list_id and row.get("orderId") == proposal["canonical"]["cancel_order_id"] for row in current):
                    raise SecurityError("the exact OCO protection list is no longer active; cancellation was not submitted")
            if proposal["canonical"].get("source") == "manual-live-close":
                base_asset = str(proposal["canonical"].get("base_asset", ""))
                quantity = Decimal(proposal["canonical"]["quantity"])
                step = Decimal(proposal["canonical"]["market_step_size"])
                account = self.live_executor.read_spot_account()
                free = next((Decimal(row["free"]) for row in account["balances"] if row["asset"] == base_asset), Decimal("0"))
                if quantity > floor_to_step(free, step):
                    raise SecurityError("free Spot balance changed; live close was not submitted")
            response = self.live_executor.execute_partial_exit(proposal) if proposal["canonical"].get("source") == "manual-live-partial-exit" else self.live_executor.execute(proposal)
        except Exception as exc:
            # After a write child has started, absence of a usable response is
            # never proof that Binance rejected it. Preserve the lease outcome
            # as RECONCILE and do not retry automatically.
            return self.uncertain_execution(proposal_id, lease, str(exc))
        source = proposal["canonical"].get("source")
        is_close = source == "manual-live-close"
        is_cancel = source == "manual-live-cancel-protection"
        is_partial = source == "manual-live-partial-exit"
        is_restore = source == "manual-live-set-protection"
        if is_cancel:
            remaining = self.live_executor.read_open_spot_orders()
            if any(row.get("symbol") == proposal["symbol"] and row.get("orderListId") == proposal["canonical"]["order_list_id"] for row in remaining):
                return self.uncertain_execution(proposal_id, lease, "OCO cancellation response requires reconciliation")
        order_id = response.get("orderId") if (is_close or is_cancel or is_partial) else response.get("orderListId")
        if not isinstance(order_id, int):
            return self.uncertain_execution(proposal_id, lease, "protected Spot response has no order identifier")
        status = str(response.get("status" if (is_close or is_partial) else "listStatusType", ""))
        fill = None
        evidence = None
        if not is_cancel and not is_restore:
            evidence = self._live_execution_evidence(proposal, response, order_id)
            if evidence is not None:
                try:
                    self._persist_live_execution_evidence(evidence)
                except Exception:
                    # The exchange write completed, but its local evidence
                    # could not be durably recorded.  Never retry the write.
                    result = self.ledger.finish_execution(
                        proposal_id, lease_hash, "RECONCILE", str(order_id), status,
                        {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"],
                         "accounting_status": "RECONCILE",
                         "accounting_reason": "LIVE_EXECUTION_EVIDENCE_PERSISTENCE_FAILED"},
                    )
                    result["accounting_status"] = "RECONCILE"
                    result["accounting_reason"] = "LIVE_EXECUTION_EVIDENCE_PERSISTENCE_FAILED"
                    return result
                if isinstance(evidence.get("entry"), Mapping) and isinstance(evidence["entry"].get("order_id"), int):
                    # Protected responses identify the list and the actual
                    # entry order separately.  Keep execution provenance on
                    # the entry order, never on the order-list id.
                    order_id = evidence["entry"]["order_id"]
            fill = self._verified_live_fill(proposal, response, order_id)
            if fill is None:
                entry_status = str((evidence or {}).get("entry", {}).get("status", "")).upper()
                if (not is_close and not is_partial and not is_cancel and not is_restore
                        and entry_status in {"NEW", "PENDING_NEW", "PARTIALLY_FILLED"}):
                    result = self.ledger.record_execution_submitted(
                        proposal_id, lease_hash, str(order_id), "EXEC_STARTED",
                        {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"],
                         "accounting_status": "WAITING_FOR_FILL", "execution_evidence": evidence},
                    )
                    result["accounting_status"] = "WAITING_FOR_FILL"
                    result["fill"] = "WAITING"
                    return result
                # The exchange write and protection can be successful while
                # the response lacks enough fill evidence for session PnL.
                # Keep the execution outcome, but make the accounting state
                # explicit and fail closed for subsequent entries.
                result = self.ledger.finish_execution(
                    proposal_id, lease_hash, "EXECUTED", str(order_id), status,
                    {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"],
                     "accounting_status": "RECONCILE", "accounting_reason": "LIVE_FILL_PROVENANCE_INCOMPLETE",
                     "protected_order_type": "PARTIAL_EXIT_REARM" if is_partial else ("MARKET_CLOSE" if is_close else "OTOCO"),
                     "binance_response": dict(response)},
                )
                result["accounting_status"] = "RECONCILE"
                result["accounting_reason"] = "LIVE_FILL_PROVENANCE_INCOMPLETE"
                return result
        result = self.ledger.finish_execution(
            proposal_id, lease_hash, "EXECUTED", str(order_id), status,
            {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"],
             "protected_order_type": "PARTIAL_EXIT_REARM" if is_partial else ("OCO_RESTORE" if is_restore else ("OCO_CANCEL" if is_cancel else ("MARKET_CLOSE" if is_close else "OTOCO"))), "binance_response": dict(response)},
        )
        if fill is not None:
            try:
                self._persist_live_risk_fill(fill)
            except Exception as exc:
                # The Binance write has already completed; never retry it.
                try:
                    self.ledger.add_event("live.risk_accounting", proposal_id, {
                        "status": "RECONCILE", "reason": "LIVE_RISK_FILL_PERSISTENCE_FAILED",
                    })
                except Exception:
                    pass
                result["accounting_status"] = "RECONCILE"
                result["accounting_reason"] = "LIVE_RISK_FILL_PERSISTENCE_FAILED"
                return result
            accounting_ok, _, _, accounting_reason = self._live_session_accounting()
            result["accounting_status"] = "VERIFIED" if accounting_ok else "RECONCILE"
            if not accounting_ok:
                result["accounting_reason"] = accounting_reason or "RISKPILOT_SESSION_PNL_INCOMPLETE"
                self._mark_live_epoch_reconcile(result["accounting_reason"])
            if is_close and str(status).upper() == "FILLED":
                epoch = self._effective_live_risk_epoch()
                if isinstance(epoch, Mapping):
                    self.ledger.add_event("live.session_flat", proposal_id, {
                        "epoch_id": epoch.get("epoch_id"), "symbol": proposal["symbol"],
                        "verified": True, "protection_closed": True,
                    })
        return result

    def _live_execution_evidence(self, proposal: Mapping[str, Any], response: Mapping[str, Any],
                                 list_or_order_id: int) -> dict[str, Any] | None:
        """Keep a bounded, exchange-derived execution record before reduction."""
        fill_response = response
        partial = response.get("partial_exit")
        if isinstance(partial, Mapping) and isinstance(partial.get("sell"), Mapping):
            fill_response = partial["sell"]
        reports = fill_response.get("orderReports")
        candidates = reports if isinstance(reports, list) else [fill_response]
        entry = next((row for row in candidates if isinstance(row, Mapping)
                      and row.get("side") == proposal.get("side")
                      and str(row.get("status", "")).upper() in {"FILLED", "PARTIALLY_FILLED"}), None)
        if entry is None:
            # Preserve the actual entry/order-list response even when it did
            # not contain a verified fill.  Accounting extraction below still
            # requires a filled status and exchange-derived quantity/price.
            entry = next((row for row in candidates if isinstance(row, Mapping)
                          and row.get("side") == proposal.get("side")), None)
        if not isinstance(entry, Mapping):
            return None
        order_id = entry.get("orderId")
        if not isinstance(order_id, int) or order_id <= 0:
            order_id = None
        entry_evidence: dict[str, Any] = {
            "order_id": order_id,
            "order_list_id": response.get("orderListId"),
            "status": entry.get("status"),
        }
        for output, names in {
            "executed_qty": ("executedQty",),
            "cumulative_quote_qty": ("cummulativeQuoteQty", "cumulativeQuoteQty"),
            "limit_price": ("price",),
            "average_fill_price": ("avgPrice", "averagePrice"),
        }.items():
            value = next((entry[name] for name in names if name in entry), None)
            if value is not None:
                try:
                    parsed = Decimal(str(value))
                    if parsed.is_finite() and parsed >= 0:
                        entry_evidence[output] = format(parsed, "f")
                except (TypeError, ValueError, ArithmeticError):
                    pass
        fills_evidence = []
        if isinstance(entry.get("fills"), list):
            for item in entry["fills"]:
                if not isinstance(item, Mapping):
                    continue
                safe: dict[str, Any] = {}
                for output, source_key in (("price", "price"), ("qty", "qty"),
                                           ("commission", "commission"), ("commission_asset", "commissionAsset")):
                    if item.get(source_key) is not None:
                        value = item[source_key]
                        if output == "commission_asset":
                            if isinstance(value, str):
                                safe[output] = value
                        else:
                            try:
                                parsed = Decimal(str(value))
                                if parsed.is_finite() and parsed >= 0:
                                    safe[output] = format(parsed, "f")
                            except (TypeError, ValueError, ArithmeticError):
                                pass
                if safe:
                    fills_evidence.append(safe)
        if fills_evidence:
            entry_evidence["fills"] = fills_evidence
        if entry.get("commission") is not None:
            try:
                commission = Decimal(str(entry["commission"]))
                if commission.is_finite() and commission >= 0:
                    entry_evidence["commission"] = format(commission, "f")
            except (TypeError, ValueError, ArithmeticError):
                pass
        if isinstance(entry.get("commissionAsset"), str):
            entry_evidence["commission_asset"] = entry["commissionAsset"]
        protection = []
        for item in candidates:
            if not isinstance(item, Mapping) or item is entry:
                continue
            if item.get("side") == "SELL":
                row = {key: item[key] for key in ("orderId", "clientOrderId", "status", "type")
                       if key in item and (key != "orderId" or isinstance(item[key], int))}
                if row:
                    protection.append(row)
        phase = "FILLED" if str(entry_evidence.get("status", "")).upper() in {"FILLED", "PARTIALLY_FILLED"} else "SUBMITTED"
        return {"epoch_id": (self._effective_live_risk_epoch() or {}).get("epoch_id"),
                "proposal_id": proposal["id"], "symbol": proposal["symbol"],
                "side": proposal["side"], "entry": entry_evidence,
                "protection": protection, "executed_at": isoformat(),
                "source": "riskpilot_live_execution", "phase": phase}

    def _verified_live_fill(self, proposal: Mapping[str, Any], response: Mapping[str, Any],
                            list_or_order_id: int) -> dict[str, Any] | None:
        """Extract only exchange-reported fills; never promote request estimates."""
        evidence = self._live_execution_evidence(proposal, response, list_or_order_id)
        if evidence is None or not isinstance(evidence.get("entry"), Mapping):
            return None
        entry = evidence["entry"]
        if str(entry.get("status", "")).upper() not in {"FILLED", "PARTIALLY_FILLED"}:
            return None
        if not isinstance(entry.get("order_id"), int) or entry["order_id"] <= 0:
            return None
        try:
            quantity = Decimal(str(entry["executed_qty"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        fills = entry.get("fills")
        weighted = Decimal("0")
        filled_qty = Decimal("0")
        priced_fills = isinstance(fills, list) and fills and all(
            isinstance(item, Mapping) and item.get("price") is not None and item.get("qty") is not None
            for item in fills
        )
        if priced_fills:
            try:
                for item in fills:
                    fill_price = Decimal(str(item["price"])); fill_qty = Decimal(str(item["qty"]))
                    if fill_price <= 0 or fill_qty <= 0:
                        return None
                    weighted += fill_price * fill_qty
                    filled_qty += fill_qty
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return None
        if filled_qty > 0:
            price = weighted / filled_qty
        elif entry.get("average_fill_price") is not None:
            try:
                price = Decimal(str(entry["average_fill_price"]))
            except (TypeError, ValueError, ArithmeticError):
                return None
        else:
            try:
                cumulative = Decimal(str(entry["cumulative_quote_qty"]))
                price = cumulative / quantity
            except (KeyError, TypeError, ValueError, ArithmeticError, ZeroDivisionError):
                return None
        if quantity <= 0 or price <= 0 or not quantity.is_finite() or not price.is_finite():
            return None
        if proposal.get("side") == "BUY":
            canonical = proposal.get("canonical", {})
            cap_raw = canonical.get(
                "entry_slippage_cap_price", canonical.get("entry_limit_price")
            ) if isinstance(canonical, Mapping) else None
            # Legacy reconciliation evidence predating the marketable-LIMIT patch
            # has no immutable cap. Keep it reconcilable; every newly created LIVE
            # BUY proposal carries the cap and is therefore enforced here.
            if cap_raw is not None:
                try:
                    approved_cap = Decimal(str(cap_raw))
                except (TypeError, ValueError, ArithmeticError):
                    return None
                if approved_cap <= 0 or not approved_cap.is_finite() or price > approved_cap:
                    return None
                if priced_fills:
                    try:
                        if any(Decimal(str(item["price"])) > approved_cap for item in fills):
                            return None
                    except (TypeError, ValueError, ArithmeticError):
                        return None
        fees = fills if isinstance(fills, list) and fills else []
        if not fees and entry.get("commission") is not None:
            fees = [{"commission": entry.get("commission"), "commission_asset": entry.get("commissionAsset")}]
        parsed_fees = []
        for fee in fees:
            if not isinstance(fee, Mapping) or fee.get("commission") is None or not isinstance(fee.get("commission_asset", fee.get("commissionAsset")), str):
                return None
            try:
                amount = Decimal(str(fee["commission"]))
            except (TypeError, ValueError, ArithmeticError):
                return None
            if amount < 0 or not amount.is_finite():
                return None
            parsed_fees.append((amount, fee.get("commission_asset", fee.get("commissionAsset"))))
        fee_amount = fee_quote = fee_asset = None
        if parsed_fees:
            if len({asset for _, asset in parsed_fees}) != 1:
                return None
            fee_amount = sum((amount for amount, _ in parsed_fees), Decimal("0"))
            fee_asset = parsed_fees[0][1]
            if fee_asset == self.settings.risk.quote_asset:
                fee_quote = fee_amount
            elif fee_asset == proposal["symbol"][:-len(self.settings.risk.quote_asset)]:
                fee_quote = fee_amount * price
        quote_value = Decimal(str(entry.get("cumulative_quote_qty", ""))) if entry.get("cumulative_quote_qty") is not None else quantity * price
        return {"epoch_id": evidence.get("epoch_id"), "proposal_id": proposal["id"], "delegated_order_id": entry.get("order_id", list_or_order_id),
                "symbol": proposal["symbol"], "side": proposal["side"], "quantity": format(quantity, "f"),
                "price": format(price, "f"), "quote_value": format(quote_value, "f"),
                "fee_amount": format(fee_amount, "f") if fee_amount is not None else None,
                "fee_asset": fee_asset, "fee_quote": format(fee_quote, "f") if fee_quote is not None else None,
                "executed_at": evidence["executed_at"], "source": "riskpilot_live_execution",
                "order_list_id": evidence["entry"].get("order_list_id")}

    def _persist_live_execution_evidence(self, evidence: Mapping[str, Any]) -> None:
        key = (evidence.get("epoch_id"), evidence.get("proposal_id"),
               (evidence.get("entry") or {}).get("order_id"), evidence.get("side"))
        for existing in self.ledger.events_by_kind("live.execution_evidence"):
            entry = existing.get("entry") or {}
            if (existing.get("epoch_id"), existing.get("proposal_id"), entry.get("order_id"), existing.get("side")) == key:
                if existing.get("phase") == evidence.get("phase"):
                    return
        self.ledger.add_event("live.execution_evidence", str(evidence["proposal_id"]), dict(evidence))

    def _persist_live_risk_fill(self, fill: Mapping[str, Any]) -> None:
        key = (fill.get("epoch_id"), fill.get("proposal_id"),
               fill.get("delegated_order_id"), fill.get("side"))
        for existing in self.ledger.events_by_kind("live.risk_fill"):
            if (existing.get("epoch_id"), existing.get("proposal_id"),
                    existing.get("delegated_order_id", existing.get("order_id")), existing.get("side")) == key:
                return
        self.ledger.add_event("live.risk_fill", str(fill["proposal_id"]), dict(fill))

    def reconcile_live_risk_fill(self, proposal_id: str, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Reconcile one already-recorded LIVE execution using local evidence only."""
        if not operator_confirmed:
            raise SecurityError("LIVE risk-fill reconciliation requires explicit operator confirmation")
        proposal = self.ledger.get_proposal(validate_simple_id(proposal_id, "proposal_id"), include_private=True)
        if proposal.get("mode") != "live" or proposal.get("status") != "EXECUTED":
            raise SecurityError("LIVE risk-fill reconciliation requires an EXECUTED LIVE proposal")
        epoch = self._effective_live_risk_epoch()
        if not isinstance(epoch, Mapping) or epoch.get("status") != "active":
            return {"status": "PNL_INCOMPLETE", "reason": "RISKPILOT_SESSION_PNL_INCOMPLETE"}
        summary = proposal.get("execution_summary") or {}
        response = summary.get("binance_response")
        if not isinstance(response, Mapping):
            return {"status": "PNL_INCOMPLETE", "reason": "LIVE_FILL_PROVENANCE_INCOMPLETE"}
        try:
            order_id = int(proposal.get("execution_order_id"))
        except (TypeError, ValueError):
            return {"status": "PNL_INCOMPLETE", "reason": "LIVE_FILL_PROVENANCE_INCOMPLETE"}
        fill = self._verified_live_fill(proposal, response, order_id)
        if fill is None:
            return {"status": "PNL_INCOMPLETE", "reason": "LIVE_FILL_PROVENANCE_INCOMPLETE"}
        self._persist_live_risk_fill(fill)
        ok, _, _, reason = self._live_session_accounting()
        return {"status": "VERIFIED" if ok else "PNL_INCOMPLETE",
                "reason": None if ok else (reason or "RISKPILOT_SESSION_PNL_INCOMPLETE"),
                "proposal_id": proposal["id"], "delegated_order_id": fill["delegated_order_id"]}

    def reconcile_live_execution(self, proposal_id: str, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Reconcile one submitted LIVE order with one bounded read-only query."""
        if not operator_confirmed:
            raise SecurityError("LIVE execution reconciliation requires explicit operator confirmation")
        return self._reconcile_live_execution(proposal_id)

    def _is_unresolved_live_execution(self, proposal: Mapping[str, Any]) -> bool:
        if proposal.get("mode") != "live" or proposal.get("execution_status") not in {"EXEC_STARTED", "EXECUTING"}:
            return False
        if proposal.get("status") in {"REJECTED", "FAILED", "EXPIRED", "RECONCILE"}:
            return False
        evidence = [row for row in self.ledger.events_by_kind("live.execution_evidence")
                    if row.get("proposal_id") == proposal.get("id")]
        if not evidence:
            return False
        latest = evidence[-1]
        entry = latest.get("entry") or {}
        if str(latest.get("phase", "")).upper() == "FILLED" or (
                str(entry.get("status", "")).upper() == "FILLED"
                and Decimal(str(entry.get("executed_qty", "0"))) > 0):
            return False
        return (str(entry.get("status", "")).upper() in {"NEW", "PENDING_NEW", "PARTIALLY_FILLED"}
                and isinstance(entry.get("order_id"), int) and entry["order_id"] > 0
                and isinstance(entry.get("order_list_id"), int) and entry["order_list_id"] > 0
                and not any(row.get("proposal_id") == proposal.get("id") for row in self.ledger.events_by_kind("live.risk_fill")))

    def _reconcile_live_execution(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.ledger.get_proposal(validate_simple_id(proposal_id, "proposal_id"), include_private=True)
        if not self._is_unresolved_live_execution(proposal):
            raise SecurityError("LIVE execution reconciliation requires an unresolved LIVE submission")
        evidence = [row for row in self.ledger.events_by_kind("live.execution_evidence")
                    if row.get("proposal_id") == proposal["id"]]
        if not evidence:
            raise SecurityError("LIVE_EXECUTION_EVIDENCE_UNAVAILABLE")
        submitted = evidence[-1]
        entry = submitted.get("entry") or {}
        order_id, order_list_id = entry.get("order_id"), entry.get("order_list_id")
        if not isinstance(order_id, int) or order_id <= 0 or not isinstance(order_list_id, int) or order_list_id <= 0:
            raise SecurityError("LIVE_EXECUTION_EVIDENCE_PROVENANCE_INCOMPLETE")
        response = self.live_executor.read_spot_order_status(proposal["symbol"], order_id, order_list_id)
        if response.get("orderListId") != order_list_id or response.get("orderId") not in {None, order_id}:
            raise SecurityError("LIVE execution reconciliation provenance mismatch")
        reports = response.get("orderReports")
        if isinstance(reports, list):
            observed_protection = {row.get("orderId") for row in reports if isinstance(row, Mapping) and row.get("side") == "SELL"}
            expected_protection = {row.get("order_id") for row in submitted.get("protection", []) if isinstance(row, Mapping)}
            if observed_protection and expected_protection and observed_protection != expected_protection:
                raise SecurityError("LIVE execution reconciliation protection provenance mismatch")
        response.setdefault("orderListId", order_list_id)
        response.setdefault("orderId", order_id)
        if not isinstance(response.get("side"), str):
            response["side"] = proposal.get("side")
        updated = self._live_execution_evidence(proposal, response, order_id)
        if updated is None:
            raise SecurityError("LIVE_EXECUTION_EVIDENCE_PROVENANCE_INCOMPLETE")
        self._persist_live_execution_evidence(updated)
        status = str((updated.get("entry") or {}).get("status", response.get("status", ""))).upper()
        if status in {"NEW", "PENDING_NEW"}:
            return {"status": "EXECUTING", "fill": "WAITING", "accounting_status": "WAITING_FOR_FILL", "proposal_id": proposal["id"]}
        if status in {"CANCELED", "EXPIRED", "REJECTED"}:
            result = self.ledger.finish_execution(proposal["id"], proposal["execution_lease_hash"], "RECONCILE", str(order_id), status, {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"], "accounting_status": "NO_FILL", "execution_evidence": updated}, allow_legacy_unresolved=True)
            return {**result, "accounting_status": "NO_FILL"}
        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            result = self.ledger.finish_execution(proposal["id"], proposal["execution_lease_hash"], "RECONCILE", str(order_id), "UNKNOWN", {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"], "accounting_status": "RECONCILE", "accounting_reason": "AMBIGUOUS_LIVE_ORDER_STATUS", "execution_evidence": updated}, allow_legacy_unresolved=True)
            return {**result, "accounting_status": "RECONCILE", "accounting_reason": "AMBIGUOUS_LIVE_ORDER_STATUS"}
        if status == "PARTIALLY_FILLED":
            return {"status": "EXECUTING", "fill": "PARTIAL", "accounting_status": "WAITING_FOR_FILL", "proposal_id": proposal["id"]}
        fill = self._verified_live_fill(proposal, response, order_id)
        result = self.ledger.finish_execution(proposal["id"], proposal["execution_lease_hash"], "EXECUTED", str(order_id), status, {"simulated": False, "symbol": proposal["symbol"], "side": proposal["side"], "execution_evidence": updated}, allow_legacy_unresolved=True)
        if fill is None:
            return {**result, "accounting_status": "RECONCILE", "accounting_reason": "LIVE_FILL_PROVENANCE_INCOMPLETE"}
        self._persist_live_risk_fill(fill)
        ok, _, _, reason = self._live_session_accounting()
        return {**result, "accounting_status": "VERIFIED" if ok else "RECONCILE", "accounting_reason": None if ok else (reason or "RISKPILOT_SESSION_PNL_INCOMPLETE")}

    def monitor_live_executions(self) -> dict[str, Any]:
        """Single-pass monitor hook; no polling loop or automatic retries."""
        results = []
        for proposal in self.ledger.active_proposals_status():
            if proposal.get("mode") == "live" and proposal.get("status") == "EXECUTING":
                results.append(self._reconcile_live_execution(proposal["id"]))
        return {"results": results}

    def finalize_live_risk_epoch(self, *, operator_confirmed: bool = False,
                                 empty_only: bool = False) -> dict[str, Any]:
        """Close an incomplete flat epoch without deleting or resetting history."""
        if not operator_confirmed:
            raise SecurityError("LIVE risk-epoch finalization requires explicit operator confirmation")
        epoch = self._effective_live_risk_epoch()
        if not isinstance(epoch, Mapping) or epoch.get("status") not in {"ACTIVE", "active", "RECONCILE", "CLOSED_INCOMPLETE", "CLOSED", "ABORTED_EMPTY"}:
            raise SecurityError("no incomplete LIVE risk epoch is available for finalization")
        if epoch.get("status") in {"CLOSED_INCOMPLETE", "CLOSED", "ABORTED_EMPTY"}:
            return {"status": epoch["status"], "epoch_id": epoch.get("epoch_id"), "realized_pnl_verified": False}
        if self.live_executor.rate_limit_status().get("status") == "BLOCKED":
            return {"status": "RATE_LIMIT_BLOCKED", "blocked_until": self.live_executor.rate_limit_status().get("blocked_until"),
                    "epoch_id": epoch.get("epoch_id"), "realized_pnl_verified": False}
        epoch_id = epoch.get("epoch_id")
        active = [row for row in self.ledger.active_proposals_status() if row.get("mode") == "live"]
        if active:
            raise SecurityError("pending or executing LIVE proposal prevents epoch finalization")
        proposals = [row for row in self.ledger.list_proposals(limit=1000)
                     if row.get("mode") == "live" and isinstance(row.get("symbol"), str)
                     and self._after_epoch_start(row.get("created_at"), epoch.get("started_at"))]
        symbols = sorted({row["symbol"] for row in proposals})
        if not symbols:
            if not self._is_proven_empty_live_epoch(epoch):
                raise SecurityError("LIVE epoch has no durable touched-symbol evidence; finalization refused")
            return self._finalize_empty_live_epoch(epoch)
        if empty_only:
            raise SecurityError("LIVE epoch has durable trading activity; use normal finalization")
        account = self.live_executor.read_spot_account()
        open_orders = self.live_executor.read_open_spot_orders()
        if any(row.get("symbol") in symbols for row in open_orders):
            raise SecurityError("active Spot protection/order remains for the legacy LIVE epoch")
        dust_balances = []
        quote = self.settings.risk.quote_asset
        balances = {row.get("asset"): Decimal(str(row.get("free", "0"))) + Decimal(str(row.get("locked", "0")))
                    for row in account.get("balances", []) if isinstance(row, Mapping)}
        for symbol in symbols:
            base = symbol[:-len(quote)]
            residual = balances.get(base, Decimal("0"))
            if residual <= 0:
                continue
            filters = validate_spot_symbol(self.settings, symbol, live=True)
            rounded = floor_to_step(residual, Decimal(str(filters["market_step_size"])))
            reference_price = None
            observed_at = None
            if rounded >= Decimal(str(filters["market_min_qty"])):
                if self.live_executor.rate_limit_status().get("status") == "BLOCKED":
                    return {"status": "RATE_LIMIT_BLOCKED", "blocked_until": self.live_executor.rate_limit_status().get("blocked_until")}
                # This is the only current-price read for this symbol.  Its
                # exact-symbol validation and fresh timestamp are retained as
                # provenance for the dust decision.
                snapshot = fetch_spot_snapshot(self.settings, symbol)
                if snapshot.symbol != symbol or snapshot.bid <= 0 or not snapshot.bid.is_finite():
                    raise SecurityError("verified Spot reference price is invalid")
                reference_price = snapshot.bid
                observed_at = snapshot.observed_at_ms
            balance_state = classify_spot_base_balance(residual, filters, reference_price)
            if balance_state["tradable"]:
                raise SecurityError(f"meaningful LIVE base balance remains for {symbol}; finalization refused")
            dust_balances.append({"symbol": symbol, "asset": base, "quantity": format(residual, "f"),
                                  "classification": balance_state["classification"],
                                  "reference_price_used": format(reference_price, "f") if reference_price is not None else None,
                                  "price_observed_at_ms": observed_at,
                                  "minimum_quantity": balance_state.get("minimum_quantity"),
                                  "minimum_notional": balance_state.get("minimum_notional"),
                                  "classification_reason": (
                                      "below_exchange_minimum_quantity" if rounded < Decimal(str(filters["market_min_qty"]))
                                      else "below_exchange_minimum_notional" if balance_state["classification"] == "EXCHANGE_DUST"
                                      else "valid_exchange_sell_quantity_and_notional")})
        finalized = {**dict(epoch), "status": "CLOSED_INCOMPLETE", "finalized_at": isoformat(),
                     "previous_status": epoch.get("status"),
                     "finalization_reason": "LEGACY_FLAT_EPOCH_MISSING_FILL_PROVENANCE",
                     "realized_pnl_verified": False, "accounting_complete": False,
                     "closed_at": isoformat(), "touched_symbols": symbols,
                     "dust_balances": dust_balances, "operator_confirmed": True}
        self.ledger.add_event("live.risk_epoch", None, finalized)
        self.ledger.add_event("live.risk_epoch_finalized", None, {"epoch_id": epoch_id, "status": "CLOSED_INCOMPLETE"})
        return {"status": "CLOSED_INCOMPLETE", "epoch_id": epoch_id,
                "realized_pnl_verified": False}

    def _is_proven_empty_live_epoch(self, epoch: Mapping[str, Any]) -> bool:
        """Prove no RiskPilot LIVE activity existed, without exchange access."""
        epoch_id = epoch.get("epoch_id")
        for fill in self.ledger.events_by_kind("live.risk_fill"):
            if fill.get("epoch_id") == epoch_id:
                return False
        for event_kind in ("live.risk_accounting", "live.session_flat", "execution.started",
                           "execution.completed", "execution.result"):
            for event in self.ledger.events_by_kind(event_kind):
                if event.get("epoch_id") == epoch_id or self._after_epoch_start(
                        event.get("recorded_at"), epoch.get("started_at")):
                    return False
        # Any LIVE proposal created during the epoch is activity, even if it
        # never reached execution. Unknown timestamps are not proof of empty.
        for proposal in self.ledger.list_proposals(limit=1000):
            if proposal.get("mode") != "live":
                continue
            created = proposal.get("created_at")
            if not isinstance(created, str) or not isinstance(epoch.get("started_at"), str):
                return False
            if self._after_epoch_start(created, epoch["started_at"]):
                return False
        return True

    def _finalize_empty_live_epoch(self, epoch: Mapping[str, Any]) -> dict[str, Any]:
        finalized = {**dict(epoch), "status": "ABORTED_EMPTY",
                     "previous_status": epoch.get("status"),
                     "finalization_reason": "EMPTY_EPOCH_NO_TRADING_ACTIVITY",
                     "realized_pnl_verified": False, "accounting_complete": False,
                     "closed_at": isoformat(), "operator_confirmed": True}
        self.ledger.add_event("live.risk_epoch", None, finalized)
        self.ledger.add_event("live.risk_epoch_finalized", None, {
            "epoch_id": epoch.get("epoch_id"), "status": "ABORTED_EMPTY",
            "reason": "EMPTY_EPOCH_NO_TRADING_ACTIVITY"})
        return {"status": "ABORTED_EMPTY", "epoch_id": epoch.get("epoch_id"),
                "realized_pnl_verified": False, "accounting_complete": False}

    @staticmethod
    def _after_epoch_start(created_at: Any, started_at: Any) -> bool:
        try:
            return parse_time(created_at) >= parse_time(started_at)
        except (TypeError, ValueError):
            return False

    @localized
    def approve_live_button(self, proposal_id: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal["mode"] != "live":
            raise SecurityError("native live approval can approve LIVE proposals only")
        token = self.signer.approval_token(proposal["canonical_json"])
        claim = self.claim(proposal_id, token, sender_id, chat_id)
        return self.execute_live(proposal_id, claim["lease"])

    @localized
    def reject_live_button(self, proposal_id: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal["mode"] != "live":
            raise SecurityError("native live rejection can reject LIVE proposals only")
        return self.reject(proposal_id, self.signer.approval_token(proposal["canonical_json"]), sender_id, chat_id)

    def _execute_claimed_paper(self, proposal: dict[str, Any], lease_hash: str) -> dict[str, Any]:
        proposal_id = proposal["id"]
        if proposal["mode"] != "paper" or proposal["canonical"].get("mode") != "paper":
            raise SecurityError("paper executor cannot execute a live proposal")
        candidate = self.ledger.get_candidate(proposal["candidate_id"])
        is_demo = bool(candidate["metrics"].get("paper_demo"))
        if is_demo:
            fill_price = Decimal(proposal["entry_reference"])
            step = Decimal("0.00000001")
            min_notional = self.settings.risk.min_quote_amount
        else:
            market = fetch_spot_snapshot(self.settings, proposal["symbol"])
            fill_price, step, min_notional = market.ask, market.step_size, market.min_notional
            reference = Decimal(proposal["entry_reference"])
            drift = abs(fill_price - reference) / reference * Decimal("100")
            if drift > self.settings.market.max_entry_drift_pct:
                if not self.settings.sizing_policy.enabled:
                    self.ledger.fail_execution(proposal_id, lease_hash, "fresh price drift exceeds configured limit; requote required")
                raise PolicyError("fresh price drift exceeds configured limit; proposal rejected, request a requote")
        requested = Decimal(proposal["quote_amount"])
        gross = floor_to_step(requested / fill_price, step)
        if gross <= 0 or gross * fill_price < min_notional:
            if not self.settings.sizing_policy.enabled:
                self.ledger.fail_execution(proposal_id, lease_hash, "fresh exchange filters fail minimum notional or quantity step")
            raise PolicyError(
                "EXCHANGE_FILTER_FAILED: fresh exchange filters reject the immutable fill; proposal rejected, request a new proposal"
            )
        plan = build_fill_risk(self.settings, proposal, fill_price, gross)
        self._validate_stored_policy_snapshot(proposal)
        fresh_bid = fill_price if is_demo else market.bid
        fresh_ask = fill_price if is_demo else market.ask
        proposal_equity = self._proposal_equity(proposal)
        context, symbols = self._paper_policy_context(
            price_overrides={proposal["symbol"]: fresh_bid},
            effective_equity=proposal_equity,
        )
        self._validate_equity_drift(proposal_equity, context.equity.equity)
        if self.settings.sizing_policy.percentage_based:
            existing_exposure, existing_risk = self._paper_symbol_usage(
                proposal["symbol"]
            )
            sizing = size_entry(
                context,
                entry_price=Decimal(proposal["entry_reference"]),
                stop_price=Decimal(proposal["stop_reference"]),
                existing_position_exposure=existing_exposure,
                existing_position_risk=existing_risk,
                requested_notional=requested,
                fee_buffer_rate=self.settings.risk.paper_fee_pct / Decimal("100"),
            )
            if not sizing.accepted:
                raise PolicyError(
                    "REVALIDATION_FAILED: ACCOUNT_STATE_CHANGED: immutable proposal was not resized; "
                    + "; ".join(sizing.reasons)
                )
        projection = self._paper_risk_projection(proposal["symbol"], fill_price, plan,
            bid=fresh_bid, ask=fresh_ask,
            reward_risk=Decimal(proposal["reward_risk"]),
            effective_limits=context.effective_limits)
        evaluation = self._evaluate_paper_entry(
            context, symbols, proposal["symbol"], requested,
            projected_position_risk=Decimal(str(projection["new_position_risk"])),
            projected_aggregate_risk=Decimal(str(projection["projected_aggregate_risk"])),
            projected_exposure=(
                context.usage.open_exposure + Decimal(str(plan["quote_spent"]))
            ),
            projected_position_exposure=(
                self._paper_symbol_usage(proposal["symbol"])[0]
                + Decimal(str(plan["quote_spent"]))
            ),
            estimated_fee=(
                requested * self.settings.risk.paper_fee_pct / Decimal("100")
                if self.settings.sizing_policy.percentage_based
                else Decimal("0")
            ),
        )
        self._raise_policy_rejection(evaluation, phase="execution")
        order_id = f"paper-{proposal_id[2:]}"
        position_id = f"pp-{proposal_id[2:]}"
        opened_at = isoformat()
        position = {"id": position_id, "proposal_id": proposal_id, "symbol": proposal["symbol"],
                    "entry_reference": proposal["entry_reference"], "opened_at": opened_at, **plan}
        existing_symbol = [row for row in self.ledger.list_paper_positions(True) if row["symbol"] == proposal["symbol"]]
        economic = None
        if existing_symbol:
            new_qty = Decimal(plan["net_base_quantity"]); total_qty = new_qty + sum((Decimal(row["net_quantity"]) for row in existing_symbol), Decimal("0"))
            weighted = (fill_price * new_qty + sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in existing_symbol), Decimal("0"))) / total_qty
            stop = max(Decimal(row["final_stop"]) for row in existing_symbol)
            target = Decimal(projection["projected_target"])
            validate_long_bracket(weighted, stop, target, Decimal(proposal["reward_risk"]),
                bid=fill_price if is_demo else market.bid, ask=fill_price if is_demo else market.ask)
            economic = {"weighted_average_entry": str(weighted), "final_stop": str(stop),
                "final_target": str(target), "risk_amount": str(projection["new_position_risk"]),
                "active_tranche_count": len(existing_symbol) + 1}
        summary = {"simulated": True, "warning": "MANUAL PAPER TEST — NOT A REAL ORDER"
                   if proposal["canonical"].get("source") == "manual-paper-test" else "PAPER FILL — NOT A REAL ORDER",
                   "source": proposal["canonical"].get("source"), "symbol": proposal["symbol"],
                   "side": "BUY", "requested_quote_amount": str(requested),
                   "average_fill_price": plan["average_fill_price"],
                   "gross_base_quantity": plan["gross_base_quantity"],
                   "simulated_fee": plan["entry_fee_base"],
                   "net_base_quantity": plan["net_base_quantity"],
                   "actual_paper_spend": plan["quote_spent"],
                   "stop_loss": economic["final_stop"] if economic else plan["final_stop"], "take_profit": economic["final_target"] if economic else plan["final_target"],
                   "risk_amount": economic["risk_amount"] if economic else plan["risk_amount"],
                   "net_expected_reward_risk": plan["net_expected_reward_risk"],
                   "paper_order_id": order_id, "position_id": position_id, "timestamp": opened_at,
                   "economic_position": economic}
        fee_usdt = Decimal(plan["entry_fee_base"]) * fill_price
        result = self.ledger.finish_paper_and_open(proposal_id, lease_hash, order_id, summary,
            position, fee_usdt, self.settings.paper.max_active_tranches,
            context.effective_limits.max_total_open_exposure,
            context.effective_limits.max_risk_per_position,
            context.effective_limits.max_aggregate_open_risk,
            self.settings.risk.paper_fee_pct / Decimal("100"),
            self.settings.paper.slippage_pct / Decimal("100"),
            fresh_bid, fresh_ask,
            context.effective_limits.max_economic_positions,
            context.equity.reserve_quote,
            context.effective_limits.max_daily_realized_loss,
            utcnow().date().isoformat(),
            context.effective_limits.max_entry_notional,
            context.effective_limits.max_weekly_realized_loss,
            isoformat(
                utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=utcnow().weekday())
            ))
        self.ledger.add_event("paper.fill", proposal_id, summary)
        return result

    def complete_live(
        self,
        proposal_id: str,
        lease: str,
        order_id: str,
        execution_status: str,
        filled_quantity: str | None = None,
        average_price: str | None = None,
        fee_quote: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.live.enabled:
            raise SecurityError("live completion is disabled locally")
        proposal, lease_hash = self._verify_execution_lease(proposal_id, lease)
        order_id = validate_simple_id(order_id, "order_id")
        normalized_status = execution_status.upper()
        if normalized_status not in {"FILLED", "SUCCESS", "ACCEPTED", "NEW", "PARTIALLY_FILLED"}:
            raise SpotGuardError("execution status is not a recognized successful or pending state")
        final_status = "EXECUTED" if normalized_status in {"FILLED", "SUCCESS"} else "RECONCILE"
        summary = {
            "simulated": False,
            "symbol": proposal["symbol"],
            "side": proposal["side"],
            "quote_amount_limit": proposal["quote_amount"],
            "filled_quantity": filled_quantity,
            "average_price": average_price,
        }
        result = self.ledger.finish_execution(
            proposal_id,
            lease_hash,
            final_status,
            order_id,
            normalized_status,
            summary,
        )
        if final_status == "EXECUTED":
            # This legacy completion API has no delegated response and cannot
            # prove actual quantity, fill price, or commission.  Do not turn
            # caller-supplied estimates into a risk fill.
            self.ledger.add_event("live.risk_accounting", proposal_id, {
                "status": "RECONCILE", "reason": "LIVE_FILL_PROVENANCE_INCOMPLETE",
            })
        return result

    def fail_execution(self, proposal_id: str, lease: str, reason: str) -> dict[str, Any]:
        _, lease_hash = self._verify_execution_lease(proposal_id, lease)
        sanitized = bounded_text(reason, "reason", maximum=200)
        return self.ledger.fail_execution(proposal_id, lease_hash, sanitized)

    def uncertain_execution(self, proposal_id: str, lease: str, reason: str) -> dict[str, Any]:
        if not self.settings.live.enabled:
            raise SecurityError("uncertain execution reconciliation is disabled locally")
        proposal, lease_hash = self._verify_execution_lease(proposal_id, lease)
        sanitized = bounded_text(reason, "reason", maximum=200)
        summary = {
            "simulated": False,
            "symbol": proposal["symbol"],
            "side": proposal["side"],
            "quote_amount_limit": proposal["quote_amount"],
            "uncertain_reason": sanitized,
            "required_action": "reconcile against Binance Agent OS order history; never retry automatically",
        }
        if proposal["canonical"].get("source") == "manual-live-partial-exit":
            summary["recovery"] = {
                "retry_sell": f"/spot live-close-all {proposal['symbol']}",
                "rearm_bracket": {"stop": proposal["canonical"]["stop_reference"], "target": proposal["canonical"]["take_profit_reference"], "remaining_quantity": proposal["canonical"]["remaining_quantity"]},
                "approval_required": True,
            }
        return self.ledger.finish_execution(
            proposal_id,
            lease_hash,
            "RECONCILE",
            f"unknown:{proposal_id}",
            "UNKNOWN",
            summary,
        )

    def live_status(self, check_symbols: bool = False,
                    symbols: list[str] | None = None) -> dict[str, Any]:
        arm = self.live_arm.status()
        agent = self.agent_os.status()
        symbol_checks: list[dict[str, Any]] = []
        if check_symbols:
            targets = symbols if symbols is not None else self.settings.live.allowed_symbols
            for symbol in targets:
                try:
                    item = validate_spot_symbol(self.settings, symbol, live=True)
                    item["protected_live_supported"] = bool(item.get("oto_allowed") and item.get("opo_allowed") and item.get("oco_allowed") and Decimal(item.get("price_tick_size", "0")) > 0 and item.get("percent_price_filter") and item.get("max_num_orders", 0) > 0 and item.get("max_num_algo_orders", 0) > 0 and item.get("max_num_order_lists", 0) > 0)
                except Exception as exc:
                    item = {"symbol": symbol, "protected_live_supported": False, "reason": str(exc)}
                symbol_checks.append(item)
        flags_ok = bool(symbol_checks) and all(item["protected_live_supported"] for item in symbol_checks)
        connected = bool(agent.get("authenticated") and agent.get("mcp_configured"))
        proofs = self.live_executor.verify_readiness(
            connected=connected, symbol_flags_verified=flags_ok,
            permission_attestation=self.ledger.latest_event("live.trade_permission_attestation"),
            decimal_transport_attestation=self.ledger.latest_event("live.decimal_transport_attestation"))
        readiness = self.live_executor.readiness(
            connected=connected, armed=arm.armed, symbol_flags_verified=flags_ok,
            account_read_verified=proofs["account_read_verified"],
            open_orders_read_verified=proofs["open_orders_read_verified"],
            spot_trade_scope_verified=proofs["spot_trade_scope_verified"],
            write_tool_discovered=proofs["write_tool_discovered"],
            write_schema_verified=proofs["write_schema_verified"],
            decimal_transport_verified=proofs["decimal_transport_verified"])
        result = readiness.to_dict()
        result["readiness_reasons"] = proofs.get("reasons", [])
        result["decimal_transport_mode"] = proofs.get("decimal_transport_mode") or ("bounded" if proofs["decimal_transport_verified"] else "blocked")
        result["minimum_verified_fractional_number"] = proofs.get("minimum_verified_fractional_number")
        result["decimal_transport_attestation_present"] = proofs.get(
            "decimal_transport_attestation_present", proofs["decimal_transport_verified"])
        result["decimal_transport_remote_refresh_available"] = proofs.get(
            "decimal_transport_remote_refresh_available", True)
        result["blockers"].extend(reason for reason in result["readiness_reasons"]
                                   if reason not in result["blockers"])
        minimum_profile_balance = (
            None
            if self.settings.sizing_policy.percentage_based
            else self.settings.live.max_quote_per_entry_usdt
            + self.settings.live.min_free_reserve_usdt
        )
        balance_note = (
            "Schema-v2 entry and reserve allowances depend on fresh Spot equity and free quote; use policy explain for a read-only snapshot."
            if self.settings.sizing_policy.percentage_based
            else "A 28 USDT account cannot support a 100 USDT entry plus the 8 USDT reserve. This status report does not claim an account-read or write-scope verification; the execution gate requires independent evidence."
        )
        result.update({"arm_expires_at": arm.expires_at,
            "rate_limit_status": self.live_executor.rate_limit_status(),
            "account_trade_history_available": False,
            "account_global_realized_loss_verified": False,
            "account_global_realized_loss_reason": "ACCOUNT_TRADE_HISTORY_CAPABILITY_UNAVAILABLE",
            "realized_loss_scope": "riskpilot_session",
            "riskpilot_session_accounting_verified": self._live_session_accounting()[0]
                if isinstance(self._effective_live_risk_epoch(), Mapping) else False,
            "max_quote_per_entry_usdt": str(self.settings.live.max_quote_per_entry_usdt),
            "entry_slippage_cap_pct": str(self.settings.live.entry_slippage_cap_pct),
            "max_active_tranches": self.settings.live.max_active_tranches,
            "max_economic_positions": self.settings.live.max_economic_positions,
            "max_open_exposure_usdt": str(self.settings.live.max_open_exposure_usdt),
            "min_free_reserve_usdt": str(self.settings.live.min_free_reserve_usdt),
            "max_risk_per_position_usdt": str(self.settings.live.max_risk_per_position_usdt),
            "max_aggregate_risk_usdt": str(self.settings.live.max_aggregate_risk_usdt),
            "daily_realized_loss_cap_usdt": str(self.settings.live.daily_realized_loss_cap_usdt),
            "max_successful_entries_per_utc_day": self.settings.live.max_successful_entries_per_utc_day,
            "minimum_profile_balance_before_fees_usdt": (
                str(minimum_profile_balance)
                if minimum_profile_balance is not None else None
            ),
            "balance_profile_note": balance_note,
            "max_pending_proposals": self.settings.live.max_pending_proposals,
            "allowed_symbols": list(self.settings.live.allowed_symbols), "symbol_checks": symbol_checks})
        return result

    def prepare_live_session(self, symbol: str, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Collect one bounded, operator-confirmed LIVE preflight session.

        The adapter instance owns the wrapper/catalog cache for the complete
        session.  This method never arms LIVE and never creates a proposal.
        """
        if not operator_confirmed:
            raise SecurityError("LIVE session preparation requires explicit operator confirmation")
        if symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("LIVE session symbol is not allowlisted")
        executor = self.live_executor
        permission_event = self.ledger.latest_event("live.trade_permission_attestation")
        decimal_event = self.ledger.latest_event("live.decimal_transport_attestation")
        circuit = executor.rate_limit_status()

        def blocked_result(reason: str = "binance_rate_limit_blocked") -> dict[str, Any]:
            present = isinstance(decimal_event, Mapping) and decimal_event.get("result") == "verified"
            mode = "bounded" if present else None
            floor = decimal_event.get("minimum_verified_fractional_number") if present else None
            return {"rate_limit_status": circuit, "account_read_verified": False,
                    "open_orders_read_verified": False, "spot_trade_scope_verified": False,
                    "write_tool_discovered": False, "write_schema_verified": False,
                    "decimal_transport_verified": False, "decimal_transport_attestation_present": present,
                    "decimal_transport_mode": mode, "minimum_verified_fractional_number": floor,
                    "decimal_transport_remote_refresh_available": False,
                    "symbol_exchange_flags_verified": False,
                    "protective_order_capability_verified": False, "live_limits_valid": False,
                    "live_enabled": self.settings.live.enabled, "live_armed": False,
                    "execution_ready": False, "blockers": [reason],
                    "mcp_calls_by_category": executor.request_budget()}

        if circuit["status"] == "BLOCKED":
            return blocked_result()
        executor.begin_request_budget()
        arm = self.live_arm.status()
        agent = self.agent_os.status()
        connected = bool(agent.get("authenticated") and agent.get("mcp_configured"))
        if not connected:
            return blocked_result("backend_not_connected")

        try:
            exchange = validate_spot_symbol(self.settings, symbol, live=True)
            flags_ok = bool(exchange.get("oto_allowed") and exchange.get("opo_allowed")
                            and exchange.get("oco_allowed") and Decimal(exchange.get("price_tick_size", "0")) > 0
                            and exchange.get("percent_price_filter") and exchange.get("max_num_orders", 0) > 0
                            and exchange.get("max_num_algo_orders", 0) > 0 and exchange.get("max_num_order_lists", 0) > 0)
        except Exception as exc:
            exchange, flags_ok = {}, False
            symbol_reason = f"symbol_exchange_flags:{exc}"
        else:
            symbol_reason = None

        proofs = executor.verify_readiness(
            connected=True, symbol_flags_verified=flags_ok,
            permission_attestation=permission_event,
            decimal_transport_attestation=decimal_event)
        if executor.rate_limit_status()["status"] == "BLOCKED":
            circuit = executor.rate_limit_status()
            return blocked_result()
        permission_ok = bool(proofs.get("spot_trade_scope_verified"))
        decimal_ok = bool(proofs.get("decimal_transport_verified"))
        decimal_probe_needed = not decimal_ok
        if decimal_ok and isinstance(decimal_event, Mapping):
            try:
                # A valid but weaker historical proof may be strengthened by
                # the canonical BTCUSDT floor probe.  Never weaken it.
                decimal_probe_needed = Decimal(str(decimal_event.get(
                    "minimum_verified_fractional_number"))) > executor._REMOTE_DECIMAL_FLOOR
            except (ArithmeticError, TypeError, ValueError):
                decimal_probe_needed = True
        discovery = None
        if not permission_ok or decimal_probe_needed:
            discovery = executor.execution_discovery()
        market = None
        permission_result = None
        decimal_result = None
        if not permission_ok:
            market = fetch_spot_snapshot(self.settings, symbol)
            permission_result = executor.attest_spot_trade_permission(symbol, exchange, market, discovery=discovery)
            if permission_result.get("classification") == "SUCCESS":
                now = utcnow()
                proof = {"result": "verified", "verified_at": isoformat(now),
                         "expires_at": isoformat(now + timedelta(seconds=executor._PERMISSION_ATTESTATION_TTL_SECONDS)),
                         "backend": self.settings.codex.mcp_server,
                         "profile_fingerprint": executor.execution_profile_fingerprint(),
                         "delegated_operation": "spot.orderTest",
                         "schema_fingerprint": permission_result["schema_fingerprint"]}
                self.ledger.add_event("live.trade_permission_attestation", None, proof)
                permission_ok = proofs.get("account_type") == "SPOT" and proofs.get("can_trade") is True
        if executor.rate_limit_status()["status"] == "BLOCKED":
            circuit = executor.rate_limit_status()
            return blocked_result()
        if decimal_probe_needed:
            # Decimal transport is a backend capability.  It must use the
            # canonical low-floor symbol, not the requested execution pair's
            # notional-derived quantity.
            attestation_symbol = (
                "BTCUSDT" if "BTCUSDT" in self.settings.live.allowed_symbols else symbol
            )
            if attestation_symbol == symbol:
                decimal_exchange, decimal_market = exchange, market
            else:
                decimal_exchange = validate_spot_symbol(self.settings, attestation_symbol, live=True)
                decimal_market = fetch_spot_snapshot(self.settings, attestation_symbol)
            decimal_result = executor.attest_decimal_transport(
                attestation_symbol, decimal_exchange, decimal_market, discovery=discovery)
            if decimal_result.get("classification") == "SUCCESS":
                payload = decimal_result.get("payload", {})
                quantity = Decimal(str(payload.get("quantity", "0")))
                price = Decimal(str(payload.get("price", "0")))
                if quantity >= executor._REMOTE_DECIMAL_FLOOR and quantity != quantity.to_integral_value() and price > 0 and price != price.to_integral_value():
                    now = utcnow()
                    effective_floor = quantity
                    if executor._valid_decimal_transport_attestation(
                            decimal_event, decimal_result["schema_fingerprint"]):
                        try:
                            effective_floor = min(effective_floor, Decimal(str(
                                decimal_event["minimum_verified_fractional_number"])))
                        except (ArithmeticError, TypeError, ValueError):
                            pass
                    proof = {"result": "verified", "verified_at": isoformat(now),
                             "expires_at": isoformat(now + timedelta(seconds=executor._PERMISSION_ATTESTATION_TTL_SECONDS)),
                             "backend": self.settings.codex.mcp_server,
                             "profile_fingerprint": executor.execution_profile_fingerprint(),
                             "delegated_operation": "spot.orderTest", "schema_fingerprint": decimal_result["schema_fingerprint"],
                             "symbol": attestation_symbol, "attestation_symbol": attestation_symbol,
                             "target_symbol": symbol, "tested_fields": ["quantity", "price"],
                             "wire_mode": "fixed-point-json-number",
                             "minimum_verified_fractional_number": format(effective_floor, "f"),
                             "classification": "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG",
                             "scope": "bounded_decimal_domain"}
                    self.ledger.add_event("live.decimal_transport_attestation", None, proof)
                    decimal_ok = True
        if executor.rate_limit_status()["status"] == "BLOCKED":
            circuit = executor.rate_limit_status()
            return blocked_result()
        readiness = executor.readiness(
            connected=True, armed=arm.armed, symbol_flags_verified=flags_ok,
            account_read_verified=proofs["account_read_verified"],
            open_orders_read_verified=proofs["open_orders_read_verified"],
            spot_trade_scope_verified=permission_ok,
            write_tool_discovered=proofs["write_tool_discovered"],
            write_schema_verified=proofs["write_schema_verified"],
            decimal_transport_verified=decimal_ok)
        result = readiness.to_dict()
        result.update({"rate_limit_status": executor.rate_limit_status(),
                       "decimal_transport_mode": "bounded" if decimal_ok else "blocked",
                       "minimum_verified_fractional_number": (
                           (decimal_event or {}).get("minimum_verified_fractional_number") if not decimal_result
                           else (format(Decimal(str(decimal_result.get("payload", {}).get("quantity"))), "f")
                                 if decimal_result.get("payload", {}).get("quantity") is not None else None)),
                       "decimal_transport_remote_refresh_available": True,
                       "mcp_calls_by_category": executor.request_budget(),
                       "symbol": symbol})
        # `readiness` is the sole source of execution blockers.  Probe reasons
        # remain diagnostic, but stale pre-attestation reasons must not survive
        # after the corresponding final proof has become valid.
        result["readiness_reasons"] = proofs.get("reasons", [])
        if decimal_ok:
            result["upstream_decimal_limitation"] = "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG"
        if symbol_reason and symbol_reason not in result["blockers"]:
            result["blockers"].append(symbol_reason)
        if not result["execution_ready"] and not result["blockers"]:
            result["blockers"] = ["live_not_ready"]
        result["permission_attestation"] = permission_result and {"classification": permission_result.get("classification")}
        result["decimal_attestation"] = decimal_result and {"classification": decimal_result.get("classification")}
        if (not arm.armed and result["execution_ready"] is False
                and result["blockers"] == ["live_armed"]):
            self._ensure_live_risk_epoch(proofs.get("account_balances", []))
            prepared_at = utcnow()
            ticket = {"result": "prepared", "prepared_at": isoformat(prepared_at),
                      "expires_at": isoformat(prepared_at + timedelta(minutes=5)),
                      "target_symbol": symbol, "backend": self.settings.codex.mcp_server,
                      "profile_fingerprint": executor.execution_profile_fingerprint(),
                      "schema_fingerprint": proofs.get("test_order_schema_fingerprint"),
                      "account_read_verified": result["account_read_verified"],
                      "open_orders_read_verified": result["open_orders_read_verified"],
                      "spot_trade_scope_verified": result["spot_trade_scope_verified"],
                      "write_tool_discovered": result["write_tool_discovered"],
                      "write_schema_verified": result["write_schema_verified"],
                      "decimal_transport_verified": result["decimal_transport_verified"],
                      "decimal_transport_mode": result.get("decimal_transport_mode"),
                      "minimum_verified_fractional_number": result.get("minimum_verified_fractional_number"),
                      "protective_order_capability_verified": result["protective_order_capability_verified"],
                      "symbol_exchange_flags_verified": result["symbol_exchange_flags_verified"],
                      "live_limits_valid": result["live_limits_valid"],
                      "live_enabled": result["live_enabled"],
                      "rate_limit_status": "CLEAR", "final_blockers": ["live_armed"]}
            self.ledger.add_event("live.session_prepared", None, ticket)
        return result

    def _ensure_live_risk_epoch(self, balances: list[Mapping[str, Any]]) -> dict[str, Any]:
        profile = self.live_executor.execution_profile_fingerprint()
        current = self._effective_live_risk_epoch()
        if (isinstance(current, Mapping) and str(current.get("status", "")).upper() == "ACTIVE"
                and current.get("profile_fingerprint") == profile):
            return dict(current)
        if isinstance(current, Mapping) and str(current.get("status", "")).upper() == "ACTIVE":
            raise SecurityError("RISKPILOT_SESSION_PNL_INCOMPLETE: active LIVE risk epoch belongs to another execution profile")
        if self._epoch_blocks_new_live_entry(current):
            raise SecurityError("RISKPILOT_SESSION_PNL_INCOMPLETE: LIVE risk epoch requires operator reconciliation")
        quote = sum((Decimal(str(row.get("free", "0"))) for row in balances
                     if row.get("asset") == self.settings.risk.quote_asset), Decimal("0"))
        epoch = {"epoch_id": f"le-{secrets.token_hex(8)}", "started_at": isoformat(),
                 "profile_fingerprint": profile, "quote_asset": self.settings.risk.quote_asset,
                 "initial_free_quote": format(quote, "f"), "initial_equity_snapshot": None,
                 "status": "active"}
        self.ledger.add_event("live.risk_epoch", None, epoch)
        return epoch

    def _effective_live_risk_epoch(self) -> dict[str, Any] | None:
        """Resolve the newest state per epoch, then select the newest epoch state."""
        latest_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
        for sequence, event in enumerate(self.ledger.events_by_kind("live.risk_epoch")):
            epoch_id = event.get("epoch_id")
            if not isinstance(epoch_id, str) or not epoch_id:
                continue
            latest_by_id[epoch_id] = (sequence, dict(event))
        if not latest_by_id:
            return None
        return max(latest_by_id.values(), key=lambda item: item[0])[1]

    @staticmethod
    def _epoch_blocks_new_live_entry(epoch: Mapping[str, Any] | None) -> bool:
        if not isinstance(epoch, Mapping):
            return False
        return str(epoch.get("status", "")).upper() in {
            "ACTIVE", "RECONCILE", "PNL_INCOMPLETE", "INCOMPLETE",
        }

    def _mark_live_epoch_reconcile(self, reason: str) -> None:
        current = self._effective_live_risk_epoch()
        if not isinstance(current, Mapping) or str(current.get("status", "")).upper() != "ACTIVE":
            return
        self.ledger.add_event("live.risk_epoch", None, {
            **dict(current), "status": "RECONCILE", "reconcile_reason": bounded_text(reason, "reason", maximum=160),
        })

    def _live_session_accounting(self) -> tuple[bool, Decimal, Decimal, str | None]:
        epoch = self._effective_live_risk_epoch()
        profile = self.live_executor.execution_profile_fingerprint()
        if (not isinstance(epoch, Mapping) or str(epoch.get("status", "")).upper() != "ACTIVE"
                or epoch.get("profile_fingerprint") != profile):
            return False, Decimal("0"), Decimal("0"), "RISKPILOT_SESSION_PNL_INCOMPLETE"
        # FIFO lots carry fee-inclusive unit cost; BUY fees are part of cost
        # basis, while SELL fees are charged directly to realized PnL.
        lots: list[list[Decimal]] = []
        daily_loss = Decimal("0")
        weekly_loss = Decimal("0")
        now = utcnow()
        day = now.date()
        week = day - timedelta(days=day.weekday())
        for fill in self.ledger.events_by_kind("live.risk_fill"):
            # Historical fills belong to their original epoch.  They remain
            # visible, but must never be imported into a new clean epoch.
            if fill.get("epoch_id") != epoch.get("epoch_id"):
                continue
            try:
                qty = Decimal(str(fill["quantity"])); price = Decimal(str(fill["price"]))
                fee = Decimal(str(fill["fee_quote"]))
                when = parse_time(fill.get("executed_at"))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return False, Decimal("0"), Decimal("0"), "RISKPILOT_SESSION_PNL_INCOMPLETE"
            if qty <= 0 or price <= 0 or fee < 0 or not when:
                return False, Decimal("0"), Decimal("0"), "RISKPILOT_SESSION_PNL_INCOMPLETE"
            if fill.get("side") == "BUY":
                lots.append([qty, price + (fee / qty)])
                continue
            if fill.get("side") != "SELL":
                return False, Decimal("0"), Decimal("0"), "RISKPILOT_SESSION_PNL_INCOMPLETE"
            remaining = qty; pnl = -fee
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                used = min(remaining, lot_qty)
                pnl += used * (price - lot_price)
                remaining -= used; lot_qty -= used
                if lot_qty == 0: lots.pop(0)
                else: lots[0][0] = lot_qty
            if remaining > 0:
                return False, Decimal("0"), Decimal("0"), "RISKPILOT_SESSION_PNL_INCOMPLETE"
            if when.date() == day and pnl < 0: daily_loss += -pnl
            if when.date() >= week and pnl < 0: weekly_loss += -pnl
        return True, daily_loss, weekly_loss, None

    def verify_live_trade_permission(self, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Explicitly attest Spot trade permission with one order-test call."""
        if not operator_confirmed:
            raise SecurityError("Spot trade permission attestation requires explicit operator confirmation")
        symbols = tuple(self.settings.live.allowed_symbols)
        if not symbols:
            raise SecurityError("no LIVE Spot symbol is allowlisted")
        account = self.live_executor.read_spot_account()
        if account.get("account_type") != "SPOT" or account.get("can_trade") is not True:
            raise SecurityError("authenticated Spot account metadata does not prove trade eligibility")
        symbol = symbols[0]
        exchange = validate_spot_symbol(self.settings, symbol, live=True)
        market = fetch_spot_snapshot(self.settings, symbol)
        result = self.live_executor.attest_spot_trade_permission(symbol, exchange, market)
        if result.get("classification") == "SUCCESS":
            verified_at = utcnow()
            proof = {"result": "verified", "verified_at": isoformat(verified_at),
                     "expires_at": isoformat(verified_at + timedelta(seconds=self.live_executor._PERMISSION_ATTESTATION_TTL_SECONDS)),
                     "backend": self.settings.codex.mcp_server,
                     "profile_fingerprint": self.live_executor.execution_profile_fingerprint(),
                     "delegated_operation": "spot.orderTest",
                     "schema_fingerprint": result["schema_fingerprint"]}
            self.ledger.add_event("live.trade_permission_attestation", None, proof)
            result["proof"] = {"result": proof["result"], "verified_at": proof["verified_at"], "expires_at": proof["expires_at"]}
        return result

    def discover_live_trade_history_tool(self, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Discover, but never invoke, the authenticated Spot fill-history capability."""
        if not operator_confirmed:
            raise SecurityError("trade-history discovery requires explicit operator confirmation")
        candidates = self.live_executor.discover_spot_trade_history_tools()
        return {"candidates": candidates, "verified": len(candidates) == 1,
                "reason": (None if len(candidates) == 1 else "ambiguous_or_missing")}

    def discover_live_execution_read_capabilities(self, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Discover read capabilities only; never invoke an execution/read tool."""
        if not operator_confirmed:
            raise SecurityError("execution read-capability discovery requires explicit operator confirmation")
        result = self.live_executor.discover_live_execution_read_capabilities(operator_confirmed=True)
        self.ledger.add_event("live.execution_read_capability_discovery", None, {
            "individual_order_status_count": result.get("individual_order_status_count", 0),
            "order_list_status_count": result.get("order_list_status_count", 0),
            "trade_fill_history_count": result.get("trade_fill_history_count", 0),
            "individual_order_status": result.get("individual_order_status"),
            "order_list_status": result.get("order_list_status"),
            "trade_fill_history": result.get("trade_fill_history"),
            "tools_list_calls": result.get("tools_list_calls", 0),
            "catalog_search_calls": result.get("catalog_search_calls", 0),
        })
        return result

    def mark_live_execution_unreconcilable(
        self, proposal_id: str, *, operator_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Locally close the accounting path when no safe read capability exists."""
        if not operator_confirmed:
            raise SecurityError("marking LIVE execution unreconcilable requires explicit operator confirmation")
        proposal = self.ledger.get_proposal(validate_simple_id(proposal_id, "proposal_id"), include_private=True)
        if proposal.get("mode") != "live":
            raise SecurityError("unreconcilable execution requires a LIVE proposal")
        if proposal.get("execution_status") not in {"EXEC_STARTED", "EXECUTING"}:
            raise SecurityError("LIVE execution is not an unresolved submission")
        evidence = [row for row in self.ledger.events_by_kind("live.execution_evidence")
                    if row.get("proposal_id") == proposal["id"]]
        if not evidence:
            raise SecurityError("LIVE execution evidence is unavailable")
        latest = evidence[-1]
        entry = latest.get("entry") or {}
        entry_status = str(entry.get("status", "")).upper()
        try:
            executed_qty = Decimal(str(entry.get("executed_qty", "0")))
        except (TypeError, ValueError, ArithmeticError):
            raise SecurityError("LIVE execution evidence has invalid quantity")
        if str(latest.get("phase", "")).upper() == "FILLED" or (entry_status == "FILLED" and executed_qty > 0):
            raise SecurityError("verified FILLED evidence must be reconciled, not marked unreconcilable")
        if entry_status not in {"NEW", "PENDING_NEW", "PARTIALLY_FILLED"}:
            raise SecurityError("LIVE execution evidence is not an unresolved parent order")
        if not isinstance(entry.get("order_id"), int) or entry["order_id"] <= 0:
            raise SecurityError("LIVE execution evidence has no delegated orderId")
        if not isinstance(entry.get("order_list_id"), int) or entry["order_list_id"] <= 0:
            raise SecurityError("LIVE execution evidence has no delegated orderListId")
        if any(row.get("proposal_id") == proposal["id"] for row in self.ledger.events_by_kind("live.risk_fill")):
            raise SecurityError("existing LIVE risk fill must be reconciled, not marked unreconcilable")
        protection = latest.get("protection")
        if (not isinstance(protection, list) or not protection
                or not all(isinstance(row, Mapping)
                           and isinstance(row.get("order_id", row.get("orderId")), int)
                           and row.get("order_id", row.get("orderId")) > 0
                           for row in protection)):
            raise SecurityError("protected OTOCO provenance is unavailable")
        audit = self.ledger.latest_event("live.execution_read_capability_discovery")
        counts = {key: audit.get(key) for key in ("individual_order_status_count", "order_list_status_count", "trade_fill_history_count")} if isinstance(audit, Mapping) else {}
        if counts != {"individual_order_status_count": 0, "order_list_status_count": 0, "trade_fill_history_count": 0}:
            raise SecurityError("a compatible LIVE execution read capability is available; reconciliation is required")
        epoch = self._effective_live_risk_epoch()
        if not isinstance(epoch, Mapping) or epoch.get("epoch_id") != latest.get("epoch_id"):
            raise SecurityError("LIVE execution epoch provenance is unavailable")
        reason = "ASYNC_FILL_PROVENANCE_UNRECOVERABLE_UPSTREAM_CAPABILITY"
        self.ledger.mark_live_execution_unreconcilable(proposal["id"], reason, {
            "execution_order_id": entry["order_id"], "order_list_id": entry["order_list_id"],
            "epoch_id": epoch["epoch_id"], "source": "local_capability_fallback",
        })
        self.ledger.add_event("live.risk_epoch", None, {
            **dict(epoch), "status": "RECONCILE", "reconcile_reason": reason,
            "realized_pnl_verified": False, "accounting_complete": False,
        })
        return {"status": "RECONCILE", "proposal_id": proposal["id"], "epoch_id": epoch["epoch_id"],
                "reason": reason, "accounting_status": "RECONCILE", "realized_pnl_verified": False,
                "accounting_complete": False}

    def verify_live_decimal_transport(self, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Explicitly attest fractional decimal transport with one orderTest call."""
        if not operator_confirmed:
            raise SecurityError("decimal transport attestation requires explicit operator confirmation")
        symbols = tuple(self.settings.live.allowed_symbols)
        if not symbols:
            raise SecurityError("no LIVE Spot symbol is allowlisted")
        symbol = "BTCUSDT" if "BTCUSDT" in symbols else symbols[0]
        exchange = validate_spot_symbol(self.settings, symbol, live=True)
        market = fetch_spot_snapshot(self.settings, symbol)
        result = self.live_executor.attest_decimal_transport(symbol, exchange, market)
        payload = result.get("payload")
        tested_quantity = Decimal(str(payload.get("quantity"))) if isinstance(payload, Mapping) and payload.get("quantity") is not None else Decimal("0")
        tested_price = Decimal(str(payload.get("price"))) if isinstance(payload, Mapping) and payload.get("price") is not None else Decimal("0")
        if (result.get("classification") == "SUCCESS" and tested_quantity >= self.live_executor._REMOTE_DECIMAL_FLOOR
                and tested_quantity != tested_quantity.to_integral_value()
                and tested_price > 0 and tested_price != tested_price.to_integral_value()):
            verified_at = utcnow()
            proof = {"result": "verified", "verified_at": isoformat(verified_at),
                     "expires_at": isoformat(verified_at + timedelta(seconds=self.live_executor._PERMISSION_ATTESTATION_TTL_SECONDS)),
                     "backend": self.settings.codex.mcp_server,
                     "profile_fingerprint": self.live_executor.execution_profile_fingerprint(),
                     "delegated_operation": "spot.orderTest",
                     "schema_fingerprint": result["schema_fingerprint"],
                     "symbol": symbol, "tested_fields": ["quantity", "price"],
                     "wire_mode": "fixed-point-json-number",
                     "minimum_verified_fractional_number": format(tested_quantity, "f"),
                     "classification": "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG",
                     "scope": "bounded_decimal_domain"}
            self.ledger.add_event("live.decimal_transport_attestation", None, proof)
            result["proof"] = {"result": proof["result"], "verified_at": proof["verified_at"],
                               "expires_at": proof["expires_at"]}
        return result

    def diagnose_live_decimal_transport(self, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Run one explicit diagnostic orderTest without persisting readiness proof."""
        if not operator_confirmed:
            raise SecurityError("decimal transport diagnostic requires explicit operator confirmation")
        symbols = tuple(self.settings.live.allowed_symbols)
        if not symbols:
            raise SecurityError("no LIVE Spot symbol is allowlisted")
        symbol = "BTCUSDT" if "BTCUSDT" in symbols else symbols[0]
        exchange = validate_spot_symbol(self.settings, symbol, live=True)
        market = fetch_spot_snapshot(self.settings, symbol)
        result = self.live_executor.diagnose_decimal_transport(symbol, exchange, market)
        if result.get("classification") == "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG":
            verified_at = utcnow()
            proof = {"result": "verified", "verified_at": isoformat(verified_at),
                     "expires_at": isoformat(verified_at + timedelta(seconds=self.live_executor._PERMISSION_ATTESTATION_TTL_SECONDS)),
                     "backend": self.settings.codex.mcp_server,
                     "profile_fingerprint": self.live_executor.execution_profile_fingerprint(),
                     "delegated_operation": "spot.orderTest",
                     "schema_fingerprint": result["schema_fingerprint"],
                     "symbol": symbol, "tested_fields": ["quantity", "price"],
                     "wire_mode": "fixed-point-json-number",
                     "minimum_verified_fractional_number": result["requested_quantity"],
                     "classification": "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG",
                     "scope": "bounded_decimal_domain"}
            self.ledger.add_event("live.decimal_transport_attestation", None, proof)
            result["proof"] = {"result": proof["result"], "verified_at": proof["verified_at"],
                               "expires_at": proof["expires_at"]}
        return result

    def arm_live(self, minutes: int) -> dict[str, Any]:
        if not self.settings.live.enabled:
            raise SecurityError("enable live locally before arming")
        if not 1 <= minutes <= self.settings.risk.max_live_arm_minutes:
            raise SecurityError(
                f"arm duration must be between 1 and {self.settings.risk.max_live_arm_minutes} minutes"
            )
        ticket = self.ledger.latest_event("live.session_prepared")
        error = self._validate_prepared_live_session(ticket)
        if error:
            raise SecurityError(error)
        result = self.live_arm.arm(minutes).__dict__
        self.ledger.add_event("live.session_prepared_consumed", None,
                              {"prepared_at": ticket["prepared_at"], "consumed_at": isoformat()})
        self.ledger.add_event("admin.live_armed", None, {"minutes": minutes, "local_tty": True})
        return result

    def prepare_live_recovery_session(self, symbol: str, *, operator_confirmed: bool = False) -> dict[str, Any]:
        """Prepare a short-lived, read-only-verified EXIT_ONLY authorization."""
        if not operator_confirmed:
            raise SecurityError("LIVE recovery preparation requires explicit operator confirmation")
        symbol = symbol.upper()
        if symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("LIVE recovery symbol is not allowlisted")
        if self.live_executor.rate_limit_status().get("status") == "BLOCKED":
            return {"status": "RATE_LIMIT_BLOCKED", "scope": "EXIT_ONLY", "execution_ready": False}
        epoch = self._effective_live_risk_epoch()
        if not isinstance(epoch, Mapping) or str(epoch.get("status", "")).upper() != "RECONCILE":
            raise SecurityError("LIVE recovery requires a RECONCILE risk epoch")
        exchange = validate_spot_symbol(self.settings, symbol, live=True)
        account = self.live_executor.read_spot_account()
        orders = self.live_executor.read_open_spot_orders()
        grouped: dict[int, list[dict[str, Any]]] = {}
        for order in orders:
            if order.get("symbol") == symbol and isinstance(order.get("orderListId"), int) and order["orderListId"] > 0:
                grouped.setdefault(order["orderListId"], []).append(order)
        protected = False
        protected_details: dict[str, Any] = {}
        if len(grouped) == 1:
            protected_details = self.live_executor.validate_active_protective_oco(
                next(iter(grouped.values())), symbol, tick=Decimal(exchange["price_tick_size"])
            )
            protected = True
        if not protected:
            raise SecurityError("LIVE recovery requires an auditable active Spot protection list")
        permission_proof = self.ledger.latest_event("live.trade_permission_attestation")
        decimal_proof = self.ledger.latest_event("live.decimal_transport_attestation")
        schema_fingerprint = permission_proof.get("schema_fingerprint") if isinstance(permission_proof, Mapping) else None
        permission_valid = self.live_executor._valid_permission_attestation(permission_proof, schema_fingerprint)
        decimal_valid = self.live_executor._valid_decimal_transport_attestation(decimal_proof, schema_fingerprint)
        if not permission_valid or not decimal_valid:
            raise SecurityError("LIVE recovery permission or decimal proof is missing or expired")
        prepared_at = utcnow()
        ticket = {"result": "prepared", "prepared_at": isoformat(prepared_at),
                  "expires_at": isoformat(prepared_at + timedelta(minutes=5)),
                  "scope": "EXIT_ONLY", "target_symbol": symbol,
                  "allowed_actions": ["LIVE_CLOSE_ALL", "LIVE_EXIT_PERCENT", "PROTECTION_RECONCILE"],
                  "forbidden_actions": ["LIVE_BUY", "NEW_ENTRY", "INCREASE_EXPOSURE"],
                  "backend": self.settings.codex.mcp_server,
                  "profile_fingerprint": self.live_executor.execution_profile_fingerprint(),
                  "epoch_id": epoch.get("epoch_id"), "account_read_verified": bool(account),
                  "open_orders_read_verified": True, "protective_order_capability_verified": protected,
                  "order_list_id": protected_details.get("order_list_id"),
                  "protected_order_ids": sorted(row["orderId"] for row in next(iter(grouped.values()))),
                  "protected_quantity": format(protected_details.get("quantity", Decimal("0")), "f"),
                  "permission_proof_valid": permission_valid, "decimal_proof_valid": decimal_valid,
                  "schema_fingerprint": schema_fingerprint,
                  "symbol_exchange_flags_verified": True, "rate_limit_status": "CLEAR",
                  "final_blockers": []}
        self.ledger.add_event("live.recovery_session_prepared", None, ticket)
        return {"status": "PREPARED", "scope": "EXIT_ONLY", "target_symbol": symbol,
                "expires_at": ticket["expires_at"], "execution_ready": False}

    def _validate_exit_only_approval(self, proposal: Mapping[str, Any], arm: Any) -> None:
        """Validate recovery bindings locally before any risk-reducing write."""
        canonical = proposal.get("canonical") or {}
        ticket = self.ledger.latest_event("live.recovery_session_prepared")
        binding = getattr(arm, "binding", None) or {}
        epoch = self._effective_live_risk_epoch()
        if (not getattr(arm, "armed", False) or getattr(arm, "scope", "FULL") != "EXIT_ONLY"
                or not isinstance(ticket, Mapping) or ticket.get("result") != "prepared"
                or ticket.get("scope") != "EXIT_ONLY"
                or ticket.get("prepared_at") != binding.get("prepared_at")
                or ticket.get("target_symbol") != binding.get("target_symbol")
                or ticket.get("profile_fingerprint") != self.live_executor.execution_profile_fingerprint()
                or not isinstance(epoch, Mapping) or epoch.get("epoch_id") != ticket.get("epoch_id")
                or str(epoch.get("status", "")).upper() != "RECONCILE"):
            raise SecurityError("EXIT_ONLY recovery preparation is missing or no longer valid")
        try:
            if parse_time(ticket.get("expires_at")) <= utcnow():
                raise SecurityError("EXIT_ONLY recovery preparation has expired")
        except (TypeError, ValueError):
            raise SecurityError("EXIT_ONLY recovery preparation is invalid")
        if canonical.get("symbol") != ticket.get("target_symbol") or canonical.get("side") != "SELL":
            raise SecurityError("EXIT_ONLY recovery permits only the prepared-symbol SELL")
        if canonical.get("order_type") not in {"PARTIAL_EXIT", "CANCEL_OCO", "OCO_PROTECTION"}:
            raise SecurityError("EXIT_ONLY recovery action is not risk-reducing")
        if canonical.get("order_list_id") != ticket.get("order_list_id"):
            raise SecurityError("EXIT_ONLY recovery order list does not match prepared protection")
        if sorted(canonical.get("protected_order_ids", [])) != ticket.get("protected_order_ids"):
            raise SecurityError("EXIT_ONLY recovery protection provenance does not match")
        if canonical.get("order_type") == "PARTIAL_EXIT":
            sell = Decimal(str(canonical.get("sell_quantity", "0")))
            protected = Decimal(str(ticket.get("protected_quantity", "0")))
            if sell <= 0 or sell > protected:
                raise SecurityError("EXIT_ONLY recovery sell quantity exceeds verified protection")
            if Decimal(str(canonical.get("percentage", "0"))) == Decimal("100") and Decimal(str(canonical.get("remaining_quantity", "-1"))) != 0:
                raise SecurityError("EXIT_ONLY full exit must have zero remaining quantity")
            self.live_executor.validate_remote_decimal_domain({"quantity": sell})
        if ticket.get("permission_proof_valid") is not True or ticket.get("decimal_proof_valid") is not True:
            raise SecurityError("EXIT_ONLY recovery permission or decimal proof is missing")
        schema_fingerprint = ticket.get("schema_fingerprint")
        if (not self.live_executor._valid_permission_attestation(
                self.ledger.latest_event("live.trade_permission_attestation"), schema_fingerprint)
                or not self.live_executor._valid_decimal_transport_attestation(
                    self.ledger.latest_event("live.decimal_transport_attestation"), schema_fingerprint)):
            raise SecurityError("EXIT_ONLY recovery permission or decimal proof is stale")
        if self.live_executor.rate_limit_status().get("status") != "CLEAR":
            raise SecurityError("EXIT_ONLY recovery is blocked by the rate-limit circuit")

    def arm_live_recovery(self, minutes: int) -> dict[str, Any]:
        if not self.settings.live.enabled:
            raise SecurityError("enable live locally before arming recovery")
        if not 1 <= minutes <= self.settings.risk.max_live_arm_minutes:
            raise SecurityError(f"arm duration must be between 1 and {self.settings.risk.max_live_arm_minutes} minutes")
        ticket = self.ledger.latest_event("live.recovery_session_prepared")
        try:
            valid = (isinstance(ticket, Mapping) and ticket.get("result") == "prepared"
                     and ticket.get("scope") == "EXIT_ONLY"
                     and parse_time(ticket.get("expires_at")) > utcnow()
                     and ticket.get("backend") == self.settings.codex.mcp_server
                     and ticket.get("profile_fingerprint") == self.live_executor.execution_profile_fingerprint()
                     and ticket.get("account_read_verified") is True
                     and ticket.get("open_orders_read_verified") is True
                     and ticket.get("protective_order_capability_verified") is True
                     and ticket.get("symbol_exchange_flags_verified") is True
                     and ticket.get("rate_limit_status") == "CLEAR"
                     and self.live_executor.rate_limit_status().get("status") == "CLEAR")
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SecurityError("LIVE recovery preparation is missing, expired, or no longer valid; prepare-live-recovery-session again")
        result = self.live_arm.arm(
            minutes, scope="EXIT_ONLY",
            binding={"prepared_at": ticket["prepared_at"], "target_symbol": ticket["target_symbol"],
                     "profile_fingerprint": ticket["profile_fingerprint"]},
        ).__dict__
        self.ledger.add_event("live.recovery_session_prepared_consumed", None,
                              {"prepared_at": ticket["prepared_at"], "consumed_at": isoformat()})
        return result

    def _validate_prepared_live_session(self, ticket: Mapping[str, Any] | None) -> str | None:
        """Validate only local, fresh preflight evidence; never refresh MCP."""
        if not isinstance(ticket, Mapping) or ticket.get("result") != "prepared":
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        try:
            if parse_time(ticket.get("expires_at")) <= utcnow():
                return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        except (TypeError, ValueError):
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        if ticket.get("backend") != self.settings.codex.mcp_server or ticket.get("profile_fingerprint") != self.live_executor.execution_profile_fingerprint():
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        if ticket.get("target_symbol") not in self.settings.live.allowed_symbols:
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        required = ("account_read_verified", "open_orders_read_verified", "spot_trade_scope_verified",
                    "write_tool_discovered", "write_schema_verified", "decimal_transport_verified",
                    "protective_order_capability_verified", "symbol_exchange_flags_verified",
                    "live_limits_valid", "live_enabled")
        if any(ticket.get(name) is not True for name in required) or ticket.get("final_blockers") != ["live_armed"]:
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        if ticket.get("rate_limit_status") != "CLEAR" or self.live_executor.rate_limit_status()["status"] != "CLEAR":
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        schema_fp = ticket.get("schema_fingerprint")
        permission = self.ledger.latest_event("live.trade_permission_attestation")
        decimal = self.ledger.latest_event("live.decimal_transport_attestation")
        if not self.live_executor._valid_permission_attestation(permission, schema_fp):
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        if not self.live_executor._valid_decimal_transport_attestation(decimal, schema_fp):
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        consumed = self.ledger.latest_event("live.session_prepared_consumed")
        invalidated = self.ledger.latest_event("live.session_prepared_invalidated")
        if any(isinstance(event, Mapping) and event.get("prepared_at") == ticket.get("prepared_at")
               for event in (consumed, invalidated)):
            return "live session preparation is missing, expired, or no longer valid; run prepare-live-session again"
        return None

    def invalidate_prepared_live_session(self, reason: str) -> None:
        ticket = self.ledger.latest_event("live.session_prepared")
        if isinstance(ticket, Mapping) and isinstance(ticket.get("prepared_at"), str):
            self.ledger.add_event("live.session_prepared_invalidated", None,
                                  {"prepared_at": ticket["prepared_at"], "reason": reason})

    def policy_explain(
        self, *, mode: str | None = None, symbol: str | None = None,
        quote_amount: Decimal | None = None,
        risk_at_stop: Decimal | None = None,
    ) -> dict[str, Any]:
        """Explain current policy state without creating a proposal or order."""
        selected_mode = mode or self.settings.mode
        if selected_mode == "paper":
            context, symbols = self._paper_policy_context()
            selected_symbol = (symbol or self.settings.market.symbols[0]).upper()
            if not selected_symbol.endswith(self.settings.risk.quote_asset):
                selected_symbol += self.settings.risk.quote_asset
            if selected_symbol not in self.settings.market.symbols:
                raise PolicyError("policy explanation symbol is not allowlisted")
            projected_aggregate = (
                context.usage.aggregate_open_risk + risk_at_stop
                if risk_at_stop is not None else None
            )
            evaluation = evaluate_entry(
                context,
                requested_notional=quote_amount,
                projected_exposure=(context.usage.open_exposure + quote_amount
                                    if quote_amount is not None else None),
                projected_position_risk=risk_at_stop,
                projected_aggregate_risk=projected_aggregate,
                resulting_economic_positions=(
                    context.usage.economic_positions
                    if selected_symbol in symbols
                    else context.usage.economic_positions + 1
                ) if quote_amount is not None else None,
            )
            snapshot = build_policy_snapshot(
                self.settings, "paper", context, evaluation
            )
        elif selected_mode == "live":
            selected_symbol = (symbol or self.settings.market.symbols[0]).upper()
            if not selected_symbol.endswith(self.settings.risk.quote_asset):
                selected_symbol += self.settings.risk.quote_asset
            amount = (
                quote_amount if quote_amount is not None
                else self.settings.risk.default_order_size_usdt
            )
            try:
                projection = self._validate_live_entry_limits(
                    selected_symbol, amount, Decimal("0"),
                    risk_at_stop or Decimal("0"), Decimal("1"), explain=True,
                )
            except Exception as exc:
                return {"schema": "riskpilot.policy-explain.v1", "read_only": True,
                        "mode": "live", "accepted": False,
                        "reasons": [str(exc)], "snapshot_available": False}
            snapshot = projection["policy_snapshot"]
        else:
            raise PolicyError("policy explanation mode must be paper or live")
        evaluation = snapshot["evaluation"]
        return {
            "schema": "riskpilot.policy-explain.v1", "read_only": True,
            "mode": selected_mode,
            "policy": snapshot["policy_config"]["sizing_policy"],
            "equity_snapshot": snapshot["equity_snapshot"],
            "hard_limits": snapshot["policy_config"]["hard_limits"],
            "effective_limits": snapshot["effective_limits"],
            "usage": snapshot["usage"],
            "remaining": {"exposure": evaluation["remaining_exposure"],
                          "position": evaluation["remaining_position_capacity"],
                          "aggregate_risk": evaluation["remaining_aggregate_risk"],
                          "buying_power": evaluation["remaining_buying_power"]},
            "accepted": evaluation["accepted"],
            "reason_codes": evaluation.get("reason_codes", []),
            "reasons": evaluation["reasons"],
            "hypothetical_request": {
                "symbol": selected_symbol,
                "quote_amount": str(quote_amount) if quote_amount is not None else None,
                "risk_at_stop": str(risk_at_stop) if risk_at_stop is not None else None,
            },
        }

    def status(self) -> dict[str, Any]:
        arm_status = self.live_arm.status()
        codex_status = self.agent_os.status()
        skill_path = self.settings.workspace / "skills" / "binance-spotguard" / "SKILL.md"
        return {
            "version": __version__,
            "mode": self.settings.mode,
            "execution_capability": "paper-only",
            "manual_approval_required": self.settings.security.require_manual_approval,
            "live_arm": arm_status.__dict__,
            "symbols": list(self.settings.market.symbols),
            "scheduled_proposal_mode": self.settings.scheduled_proposal_mode,
            "limits": {
                "default_order_size_usdt": str(self.settings.risk.default_quote_amount),
                "sizing_policy": self.settings.sizing_policy.to_dict(),
                "paper": {
                    "max_quote_per_entry_usdt": str(self.settings.paper.max_quote_per_entry_usdt),
                    "max_open_exposure_usdt": str(self.settings.paper.max_open_exposure_usdt),
                },
                "live": {
                    "max_quote_per_entry_usdt": str(self.settings.live.max_quote_per_entry_usdt),
                    "max_open_exposure_usdt": str(self.settings.live.max_open_exposure_usdt),
                    "entry_slippage_cap_pct": str(self.settings.live.entry_slippage_cap_pct),
                },
            },
            "database": str(self.settings.database_path),
            "ledger": self.ledger.counts(),
            "openclaw": {
                "available": openclaw_available(self.settings),
                "skill_installed": skill_path.exists(),
            },
            "codex_agent_os": codex_status,
            "isolation": {
                "technocore_modified": False,
                "credential_source": "ChatGPT and Binance OAuth managed by Codex CLI",
                "codex_invocation": "on-demand only",
            },
        }

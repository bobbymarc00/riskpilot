from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import time
from typing import Any

from . import __version__
from .presentation import detect_locale, error_text, localized, number, render, translate
from .codex_bridge import CodexAgentOSBridge
from .config import Settings, openclaw_available
from .db import Ledger
from .indicators import analyze
from .live_execution import LiveExecutionAdapter
from .market import Kline, MarketError, SymbolValidationError, fetch_1m_candles_since, fetch_klines, fetch_spot_snapshot, floor_to_step, load_fixture, scaled_synthetic_klines, synthetic_bullish_klines, validate_spot_symbol
from .policy import PolicyError, build_proposal, entry_policy_terms, execution_intent, validate_claim
from .paper import build_fill_risk, exit_values, validate_long_bracket
from .security import ApprovalSigner, LiveArm, SecurityError
from .strategy import Signal, evaluate
from .score_engine import MarketScore, SCORE_ENGINE_VERSION, score_market, score_snapshot
from .telegram import OpenClawMessenger, TelegramError, candidate_message, paper_close_message, proposal_message
from .util import bounded_text, canonical_json, isoformat, parse_time, utcnow, validate_simple_id


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
            self._ensure_paper_entry_available(scored.symbol, amount, read_only=True)
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
            )
            existing = [row for row in self.ledger.list_paper_positions(True)
                        if row["symbol"] == scored.symbol]
            return {"eligible": True, "blocking_reason": None,
                    "hypothetical_only": True, "scale_in": bool(existing),
                    "projected_exposure_usdt": str(sum((Decimal(row["quote_spent"]) for row in self.ledger.list_paper_positions(True)), Decimal("0")) + spend),
                    "projected_position_risk_usdt": str(projection["new_position_risk"]),
                    "projected_aggregate_risk_usdt": str(projection["projected_aggregate_risk"]),
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
                               ask: Decimal | None = None, reward_risk: Decimal | None = None) -> dict[str, Decimal | int]:
        rows = [row for row in self.ledger.list_paper_positions(open_only=True) if row["status"] in {"OPEN", "CLOSING"}]
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
        limit = self.settings.paper.max_aggregate_risk_usdt
        if new_position_risk > self.settings.paper.max_risk_per_position_usdt or projected_aggregate > limit:
            raise PolicyError("PAPER risk rejected: current aggregate risk "
                f"{current_aggregate:f} USDT; new economic-position risk {new_position_risk:f} USDT; "
                f"projected aggregate risk {projected_aggregate:f} USDT; configured aggregate limit {limit:f} USDT; "
                f"configured per-position limit {self.settings.paper.max_risk_per_position_usdt:f} USDT")
        return result

    def _ensure_paper_entry_available(self, symbol: str | None = None,
                                      requested: Decimal | None = None,
                                      *, read_only: bool = False) -> None:
        balance = self.ledger.paper_balance()
        positions = self.ledger.list_paper_positions(open_only=True)
        active_tranches = int(balance.get("active_tranches", balance.get("open_positions", 0)))
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
        if symbol is None and len({row["symbol"] for row in positions}) >= self.settings.paper.max_economic_positions:
            raise PolicyError(f"economic PAPER position/distinct-symbol limit reached ({self.settings.paper.max_economic_positions})")
        if symbol and symbol not in {row["symbol"] for row in positions} and len({row["symbol"] for row in positions}) >= self.settings.paper.max_economic_positions:
            raise PolicyError(f"economic PAPER position/distinct-symbol limit reached ({self.settings.paper.max_economic_positions})")
        exposure = sum((Decimal(row["quote_spent"]) for row in positions), Decimal("0"))
        aggregate_risk = sum((Decimal(row["risk_amount"]) for row in positions), Decimal("0"))
        if requested is not None and exposure + requested > self.settings.paper.max_open_exposure_usdt:
            raise PolicyError(f"PAPER exposure limit reached: projected exposure would exceed {self.settings.paper.max_open_exposure_usdt} USDT")
        if aggregate_risk >= self.settings.paper.max_aggregate_risk_usdt:
            raise PolicyError("paper aggregate open risk limit is exhausted")
        day = utcnow().date().isoformat()
        if self.ledger.daily_paper_realized_loss(day) >= self.settings.paper.daily_realized_loss_cap_usdt:
            raise PolicyError("paper daily realized loss cap is exhausted")
        if self.ledger.successful_paper_entries(day) >= self.settings.paper.max_successful_entries_per_utc_day:
            raise PolicyError(f"daily PAPER entry quota reached ({self.settings.paper.max_successful_entries_per_utc_day} successful BUY fills per UTC day)")
        free = Decimal(balance["free_usdt"])
        if free < self.settings.risk.min_quote_amount or (requested is not None and requested > free):
            raise PolicyError("insufficient free paper USDT")

    def scan(
        self,
        symbols: list[str] | None = None,
        notify: bool = False,
        fixture: Path | None = None,
        synthetic: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        paper_monitor = self.monitor_paper_positions(notify=notify, dry_run=dry_run)
        requested = symbols or list(self.settings.market.symbols)
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
                        "agent_os_confirmation_open_time": klines[-1].open_time})
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
        raw_symbol = symbol.upper()
        symbol = raw_symbol if raw_symbol.endswith(self.settings.risk.quote_asset) else raw_symbol + self.settings.risk.quote_asset
        if symbol not in self.settings.market.symbols:
            raise SpotGuardError(f"symbol is not allowlisted: {symbol}")
        if live:
            if symbol not in self.settings.live.allowed_symbols:
                raise SecurityError("symbol is not enabled for live Spot intent")
            if not self.settings.live.enabled:
                raise SecurityError("live trading is disabled")
            if not self.live_arm.status().armed:
                raise SecurityError("live trading is not armed on the VPS")
            if quote_amount <= 0:
                raise PolicyError("LIVE quote amount must be positive")
            if quote_amount > self.settings.live.max_live_trade_usdt:
                raise PolicyError(f"requested amount {quote_amount} USDT exceeds configured maximum {self.settings.live.max_quote_per_entry_usdt} USDT")
            readiness = self.live_status()
            if not readiness["execution_ready"]:
                raise SecurityError("live execution readiness checks have not all passed")
            proposal_mode, source, ttl = "live", "manual-live", self.settings.live.approval_ttl_seconds
        else:
            if self.settings.mode != "paper":
                raise SecurityError("manual paper tests are only available when mode == paper")
            if quote_amount <= 0:
                raise PolicyError("PAPER quote amount must be positive")
            if quote_amount > self.settings.paper.max_quote_per_entry_usdt:
                raise PolicyError(f"requested amount {quote_amount} USDT exceeds configured maximum {self.settings.paper.max_quote_per_entry_usdt} USDT")
            proposal_mode, source, ttl = "paper", "manual-paper-test", None
            self._ensure_paper_entry_available(symbol, quote_amount)
        market = fetch_spot_snapshot(self.settings, symbol)
        quantity = floor_to_step(quote_amount / market.ask, market.step_size)
        spend = quantity * market.ask
        if quantity <= 0 or spend < market.min_notional:
            minimum_units = (market.min_notional / market.ask / market.step_size).to_integral_value(rounding=ROUND_CEILING)
            minimum_quote = minimum_units * market.step_size * market.ask
            raise PolicyError(f"requested {quote_amount} USDT is below the current minimum notional request of {minimum_quote:f} USDT after downward quantity-step rounding; amount was not increased")
        klines = fetch_klines(self.settings, symbol)
        snapshot = analyze(klines)
        fingerprint = hashlib.sha256(f"{source}:{symbol}:{time.time_ns()}:{secrets.token_hex(4)}".encode()).hexdigest()
        signal = Signal(candidate_id=f"c-{fingerprint[:12]}", fingerprint=fingerprint, symbol=symbol, interval=self.settings.market.interval, side="BUY", score=snapshot.score, price=float(market.ask), candle_close_time=klines[-1].close_time, reasons=("manual test intent; signal score explicitly bypassed",), metrics={**snapshot.to_dict(), "agent_os_confirmed": False, "manual_signal_bypass": True, "source": source, "exchange_min_notional": str(market.min_notional), "quantity_step": str(market.step_size)})
        candidate, _ = self.ledger.create_candidate(signal, self.settings.market.candidate_ttl_minutes)
        values = build_proposal(self.settings, candidate, market.bid, market.ask, quote_amount, "Manual intent; signal-score requirement bypassed only.", proposal_mode=proposal_mode, source=source, ttl_seconds=ttl)
        if not live:
            reference_plan = build_fill_risk(self.settings, values, market.ask, quantity)
            projection = self._paper_risk_projection(symbol, market.ask, reference_plan, bid=market.bid, ask=market.ask, reward_risk=Decimal(values["reward_risk"]))
            values["canonical"].update({
                "projected_risk_at_stop": str(projection["new_position_risk"]),
                "projected_aggregate_risk": str(projection["projected_aggregate_risk"]),
                "reference_quantity": reference_plan["net_base_quantity"],
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

        if live:
            limit_price = floor_to_step(Decimal(values["entry_reference"]), market.price_tick_size)
            values["entry_reference"] = str(limit_price)
            values["canonical"]["entry_reference"] = str(limit_price)
            values["canonical"]["entry_limit_price"] = str(limit_price)
            live_quantity = floor_to_step(quote_amount / limit_price, market.step_size)
            if live_quantity <= 0 or live_quantity * limit_price < market.min_notional:
                raise PolicyError(f"requested {quote_amount} USDT is below Binance minimum notional after rounding; amount will not be increased")
            risk_at_stop = live_quantity * (limit_price - Decimal(values["stop_reference"]))
            if risk_at_stop > self.settings.live.max_risk_per_trade_usdt:
                raise PolicyError(f"LIVE risk at stop exceeds {self.settings.live.max_risk_per_position_usdt} USDT")
            values["canonical"].update({"quantity": str(live_quantity),
                "risk_at_stop": str(risk_at_stop), "projected_total_exposure": str(quote_amount),
                "projected_free_balance": "UNVERIFIED", "fee_estimate": str(quote_amount * self.settings.risk.paper_fee_pct / Decimal("100")),
                "protection": "OPO_WITH_PENDING_SELL_OCO_REQUIRED", "execution_ready_at_creation": readiness["execution_ready"]})
            values["canonical_json"] = canonical_json(values["canonical"])
        token = self.signer.approval_token(values["canonical_json"])
        code = self.signer.paper_confirmation_code(values["canonical_json"]) if values["mode"] == "paper" else None
        values["locale"] = self.locale
        proposal = self.ledger.create_proposal(values, self.signer.token_hash(token), self.settings.risk.max_active_proposals, self.signer.token_hash(code) if code else None)
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
    ) -> dict[str, Any]:
        candidate = self.ledger.get_candidate(candidate_id)
        if candidate["status"] != "ACTIVE":
            raise SpotGuardError(f"candidate must be ACTIVE, not {candidate['status']}")
        market_review = self.agent_os.review_market(candidate["symbol"])
        self._record_agent_os_read(candidate_id, "candidate-review", market_review)
        proposal = self.create_proposal(
            candidate_id,
            Decimal(market_review["best_bid"]),
            Decimal(market_review["best_ask"]),
            None,
            market_review["review"],
            notify=notify,
            dry_run=dry_run,
        )
        return {"market_review": market_review, **proposal}

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
        if proposal_mode == "paper":
            self._ensure_paper_entry_available(candidate_preview["symbol"], quote_amount or self.settings.risk.default_quote_amount)
        elif not self.live_status()["execution_ready"]:
            raise SecurityError("scheduled LIVE proposal mode is not execution-ready")
        if self.ledger.active_proposal_count() >= self.settings.risk.max_active_proposals:
            raise SpotGuardError("maximum active proposal count has been reached")
        candidate = self.ledger.get_candidate(candidate_id)
        if not candidate["metrics"].get("agent_os_confirmed") and not candidate["metrics"].get("paper_demo"):
            raise SpotGuardError("candidate lacks successful Agent OS confirmation")
        proposal_values = build_proposal(
            self.settings,
            candidate,
            bid_reference,
            ask_reference,
            quote_amount or self.settings.risk.default_quote_amount,
            rationale,
            proposal_mode=proposal_mode,
            source="deterministic-signal",
        )
        token = self.signer.approval_token(proposal_values["canonical_json"])
        code = self.signer.paper_confirmation_code(proposal_values["canonical_json"]) if proposal_values["mode"] == "paper" else None
        proposal_values["locale"] = self.locale
        proposal = self.ledger.create_proposal(
            proposal_values,
            self.signer.token_hash(token),
            self.settings.risk.max_active_proposals,
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
            raise SecurityError("paper proposal is not pending or has expired")
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
        validate_claim(self.settings, proposal, daily)
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
        effective_chat = chat_id or self.settings.telegram.chat_id
        if effective_chat != self.settings.telegram.chat_id or proposal["canonical"].get("approval_chat_id") != effective_chat or proposal["canonical"].get("approval_owner_id") != sender_id:
            raise SecurityError("rejection ownership or chat binding does not match")
        if not self.signer.verify_approval(proposal["canonical_json"], token):
            raise SecurityError("approval token does not match the proposal")
        return self.ledger.reject_proposal(proposal_id, self.signer.token_hash(token), actor)

    @localized
    def claim(self, proposal_id: str, token: str, sender_id: str, chat_id: str | None = None) -> dict[str, Any]:
        self.ledger.expire_stale_active_proposals()
        actor = self._validate_owner(sender_id)
        proposal = self.ledger.get_proposal(proposal_id)
        effective_chat = chat_id or self.settings.telegram.chat_id
        if effective_chat != self.settings.telegram.chat_id or proposal["canonical"].get("approval_chat_id") != effective_chat or proposal["canonical"].get("approval_owner_id") != sender_id:
            raise SecurityError("approval ownership or chat binding does not match")
        if not self.signer.verify_approval(proposal["canonical_json"], token):
            raise SecurityError("approval token does not match the proposal")
        self._enforce_paper_daily_entry_quota(proposal)
        daily = self.ledger.daily_committed_quote(utcnow().date().isoformat())
        validate_claim(self.settings, proposal, daily)
        if proposal["mode"] == "live":
            readiness = self.live_status(check_symbols=True)
            if not readiness["execution_ready"]:
                raise SecurityError("live approval fails closed; readiness blockers: " + ", ".join(readiness["blockers"]))
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
            str(self.settings.risk.max_daily_quote),
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
                self.ledger.fail_execution(proposal_id, lease_hash, reason)
            raise

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
                self.ledger.fail_execution(proposal_id, lease_hash, "fresh price drift exceeds configured limit; requote required")
                raise PolicyError("fresh price drift exceeds configured limit; proposal rejected, request a requote")
        requested = Decimal(proposal["quote_amount"])
        gross = floor_to_step(requested / fill_price, step)
        if gross <= 0 or gross * fill_price < min_notional:
            self.ledger.fail_execution(proposal_id, lease_hash, "fresh exchange filters fail minimum notional or quantity step")
            raise PolicyError("fresh exchange filters reject the fill; proposal rejected, request a requote")
        plan = build_fill_risk(self.settings, proposal, fill_price, gross)
        projection = self._paper_risk_projection(proposal["symbol"], fill_price, plan,
            bid=fill_price if is_demo else market.bid, ask=fill_price if is_demo else market.ask,
            reward_risk=Decimal(proposal["reward_risk"]))
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
            self.settings.paper.max_open_exposure_usdt, self.settings.paper.max_risk_per_position_usdt,
            self.settings.paper.max_aggregate_risk_usdt,
            self.settings.risk.paper_fee_pct / Decimal("100"),
            self.settings.paper.slippage_pct / Decimal("100"),
            fill_price if is_demo else market.bid, fill_price if is_demo else market.ask)
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
    ) -> dict[str, Any]:
        if self.settings.mode != "live":
            raise SecurityError("live completion is unavailable while mode is paper")
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
        return self.ledger.finish_execution(
            proposal_id,
            lease_hash,
            final_status,
            order_id,
            normalized_status,
            summary,
        )

    def fail_execution(self, proposal_id: str, lease: str, reason: str) -> dict[str, Any]:
        _, lease_hash = self._verify_execution_lease(proposal_id, lease)
        sanitized = bounded_text(reason, "reason", maximum=200)
        return self.ledger.fail_execution(proposal_id, lease_hash, sanitized)

    def uncertain_execution(self, proposal_id: str, lease: str, reason: str) -> dict[str, Any]:
        if self.settings.mode != "live":
            raise SecurityError("uncertain execution reconciliation is only used in live mode")
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
        return self.ledger.finish_execution(
            proposal_id,
            lease_hash,
            "RECONCILE",
            f"unknown:{proposal_id}",
            "UNKNOWN",
            summary,
        )

    def live_status(self, check_symbols: bool = False) -> dict[str, Any]:
        arm = self.live_arm.status()
        agent = self.agent_os.status()
        symbol_checks: list[dict[str, Any]] = []
        if check_symbols:
            for symbol in self.settings.live.allowed_symbols:
                try:
                    item = validate_spot_symbol(self.settings, symbol)
                    item["protected_live_supported"] = bool(item.get("oto_allowed") and item.get("opo_allowed") and item.get("oco_allowed") and Decimal(item.get("price_tick_size", "0")) > 0 and item.get("percent_price_filter") and item.get("max_num_orders", 0) > 0 and item.get("max_num_algo_orders", 0) > 0 and item.get("max_num_order_lists", 0) > 0)
                except Exception as exc:
                    item = {"symbol": symbol, "protected_live_supported": False, "reason": str(exc)}
                symbol_checks.append(item)
        flags_ok = bool(symbol_checks) and all(item["protected_live_supported"] for item in symbol_checks)
        readiness = self.live_executor.readiness(
            connected=bool(agent.get("currently_usable")), armed=arm.armed,
            symbol_flags_verified=flags_ok)
        result = readiness.to_dict()
        minimum_profile_balance = self.settings.live.max_quote_per_entry_usdt + self.settings.live.min_free_reserve_usdt
        result.update({"arm_expires_at": arm.expires_at,
            "max_quote_per_entry_usdt": str(self.settings.live.max_quote_per_entry_usdt),
            "max_active_tranches": self.settings.live.max_active_tranches,
            "max_economic_positions": self.settings.live.max_economic_positions,
            "max_open_exposure_usdt": str(self.settings.live.max_open_exposure_usdt),
            "min_free_reserve_usdt": str(self.settings.live.min_free_reserve_usdt),
            "max_risk_per_position_usdt": str(self.settings.live.max_risk_per_position_usdt),
            "max_aggregate_risk_usdt": str(self.settings.live.max_aggregate_risk_usdt),
            "daily_realized_loss_cap_usdt": str(self.settings.live.daily_realized_loss_cap_usdt),
            "max_successful_entries_per_utc_day": self.settings.live.max_successful_entries_per_utc_day,
            "minimum_profile_balance_before_fees_usdt": str(minimum_profile_balance),
            "balance_profile_note": "A 28 USDT account cannot support a 100 USDT entry plus the 8 USDT reserve; account balance is not queried by this read-only report.",
            "max_pending_proposals": self.settings.live.max_pending_proposals,
            "allowed_symbols": list(self.settings.live.allowed_symbols), "symbol_checks": symbol_checks})
        return result

    def arm_live(self, minutes: int) -> dict[str, Any]:
        if not self.settings.live.enabled:
            raise SecurityError("enable live locally before arming")
        if not 1 <= minutes <= self.settings.risk.max_live_arm_minutes:
            raise SecurityError(
                f"arm duration must be between 1 and {self.settings.risk.max_live_arm_minutes} minutes"
            )
        readiness = self.live_status(check_symbols=True)
        blockers = [item for item in readiness["blockers"] if item != "live_armed"]
        if blockers:
            raise SecurityError("live cannot be armed; readiness blockers: " + ", ".join(blockers))
        result = self.live_arm.arm(minutes).__dict__
        self.ledger.add_event("admin.live_armed", None, {"minutes": minutes, "local_tty": True})
        return result

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
                "paper": {
                    "max_quote_per_entry_usdt": str(self.settings.paper.max_quote_per_entry_usdt),
                    "max_open_exposure_usdt": str(self.settings.paper.max_open_exposure_usdt),
                },
                "live": {
                    "max_quote_per_entry_usdt": str(self.settings.live.max_quote_per_entry_usdt),
                    "max_open_exposure_usdt": str(self.settings.live.max_open_exposure_usdt),
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

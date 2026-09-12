from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .codex_bridge import CodexBridgeError
from .config import ConfigError, default_config_path, initialize_config, load_settings
from .db import LedgerError
from .market import MarketError
from .intent import normalize_paper_intent, normalize_trade_intent
from .presentation import detect_locale, error_text, render, vocabulary
from .policy import PolicyError
from .security import SecurityError
from .service import SpotGuard, SpotGuardError
from .telegram import TelegramError
from .util import decimal_value, json_default, pretty_json, utcnow


CALLBACK_RE = re.compile(
    r"^sg:(?:(review):(c-[0-9a-f]{12})|(approve|reject):(p-[0-9a-f]{12}):([A-Za-z0-9_-]{20,24}))$"
)
CLOSE_CALLBACK_RE = re.compile(r"^sg:close-(approve|reject):(pc-[0-9a-f]{12}):([A-Za-z0-9_-]{20,24})$")


def _project_root() -> Path:
    configured = os.environ.get("SPOTGUARD_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve(strict=False)
    return Path(__file__).resolve().parents[2]


def _emit(value: Any, compact: bool = False) -> None:
    if compact:
        print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=json_default))
    else:
        print(pretty_json(value))


def _decimal_argument(value: str, field: str) -> Decimal:
    try:
        return decimal_value(value, field)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc

def _live_spot_view(service: SpotGuard, *, include_orders: bool) -> dict[str, Any]:
    balance = service.live_executor.read_spot_account()
    orders = service.live_executor.read_open_spot_orders() if include_orders else None
    rows = balance["balances"]
    text = ["LIVE SPOT BALANCE"]
    text.extend(f"- {row['asset']}: free {row['free']}, locked {row['locked']}" for row in rows)
    if not rows:
        text.append("- No non-zero Spot balance.")
    if include_orders:
        text.append("OPEN SPOT ORDERS / TP-SL")
        if orders:
            text.extend(f"- {row.get('symbol')} {row.get('side')} {row.get('type')} qty {row.get('origQty')} price {row.get('price')} stop {row.get('stopPrice')} status {row.get('status')}" for row in orders)
        else:
            text.append("- No active Spot order or TP/SL.")
    result = {"live_spot": True, "balance": balance, "presentation": {"text": "\n".join(text)}}
    if orders is not None:
        result["open_orders"] = orders
    return result



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riskpilot",
        description="RiskPilot — fail-closed Binance Spot trading copilot",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    parser.add_argument("--locale", choices=("en", "id"), help="explicit presentation locale")
    parser.add_argument("--utterance", help="original user utterance for deterministic locale selection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create config.json without overwriting an existing file")
    init_parser.add_argument("--chat-id", help="Telegram owner/direct-chat id")
    init_parser.add_argument("--workspace", type=Path, help="OpenClaw workspace path")

    subparsers.add_parser("check", help="validate config and local safety prerequisites")
    subparsers.add_parser("status", help="show current mode, ledger, MCP, and skill status")
    policy_parser = subparsers.add_parser(
        "policy", help="inspect read-only equity-scaled guardrails"
    )
    policy_sub = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_explain = policy_sub.add_parser(
        "explain", help="explain equity, effective limits, usage, and rejection reasons"
    )
    policy_explain.add_argument("--mode", choices=("paper", "live"))
    policy_explain.add_argument("--symbol")
    policy_explain.add_argument("--quote-amount")
    policy_explain.add_argument("--risk-at-stop")
    subparsers.add_parser("symbols", help="validate and show configured Binance Spot symbols")
    radar_parser = subparsers.add_parser("radar", help="show the latest read-only Smart Radar potential ranking")
    radar_parser.add_argument("--limit", type=int, default=5)

    paper_approve = subparsers.add_parser("paper-approve", help="approve one paper proposal with its one-time code")
    paper_approve.add_argument("proposal_id")
    paper_approve.add_argument("--code", required=True)
    paper_approve.add_argument("--sender-id", required=True)
    paper_approve.add_argument("--chat-id", required=True)
    paper_reject = subparsers.add_parser("paper-reject", help="reject one paper proposal with its one-time code")
    paper_reject.add_argument("proposal_id")
    paper_reject.add_argument("--code", required=True)
    paper_reject.add_argument("--sender-id", required=True)
    paper_reject.add_argument("--chat-id", required=True)

    paper_buy = subparsers.add_parser("paper-buy", help="create a manual paper-test proposal")
    paper_buy.add_argument("--symbol", required=True)
    paper_buy.add_argument("--quote-amount", required=True)
    paper_buy.add_argument("--notify", action="store_true")
    paper_buy.add_argument("--dry-run", action="store_true")
    paper_resend = subparsers.add_parser("paper-resend", help="resend controls for the same pending PAPER proposal")
    paper_resend.add_argument("proposal_id")
    paper_resend.add_argument("--sender-id", required=True)
    paper_resend.add_argument("--chat-id", required=True)
    paper_resend.add_argument("--dry-run", action="store_true")
    live_buy_direct = subparsers.add_parser("live-buy", help="create a dormant LIVE buy proposal")
    live_buy_direct.add_argument("--symbol", required=True)
    live_buy_direct.add_argument("--quote-amount", required=True)
    live_buy_direct.add_argument("--notify", action="store_true")
    live_buy_direct.add_argument("--dry-run", action="store_true")
    live_close_direct = subparsers.add_parser("live-close-all", help="create a dormant LIVE close-all proposal")
    live_close_direct.add_argument("--symbol", required=True)
    live_close_direct.add_argument("--notify", action="store_true")
    live_close_direct.add_argument("--dry-run", action="store_true")
    live_cancel_direct = subparsers.add_parser("live-cancel-protection", help="create a dormant LIVE OCO-cancel proposal")
    live_cancel_direct.add_argument("--symbol", required=True)
    live_cancel_direct.add_argument("--notify", action="store_true")
    live_cancel_direct.add_argument("--dry-run", action="store_true")
    live_restore = subparsers.add_parser("live-restore-protection", help="create a dormant LIVE TP/SL restore proposal")
    live_restore.add_argument("--symbol", required=True); live_restore.add_argument("--notify", action="store_true"); live_restore.add_argument("--dry-run", action="store_true")
    live_partial = subparsers.add_parser("live-exit-percent", help="create a dormant LIVE partial-exit proposal")
    live_partial.add_argument("--symbol", required=True); live_partial.add_argument("--percentage", required=True); live_partial.add_argument("--notify", action="store_true"); live_partial.add_argument("--dry-run", action="store_true")
    live_approve = subparsers.add_parser("live-approve", help="approve one LIVE proposal from a native Telegram button")
    live_approve.add_argument("proposal_id")
    live_approve.add_argument("--sender-id", required=True)
    live_approve.add_argument("--chat-id", required=True)
    live_reject = subparsers.add_parser("live-reject", help="reject one LIVE proposal from a native Telegram button")
    live_reject.add_argument("proposal_id")
    live_reject.add_argument("--sender-id", required=True)
    live_reject.add_argument("--chat-id", required=True)
    live_reconcile_fill = subparsers.add_parser("reconcile-live-risk-fill", help="locally reconcile one verified LIVE fill")
    live_reconcile_fill.add_argument("proposal_id")
    live_reconcile_fill.add_argument("--owner-id", required=True)
    live_reconcile_execution = subparsers.add_parser("reconcile-live-execution", help="read one submitted LIVE execution status")
    live_reconcile_execution.add_argument("proposal_id")
    live_reconcile_execution.add_argument("--owner-id", required=True)
    live_mark_unreconcilable = subparsers.add_parser("mark-live-execution-unreconcilable", help="locally mark an async LIVE execution unreconcilable when all read capabilities are unavailable")
    live_mark_unreconcilable.add_argument("proposal_id")
    live_mark_unreconcilable.add_argument("--owner-id", required=True)
    live_finalize_epoch = subparsers.add_parser("finalize-live-risk-epoch", help="finalize a flat incomplete LIVE risk epoch")
    live_finalize_epoch.add_argument("--owner-id", required=True)
    live_finalize_empty = subparsers.add_parser("finalize-empty-live-risk-epoch", help="abort a proven empty LIVE risk epoch")
    live_finalize_empty.add_argument("--owner-id", required=True)

    paper_parser = subparsers.add_parser("paper", help="inspect the virtual paper account")
    paper_sub = paper_parser.add_subparsers(dest="paper_command", required=True)
    paper_sub.add_parser("balance")
    paper_sub.add_parser("positions")

    paper_reset = subparsers.add_parser("paper-reset", help="locally reset the PAPER account to 1000 USDT")
    paper_reset.add_argument("--owner-id", required=True)

    paper_repair = subparsers.add_parser("paper-repair-bracket", help="locally repair one invalid PAPER economic bracket")
    paper_repair.add_argument("symbol")
    paper_repair.add_argument("--owner-id", required=True)

    paper_intent = subparsers.add_parser("paper-intent", help="normalize a trusted Telegram PAPER intent")
    paper_intent.add_argument("--text", required=True)
    paper_intent.add_argument("--sender-id", required=True)
    paper_intent.add_argument("--chat-id", required=True)
    paper_intent.add_argument("--notify", action="store_true")
    paper_intent.add_argument("--dry-run", action="store_true")
    trade_intent = subparsers.add_parser("trade-intent", help="normalize a trusted Telegram trade intent; LIVE by default")
    trade_intent.add_argument("--text", required=True)
    trade_intent.add_argument("--sender-id", required=True)
    trade_intent.add_argument("--chat-id", required=True)
    trade_intent.add_argument("--notify", action="store_true")
    trade_intent.add_argument("--dry-run", action="store_true")

    paper_close = subparsers.add_parser("paper-close", help="create a manual paper-close proposal")
    paper_close.add_argument("position_id")
    paper_close.add_argument("--sender-id", required=True)
    paper_close.add_argument("--chat-id", required=True)
    paper_close.add_argument("--percentage", default="100")
    paper_close.add_argument("--notify", action="store_true")
    paper_close.add_argument("--dry-run", action="store_true")
    paper_close_approve = subparsers.add_parser("paper-close-approve", aliases=("close-approve",), help="approve a paper close with its one-time code")
    paper_close_approve.add_argument("position_id")
    paper_close_approve.add_argument("--code", required=True)
    paper_close_approve.add_argument("--sender-id", required=True)
    paper_close_approve.add_argument("--chat-id", required=True)
    paper_close_reject = subparsers.add_parser("paper-close-reject", aliases=("close-reject",), help="reject a paper close with its one-time code")
    paper_close_reject.add_argument("position_id")
    paper_close_reject.add_argument("--code", required=True)
    paper_close_reject.add_argument("--sender-id", required=True)
    paper_close_reject.add_argument("--chat-id", required=True)

    paper_close_callback = subparsers.add_parser("paper-close-callback", help=argparse.SUPPRESS)
    paper_close_callback.add_argument("close_id")
    paper_close_callback.add_argument("--action", choices=("approve", "reject"), required=True)
    paper_close_callback.add_argument("--token", required=True)
    paper_close_callback.add_argument("--sender-id", required=True)
    paper_close_callback.add_argument("--chat-id", required=True)

    scan_parser = subparsers.add_parser("scan", help="run the lightweight deterministic market prefilter")
    scan_parser.add_argument("--symbol", action="append", dest="symbols")
    scan_parser.add_argument("--notify", action="store_true")
    scan_parser.add_argument("--fixture", type=Path)
    scan_parser.add_argument("--synthetic", action="store_true", help="use deterministic demo candles")
    scan_parser.add_argument("--dry-run", action="store_true", help="render Telegram payload without sending")

    candidate_parser = subparsers.add_parser("candidate", help="inspect or manage candidates")
    candidate_sub = candidate_parser.add_subparsers(dest="candidate_command", required=True)
    candidate_list = candidate_sub.add_parser("list")
    candidate_list.add_argument("--limit", type=int, default=20)
    candidate_show = candidate_sub.add_parser("show")
    candidate_show.add_argument("candidate_id")
    candidate_dismiss = candidate_sub.add_parser("dismiss")
    candidate_dismiss.add_argument("candidate_id")
    candidate_dismiss.add_argument("--reason", required=True)
    candidate_notify = candidate_sub.add_parser("notify")
    candidate_notify.add_argument("candidate_id")
    candidate_notify.add_argument("--dry-run", action="store_true")

    proposal_parser = subparsers.add_parser("proposal", help="create, inspect, or reject proposals")
    proposal_sub = proposal_parser.add_subparsers(dest="proposal_command", required=True)
    proposal_create = proposal_sub.add_parser("create")
    proposal_create.add_argument("--candidate", required=True, dest="candidate_id")
    proposal_create.add_argument("--bid", required=True)
    proposal_create.add_argument("--ask", required=True)
    proposal_create.add_argument("--quote")
    proposal_create.add_argument("--rationale", required=True)
    proposal_create.add_argument("--notify", action="store_true")
    proposal_create.add_argument("--dry-run", action="store_true")
    proposal_sub.add_parser("active", help="show safe active proposal expiry status")
    proposal_list = proposal_sub.add_parser("list")
    proposal_list.add_argument("--limit", type=int, default=20)
    proposal_show = proposal_sub.add_parser("show")
    proposal_show.add_argument("proposal_id")
    proposal_notify = proposal_sub.add_parser("notify")
    proposal_notify.add_argument("proposal_id")
    proposal_notify.add_argument("--dry-run", action="store_true")
    proposal_reject = proposal_sub.add_parser("reject")
    proposal_reject.add_argument("proposal_id")
    proposal_reject.add_argument("--token", required=True)
    proposal_reject.add_argument("--sender-id", required=True)
    proposal_reject.add_argument("--chat-id", required=True)

    approval_parser = subparsers.add_parser("approval", help="atomically claim one approved proposal")
    approval_sub = approval_parser.add_subparsers(dest="approval_command", required=True)
    approval_claim = approval_sub.add_parser("claim")
    approval_claim.add_argument("proposal_id")
    approval_claim.add_argument("--token", required=True)
    approval_claim.add_argument("--sender-id", required=True)
    approval_claim.add_argument("--chat-id", required=True)

    execution_parser = subparsers.add_parser("execution", help="finalize an active execution lease")
    execution_sub = execution_parser.add_subparsers(dest="execution_command", required=True)
    execution_paper = execution_sub.add_parser("paper")
    execution_paper.add_argument("proposal_id")
    execution_paper.add_argument("--lease", required=True)
    execution_complete = execution_sub.add_parser("complete")
    execution_complete.add_argument("proposal_id")
    execution_complete.add_argument("--lease", required=True)
    execution_complete.add_argument("--order-id", required=True)
    execution_complete.add_argument("--status", required=True)
    execution_complete.add_argument("--filled-quantity")
    execution_complete.add_argument("--average-price")
    execution_complete.add_argument("--fee-quote")
    execution_fail = execution_sub.add_parser("fail")
    execution_fail.add_argument("proposal_id")
    execution_fail.add_argument("--lease", required=True)
    execution_fail.add_argument("--reason", required=True)
    execution_uncertain = execution_sub.add_parser("uncertain")
    execution_uncertain.add_argument("proposal_id")
    execution_uncertain.add_argument("--lease", required=True)
    execution_uncertain.add_argument("--reason", required=True)

    live_parser = subparsers.add_parser("live", help="manage the short-lived live-execution arm")
    live_sub = live_parser.add_subparsers(dest="live_command", required=True)
    live_sub.add_parser("status")
    live_sub.add_parser("balance", help="read non-zero live Spot balances")
    live_sub.add_parser("positions", help="read live Spot balances and open TP/SL orders")
    live_buy = live_sub.add_parser("buy")
    live_buy.add_argument("--symbol", required=True)
    live_buy.add_argument("--quote-amount", required=True)
    live_buy.add_argument("--notify", action="store_true")
    live_buy.add_argument("--dry-run", action="store_true")
    live_pause = live_sub.add_parser("pause")
    live_pause.add_argument("--owner-id", required=True)
    live_arm = live_sub.add_parser("arm")
    live_arm.add_argument("--minutes", type=int, default=60)
    live_arm.add_argument("--owner-id", required=True)
    live_recovery_prepare = live_sub.add_parser("prepare-recovery")
    live_recovery_prepare.add_argument("--symbol", required=True)
    live_recovery_prepare.add_argument("--owner-id", required=True)
    live_recovery_arm = live_sub.add_parser("arm-recovery")
    live_recovery_arm.add_argument("--minutes", type=int, default=15)
    live_recovery_arm.add_argument("--owner-id", required=True)
    live_disarm = live_sub.add_parser("disarm")
    live_disarm.add_argument("--owner-id", required=True)
    live_enable = live_sub.add_parser("enable")
    live_enable.add_argument("--owner-id", required=True)
    live_disable = live_sub.add_parser("disable")
    live_disable.add_argument("--owner-id", required=True)

    scheduled_parser = subparsers.add_parser("scheduled-mode", help="set scheduled proposal mode locally")
    scheduled_sub = scheduled_parser.add_subparsers(dest="scheduled_command", required=True)
    scheduled_set = scheduled_sub.add_parser("set")
    scheduled_set.add_argument("mode", choices=("paper", "live"))
    scheduled_set.add_argument("--owner-id", required=True)

    risk_profile = subparsers.add_parser("risk-profile", help="apply the canonical local PAPER/LIVE risk profile")
    risk_profile_sub = risk_profile.add_subparsers(dest="risk_profile_command", required=True)
    risk_profile_apply = risk_profile_sub.add_parser("apply")
    risk_profile_apply.add_argument("--owner-id", required=True)

    callback_parser = subparsers.add_parser("callback", help="parse a Telegram callback without executing it")
    callback_parser.add_argument("--data", required=True)
    callback_parser.add_argument("--sender-id")
    callback_parser.add_argument("--chat-id")

    agent_os_parser = subparsers.add_parser(
        "agent-os", help="use the on-demand read-only Codex CLI bridge"
    )
    agent_os_sub = agent_os_parser.add_subparsers(dest="agent_os_command", required=True)
    agent_os_sub.add_parser("status", help="check Codex login and Binance MCP configuration")
    agent_os_market = agent_os_sub.add_parser(
        "market", help="read verified live Spot market data through Binance Agent OS"
    )
    agent_os_market.add_argument("--symbol", required=True)
    permission_parser = subparsers.add_parser(
        "verify-live-trade-permission", help="explicitly attest Spot trade permission with the non-submitting order test"
    )
    permission_parser.add_argument("--owner-id", required=True)
    decimal_parser = subparsers.add_parser(
        "verify-live-decimal-transport", help="explicitly attest fractional Spot decimal transport"
    )
    decimal_parser.add_argument("--owner-id", required=True)
    diagnostic_parser = subparsers.add_parser(
        "diagnose-live-decimal-transport", help="diagnose size-sensitive Spot decimal transport"
    )
    diagnostic_parser.add_argument("--owner-id", required=True)
    prepare_live = subparsers.add_parser(
        "prepare-live-session", help="collect one consolidated, read-only LIVE readiness preflight"
    )
    prepare_live.add_argument("--symbol", required=True)
    prepare_live.add_argument("--owner-id", required=True)
    history_discovery = subparsers.add_parser(
        "discover-live-trade-history-tool", help="discover the authenticated Spot trade-history read capability"
    )
    history_discovery.add_argument("--owner-id", required=True)
    read_capability_discovery = subparsers.add_parser(
        "discover-live-execution-read-capabilities", help="discover read-only LIVE execution capabilities"
    )
    read_capability_discovery.add_argument("--owner-id", required=True)
    rate_limit_clear = subparsers.add_parser(
        "clear-binance-rate-limit", help="explicitly clear the local Binance rate-limit circuit"
    )
    rate_limit_clear.add_argument("--owner-id", required=True)
    analyze_parser = subparsers.add_parser(
        "analyze", help="read one allowlisted closed candle through Binance Agent OS"
    )
    analyze_parser.add_argument("symbol", help="configured symbol or base alias, for example BTC")
    analyze_parser.add_argument("--amount", "--quote-amount", dest="quote_amount", type=lambda value: _decimal_argument(value, "amount"))
    comparison = subparsers.add_parser("compare", help="compare verified closed-candle changes; never recommend an unverified entry")
    comparison.add_argument("symbols", nargs="*", help="configured symbols; defaults to the allowlist")
    comparison.add_argument("--amount", "--quote-amount", dest="quote_amount", type=lambda value: _decimal_argument(value, "amount"))
    for alias in sorted(vocabulary("input.analysis") - {"analyze"}):
        alias_parser = subparsers.add_parser(alias, help=argparse.SUPPRESS)
        alias_parser.add_argument("symbol", help=argparse.SUPPRESS)
    agent_os_review = agent_os_sub.add_parser(
        "review", help="review one candidate and create a paper proposal"
    )
    agent_os_review.add_argument("--candidate", required=True, dest="candidate_id")
    agent_os_review.add_argument("--notify", action="store_true")
    agent_os_review.add_argument("--dry-run", action="store_true")
    agent_os_review.add_argument("--dispatch-source", choices=("cli", "telegram_direct"), default="cli", help=argparse.SUPPRESS)
    agent_os_demo = agent_os_sub.add_parser(
        "demo", help="seed a paper candidate from a verified live Agent OS read"
    )
    agent_os_demo.add_argument("--symbol", required=True)
    agent_os_demo.add_argument("--notify", action="store_true")
    agent_os_demo.add_argument("--dry-run", action="store_true")

    demo_candidate = subparsers.add_parser(
        "demo-candidate",
        help="create a paper-only candidate seeded from a fresh Agent OS read price",
    )
    demo_candidate.add_argument("--symbol", required=True)
    demo_candidate.add_argument("--entry", required=True)
    demo_candidate.add_argument("--notify", action="store_true")
    demo_candidate.add_argument("--dry-run", action="store_true")

    demo_parser = subparsers.add_parser("demo", help="create a deterministic paper-mode end-to-end demo")
    demo_parser.add_argument("--notify", action="store_true")
    demo_parser.add_argument("--dry-run", action="store_true")
    demo_parser.add_argument("--auto-approve", action="store_true")

    return parser


def _parse_callback(data: str) -> dict[str, str | None]:
    close_match = CLOSE_CALLBACK_RE.fullmatch(data)
    if close_match:
        action, close_id, token = close_match.groups()
        return {"action": f"close-{action}", "close_id": close_id, "candidate_id": None,
                "proposal_id": None, "token": token}
    match = CALLBACK_RE.fullmatch(data)
    if not match:
        raise SecurityError("callback format is invalid")
    review_action, candidate_id, proposal_action, proposal_id, token = match.groups()
    if review_action:
        return {"action": "review", "candidate_id": candidate_id, "proposal_id": None, "token": None}
    return {"action": proposal_action, "candidate_id": None, "proposal_id": proposal_id, "token": token}


def _dispatch_callback(service: SpotGuard, data: str, sender_id: str, chat_id: str) -> dict[str, Any]:
    parsed = _parse_callback(data)
    try:
        if parsed["action"] == "approve":
            proposal = service.ledger.get_proposal(parsed["proposal_id"])
            claim = service.claim(parsed["proposal_id"], parsed["token"], sender_id, chat_id)
            if claim["mode"] == "live":
                result = service.execute_live(parsed["proposal_id"], claim["lease"])
                return {"ok": result.get("status") == "EXECUTED", "message": "LIVE approval processed; inspect Binance reconciliation status", "proposal": result}
            if claim["mode"] != "paper":
                raise SecurityError("proposal mode is invalid")
            fill = service.execute_paper(parsed["proposal_id"], claim["lease"])
            return {"ok": True, "message": "APPROVE PAPER accepted; simulated fill completed", "proposal": fill, "presentation": fill["presentation"]}
        if parsed["action"] == "reject":
            rejected = service.reject(parsed["proposal_id"], parsed["token"], sender_id, chat_id)
            return {"ok": True, "message": "Proposal rejected", "proposal": rejected, "presentation": rejected["presentation"]}
        if parsed["action"] == "close-approve":
            return service.approve_paper_close(parsed["close_id"], sender_id, chat_id, token=parsed["token"])
        if parsed["action"] == "close-reject":
            return service.reject_paper_close(parsed["close_id"], parsed["token"], sender_id, chat_id)
        raise SecurityError("review callbacks are not approval callbacks")
    except Exception as exc:
        entity_id = parsed.get("proposal_id") or parsed.get("close_id")
        service.ledger.add_event("approval.rejected", entity_id, {
            "method": "telegram_callback", "reason_type": type(exc).__name__
        })
        raise


def _check_result(service: SpotGuard) -> dict[str, Any]:
    status = service.status()
    config_mode = service.settings.config_path.stat().st_mode & 0o777
    warnings: list[str] = []
    if config_mode & 0o022:
        warnings.append("config.json is writable by group/others; run chmod 600 config.json")
    if not status["openclaw"]["available"]:
        warnings.append("OpenClaw CLI is not available in PATH")
    if not status["openclaw"]["skill_installed"]:
        warnings.append("binance-spotguard skill is not installed in the OpenClaw workspace")
    if not status["codex_agent_os"]["available"]:
        warnings.append("Codex CLI is not installed or not available in PATH")
    elif not status["codex_agent_os"]["logged_in"]:
        warnings.append("Codex CLI is not signed in; use ChatGPT device authentication")
    if not status["codex_agent_os"]["mcp_configured"]:
        warnings.append("Binance Agent OS MCP is not configured in Codex CLI")
    if not status["codex_agent_os"]["currently_usable"]:
        warnings.append("Binance Agent OS has no successful read-only probe in this process")
    return {
        "ok": not [item for item in warnings if "config.json" in item],
        "config": str(service.settings.config_path),
        "config_mode": oct(config_mode),
        "state_dir": str(service.settings.state_dir),
        "safety": {
            "mode": service.settings.mode,
            "manual_approval": True,
            "product": "spot",
            "withdrawal": False,
            "futures": False,
            "margin": False,
            "transfer": False,
        },
        "warnings": warnings,
        "status": status,
    }


def _require_local_admin(service: SpotGuard, owner_id: str, phrase: str) -> None:
    if not sys.stdin.isatty():
        raise SecurityError("administrative changes require a local interactive TTY")
    if owner_id != service.settings.openclaw.telegram_owner_id:
        raise SecurityError("local administrative owner does not match")
    print(f"Type {phrase} to continue:", file=sys.stderr)
    if input().strip() != phrase:
        raise SecurityError("administrative confirmation phrase did not match")


def _local_admin_update(service: SpotGuard, owner_id: str, phrase: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a local admin mutation only when validation and auditing are atomic."""
    _require_local_admin(service, owner_id, phrase)
    path = service.settings.config_path
    original = path.read_bytes()
    raw = json.loads(original.decode("utf-8"))
    for dotted, value in changes.items():
        target = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    temporary = path.with_name(path.name + ".tmp")
    backup = path.with_name(path.name + ".backup-" + utcnow().strftime("%Y%m%dT%H%M%SZ"))
    try:
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        # Validate the exact bytes before they can replace production config.
        load_settings(temporary, create_state=False)
        service.ledger.add_event("admin.config_change_intent", None,
                                 {"keys": sorted(changes), "local_tty": True})
        backup.write_bytes(original)
        backup.chmod(0o600)
        temporary.replace(path)
        try:
            service.ledger.add_event("admin.config_changed", None,
                                     {"keys": sorted(changes), "local_tty": True, "backup": str(backup)})
        except Exception:
            backup.replace(path)
            raise SecurityError("administrative update audit failed; original config was restored")
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "changed": sorted(changes), "backup": str(backup), "restart_required": False}


def _risk_profile_changes() -> dict[str, Any]:
    """Canonical PAPER/LIVE limits for the local, approval-gated admin path."""
    profile = {"max_quote_per_entry_usdt": 100, "max_active_tranches": 10,
               "max_economic_positions": 5, "max_open_exposure_usdt": 500,
               "max_risk_per_position_usdt": 2, "max_aggregate_risk_usdt": 4,
               "max_successful_entries_per_utc_day": 10, "daily_realized_loss_cap_usdt": 5}
    changes: dict[str, Any] = {"risk.max_quote_per_trade": 100, "risk.max_daily_quote": 500,
        "live.enabled": False, "live.armed": False, "live.min_free_reserve_usdt": 8,
        "live.max_pending_proposals": 1, "scheduled_proposal_mode": "paper", "execution_ready": False}
    changes.update({f"paper.{key}": value for key, value in profile.items()})
    changes.update({f"live.{key}": value for key, value in profile.items()})
    # Preserve legacy readers without leaving the obsolete 6-USDT ceiling in
    # production configuration. The only remaining 6 is default order size.
    changes.update({"live.max_live_trade_usdt": 100, "live.max_open_positions": 5,
                    "live.max_risk_per_trade_usdt": 2, "live.daily_loss_cap_usdt": 5})
    return changes


def _run(args: argparse.Namespace) -> Any:
    config_path = args.config or default_config_path()
    if args.command == "init":
        source = _project_root() / "config.example.json"
        created = initialize_config(config_path, source, chat_id=args.chat_id, workspace=args.workspace)
        return {"ok": True, "config": str(created), "mode": "paper"}

    settings = load_settings(config_path)
    service = SpotGuard(settings, locale=args.locale)
    args._locale = service.locale
    if args.utterance or args.command in vocabulary("input.analysis") or args.command in {"compare", "paper-buy", "live-buy", "live-close-all", "live-cancel-protection", "live-exit-percent", "paper-close"}:
        args._locale = service.select_locale(args.utterance or args.command, args.locale)

    if args.command == "check":
        return _check_result(service)
    if args.command == "status":
        return service.status()
    if args.command == "policy":
        return service.policy_explain(
            mode=args.mode,
            symbol=args.symbol,
            quote_amount=(decimal_value(args.quote_amount, "quote_amount")
                          if args.quote_amount is not None else None),
            risk_at_stop=(decimal_value(args.risk_at_stop, "risk_at_stop")
                          if args.risk_at_stop is not None else None),
        )
    if args.command == "symbols":
        return service.symbols_status()
    if args.command == "verify-live-trade-permission":
        _require_local_admin(service, args.owner_id, "VERIFY RISKPILOT LIVE SPOT TRADE PERMISSION")
        return service.verify_live_trade_permission(operator_confirmed=True)
    if args.command == "verify-live-decimal-transport":
        _require_local_admin(service, args.owner_id, "VERIFY RISKPILOT LIVE DECIMAL TRANSPORT")
        return service.verify_live_decimal_transport(operator_confirmed=True)
    if args.command == "diagnose-live-decimal-transport":
        _require_local_admin(service, args.owner_id, "DIAGNOSE RISKPILOT LIVE DECIMAL TRANSPORT")
        return service.diagnose_live_decimal_transport(operator_confirmed=True)
    if args.command == "prepare-live-session":
        _require_local_admin(service, args.owner_id, "PREPARE RISKPILOT LIVE SESSION")
        return service.prepare_live_session(args.symbol, operator_confirmed=True)
    if args.command == "discover-live-trade-history-tool":
        _require_local_admin(service, args.owner_id, "DISCOVER RISKPILOT LIVE TRADE HISTORY TOOL")
        return service.discover_live_trade_history_tool(operator_confirmed=True)
    if args.command == "discover-live-execution-read-capabilities":
        _require_local_admin(service, args.owner_id, "DISCOVER RISKPILOT LIVE EXECUTION READ CAPABILITIES")
        return service.discover_live_execution_read_capabilities(operator_confirmed=True)
    if args.command == "clear-binance-rate-limit":
        _require_local_admin(service, args.owner_id, "CLEAR RISKPILOT BINANCE RATE-LIMIT CIRCUIT")
        return service.live_executor.clear_rate_limit_circuit()
    if args.command == "radar":
        return service.smart_radar(args.limit)
    if args.command == "paper-approve":
        return service.paper_text_approve(args.proposal_id, args.code, args.sender_id, args.chat_id)
    if args.command == "paper-reject":
        return service.paper_text_reject(args.proposal_id, args.code, args.sender_id, args.chat_id)
    if args.command == "paper-buy":
        return service.create_manual_buy_proposal(args.symbol, decimal_value(args.quote_amount, "quote_amount"), notify=args.notify, dry_run=args.dry_run)
    if args.command == "paper-resend":
        return service.resend_paper_proposal(args.proposal_id, args.sender_id, args.chat_id, dry_run=args.dry_run)
    if args.command == "live-buy":
        return service.create_manual_buy_proposal(args.symbol, decimal_value(args.quote_amount, "quote_amount"), live=True, notify=args.notify, dry_run=args.dry_run)
    if args.command == "live-close-all":
        return service.create_live_close_all_proposal(args.symbol, notify=args.notify, dry_run=args.dry_run)
    if args.command == "live-cancel-protection":
        return service.create_live_cancel_protection_proposal(args.symbol, notify=args.notify, dry_run=args.dry_run)
    if args.command == "live-restore-protection":
        return service.create_live_restore_protection_proposal(args.symbol, notify=args.notify, dry_run=args.dry_run)
    if args.command == "live-exit-percent":
        return service.create_live_partial_exit_proposal(args.symbol, decimal_value(args.percentage, "percentage"), notify=args.notify, dry_run=args.dry_run)
    if args.command == "live-approve":
        return service.approve_live_button(args.proposal_id, args.sender_id, args.chat_id)
    if args.command == "live-reject":
        return service.reject_live_button(args.proposal_id, args.sender_id, args.chat_id)
    if args.command == "reconcile-live-risk-fill":
        _require_local_admin(service, args.owner_id, "RECONCILE RISKPILOT LIVE RISK FILL")
        return service.reconcile_live_risk_fill(args.proposal_id, operator_confirmed=True)
    if args.command == "reconcile-live-execution":
        _require_local_admin(service, args.owner_id, "RECONCILE RISKPILOT LIVE EXECUTION")
        return service.reconcile_live_execution(args.proposal_id, operator_confirmed=True)
    if args.command == "mark-live-execution-unreconcilable":
        _require_local_admin(service, args.owner_id, "MARK RISKPILOT LIVE EXECUTION UNRECONCILABLE")
        return service.mark_live_execution_unreconcilable(args.proposal_id, operator_confirmed=True)
    if args.command == "finalize-live-risk-epoch":
        _require_local_admin(service, args.owner_id, "FINALIZE RISKPILOT INCOMPLETE LIVE RISK EPOCH")
        return service.finalize_live_risk_epoch(operator_confirmed=True)
    if args.command == "finalize-empty-live-risk-epoch":
        _require_local_admin(service, args.owner_id, "FINALIZE RISKPILOT EMPTY LIVE RISK EPOCH")
        return service.finalize_live_risk_epoch(operator_confirmed=True, empty_only=True)
    if args.command == "paper":
        return service.paper_status() if args.paper_command == "positions" else service.paper_balance_status()
    if args.command in {"paper-intent", "trade-intent"}:
        service._validate_owner(args.sender_id)
        if args.chat_id != service.settings.telegram.chat_id:
            raise SecurityError("trusted intent chat does not match the configured Telegram chat")
        args._locale = service.select_locale(args.text, args.locale)
        intent = (normalize_trade_intent if args.command == "trade-intent" else normalize_paper_intent)(
            args.text, service.settings.market.symbols, service.locale)
        if intent["action"] == "analysis":
            amount = Decimal(intent["quote_amount"]) if "quote_amount" in intent else None
            return service.analyze_market(intent["symbol"], amount)
        if intent["action"] == "ranking":
            return service.compare_markets()
        if intent["action"] == "radar":
            return {"intent": intent, **service.smart_radar()}
        if intent["action"] == "buy":
            return {"intent": intent, **service.create_manual_buy_proposal(
                intent["symbol"], Decimal(intent["quote_amount"]),
                live=intent.get("mode") == "live", notify=args.notify, dry_run=args.dry_run)}
        if intent["action"] == "close":
            if intent.get("mode") == "live":
                if intent.get("close_selector") == "all":
                    return {"intent": intent, **service.create_live_partial_exit_proposal(intent["symbol"], Decimal("100"), notify=args.notify, dry_run=args.dry_run)}
                if intent.get("close_selector") == "percentage":
                    return {"intent": intent, **service.create_live_partial_exit_proposal(intent["symbol"], Decimal(intent["percentage"]), notify=args.notify, dry_run=args.dry_run)}
                raise PolicyError("LIVE partial exit accepts only a percentage or 'sell all'")
            positions = [row for row in service.ledger.list_paper_positions(True) if row["symbol"] == intent["symbol"]]
            if not positions:
                raise PolicyError(f"no open PAPER position exists for {intent['symbol']}")
            return {"intent": intent, **service.create_paper_close_proposal(
                positions[0]["id"], args.sender_id, args.chat_id,
                Decimal(intent.get("percentage", "100")),
                notify=args.notify, dry_run=args.dry_run,
                close_quantity=Decimal(intent["close_quantity"]) if intent.get("close_selector") == "quantity" else None,
                close_quote_amount=Decimal(intent["close_quote_amount"]) if intent.get("close_selector") == "quote" else None,
            )}
        if intent["action"] == "reject_pending":
            return {"intent":intent,"rejected":service.ledger.reject_all_pending_paper("explicit natural-language reject pending; chained buy deferred")}
        if intent["action"] in {"positions", "live_positions"}:
            return {"intent": intent, **_live_spot_view(service, include_orders=True)}
        if intent["action"] in {"paper_positions"}:
            return {"intent": intent, **service.paper_status()}
        if intent["action"] in {"status", "paper_status"}:
            return {"intent": intent, **service.paper_status()}
        if intent["action"] == "live_status":
            return {"intent": intent, **_live_spot_view(service, include_orders=True)}
        if intent["action"] in {"balance", "live_balance"}:
            return {"intent": intent, **_live_spot_view(service, include_orders=False)}
        if intent["action"] == "paper_balance":
            return {"intent": intent, **service.paper_balance_status()}
        return {"ok": False, "intent": intent}
    if args.command == "paper-reset":
        _require_local_admin(service, args.owner_id, "RESET RISK PILOT PAPER ACCOUNT TO 1000 USDT")
        return service.backup_and_reset_paper()
    if args.command == "paper-repair-bracket":
        symbol = args.symbol.upper()
        _require_local_admin(service, args.owner_id, f"REPAIR RISK PILOT PAPER BRACKET {symbol}")
        return service.backup_and_repair_paper_bracket(symbol)
    if args.command == "paper-close":
        return service.create_paper_close_proposal(args.position_id, args.sender_id, args.chat_id,
                                                   Decimal(args.percentage), notify=args.notify, dry_run=args.dry_run)
    if args.command in {"paper-close-approve", "close-approve"}:
        return service.approve_paper_close_by_position(args.position_id, args.code,
                                                       args.sender_id, args.chat_id)
    if args.command in {"paper-close-reject", "close-reject"}:
        return service.reject_paper_close_by_position(args.position_id, args.code,
                                                      args.sender_id, args.chat_id)
    if args.command == "paper-close-callback":
        if args.action == "approve":
            return service.approve_paper_close(args.close_id, args.sender_id, args.chat_id, token=args.token)
        return service.reject_paper_close(args.close_id, args.token, args.sender_id, args.chat_id)
    if args.command == "scan":
        if args.fixture and args.synthetic:
            raise SpotGuardError("choose either --fixture or --synthetic")
        return service.scan(
            symbols=[item.upper() for item in args.symbols] if args.symbols else None,
            notify=args.notify,
            fixture=args.fixture,
            synthetic=args.synthetic,
            dry_run=args.dry_run,
        )
    if args.command == "candidate":
        if args.candidate_command == "list":
            return {"candidates": service.ledger.list_candidates(limit=args.limit)}
        if args.candidate_command == "show":
            return service.ledger.get_candidate(args.candidate_id)
        if args.candidate_command == "dismiss":
            return service.ledger.dismiss_candidate(args.candidate_id, args.reason)
        if args.candidate_command == "notify":
            return service.notify_candidate(args.candidate_id, dry_run=args.dry_run)
    if args.command == "proposal":
        if args.proposal_command == "create":
            bid = decimal_value(args.bid, "bid")
            ask = decimal_value(args.ask, "ask")
            quote = decimal_value(args.quote, "quote") if args.quote else None
            return service.create_proposal(
                args.candidate_id,
                bid,
                ask,
                quote,
                args.rationale,
                notify=args.notify,
                dry_run=args.dry_run,
            )
        if args.proposal_command == "active":
            return {"active_proposals": service.ledger.active_proposals_status()}
        if args.proposal_command == "list":
            return {"proposals": service.ledger.list_proposals(limit=args.limit)}
        if args.proposal_command == "show":
            return service.ledger.get_proposal(args.proposal_id)
        if args.proposal_command == "notify":
            return service.notify_proposal(args.proposal_id, dry_run=args.dry_run)
        if args.proposal_command == "reject":
            return service.reject(args.proposal_id, args.token, args.sender_id, args.chat_id)
    if args.command == "approval" and args.approval_command == "claim":
        return service.claim(args.proposal_id, args.token, args.sender_id, args.chat_id)
    if args.command == "execution":
        if args.execution_command == "paper":
            return service.execute_paper(args.proposal_id, args.lease)
        if args.execution_command == "complete":
            return service.complete_live(
                args.proposal_id,
                args.lease,
                args.order_id,
                args.status,
                filled_quantity=args.filled_quantity,
                average_price=args.average_price,
                fee_quote=args.fee_quote,
            )
        if args.execution_command == "fail":
            return service.fail_execution(args.proposal_id, args.lease, args.reason)
        if args.execution_command == "uncertain":
            return service.uncertain_execution(args.proposal_id, args.lease, args.reason)
    if args.command == "live":
        if args.live_command == "status":
            return service.live_status(check_symbols=True)
        if args.live_command == "balance":
            return _live_spot_view(service, include_orders=False)
        if args.live_command == "positions":
            return _live_spot_view(service, include_orders=True)
        if args.live_command == "buy":
            return service.create_manual_buy_proposal(args.symbol, decimal_value(args.quote_amount, "quote_amount"), live=True, notify=args.notify, dry_run=args.dry_run)
        if args.live_command in {"pause", "disarm"}:
            _require_local_admin(service, args.owner_id, "DISARM RISK PILOT LIVE")
            result = service.live_arm.disarm().__dict__
            service.invalidate_prepared_live_session("live_disarmed")
            service.ledger.add_event("admin.live_disarmed", None, {"local_tty": True})
            return result
        if args.live_command == "enable":
            return _local_admin_update(service, args.owner_id, "ENABLE RISK PILOT LIVE", {"live.enabled": True})
        if args.live_command == "disable":
            result = _local_admin_update(service, args.owner_id, "DISABLE RISK PILOT LIVE", {"live.enabled": False, "live.armed": False})
            service.live_arm.disarm()
            service.invalidate_prepared_live_session("live_disabled")
            return result
        if args.live_command == "arm":
            if args.owner_id != service.settings.openclaw.telegram_owner_id:
                raise SecurityError("local administrative owner does not match")
            if not sys.stdin.isatty():
                raise SecurityError("live mode can only be armed interactively from a terminal")
            print("Type ARM SPOT LIVE to enable real Spot execution for a limited time:", file=sys.stderr)
            if input().strip() != "ARM SPOT LIVE":
                raise SecurityError("live arm phrase did not match")
            return service.arm_live(args.minutes)
        if args.live_command == "prepare-recovery":
            _require_local_admin(service, args.owner_id, "PREPARE RISKPILOT LIVE RECOVERY SESSION")
            return service.prepare_live_recovery_session(args.symbol, operator_confirmed=True)
        if args.live_command == "arm-recovery":
            _require_local_admin(service, args.owner_id, "ARM RISKPILOT LIVE RECOVERY")
            return service.arm_live_recovery(args.minutes)
    if args.command == "scheduled-mode":
        phrase = f"SET SCHEDULED PROPOSAL MODE {args.mode.upper()}"
        return _local_admin_update(service, args.owner_id, phrase, {"scheduled_proposal_mode": args.mode})
    if args.command == "risk-profile" and args.risk_profile_command == "apply":
        return _local_admin_update(service, args.owner_id,
                                   "APPLY RISKPILOT PAPER AND LIVE RISK PROFILE",
                                   _risk_profile_changes())
    if args.command == "callback":
        parsed = _parse_callback(args.data)
        if args.sender_id is None and args.chat_id is None:
            return parsed
        if not args.sender_id or not args.chat_id:
            raise SecurityError("callback handling requires both trusted sender and chat IDs")
        return _dispatch_callback(service, args.data, args.sender_id, args.chat_id)
    if args.command == "agent-os":
        if args.agent_os_command == "status":
            return service.agent_os.status()
        if args.agent_os_command == "market":
            return service.agent_os.review_market(args.symbol)
        if args.agent_os_command == "review":
            return service.review_candidate_with_agent_os(
                args.candidate_id,
                notify=args.notify,
                dry_run=args.dry_run,
                dispatch_source=args.dispatch_source,
            )
        if args.agent_os_command == "demo":
            return service.create_agent_os_demo_candidate(
                args.symbol,
                notify=args.notify,
                dry_run=args.dry_run,
            )
    if args.command in vocabulary("input.analysis"):
        return service.analyze_market(args.symbol, getattr(args, "quote_amount", None))
    if args.command == "compare":
        return service.compare_markets(args.symbols, args.quote_amount)
    if args.command == "demo-candidate":
        return service.create_demo_candidate(
            args.symbol,
            decimal_value(args.entry, "entry"),
            notify=args.notify,
            dry_run=args.dry_run,
        )
    if args.command == "demo":
        if settings.mode != "paper":
            raise SecurityError("the deterministic demo is only available in paper mode")
        scan = service.scan(
            symbols=[settings.market.symbols[0]],
            notify=False,
            synthetic=True,
            dry_run=args.dry_run,
        )
        candidate = None
        for result in scan["results"]:
            if result.get("candidate"):
                candidate = result["candidate"]
                break
        if candidate is None:
            candidates = service.ledger.list_candidates(limit=1)
            if not candidates:
                raise SpotGuardError("synthetic demo did not produce a candidate")
            candidate = candidates[0]
        if candidate["status"] != "ACTIVE":
            raise SpotGuardError("latest demo candidate already has a proposal; use a fresh state directory")
        proposal_result = service.create_proposal(
            candidate["id"],
            decimal_value(candidate["price"], "candidate.price") * Decimal("0.999"),
            decimal_value(candidate["price"], "candidate.price"),
            settings.risk.default_quote_amount,
            "Paper demo: the deterministic prefilter and fresh-price policy both passed.",
            notify=args.notify,
            dry_run=args.dry_run,
        )
        output: dict[str, Any] = {"scan": scan, "proposal": proposal_result}
        if args.auto_approve:
            proposal = proposal_result["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, settings.openclaw.telegram_owner_id)
            execution = service.execute_paper(proposal["id"], claim["lease"])
            output["claim"] = claim
            output["execution"] = execution
        return output
    raise SpotGuardError("unhandled command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if "--json" in raw_args:
        raw_args = [item for item in raw_args if item != "--json"]
        raw_args.insert(0, "--json")
    args = parser.parse_args(raw_args)
    args._locale = args.locale or detect_locale(args.utterance or getattr(args, "text", "") or args.command)
    try:
        result = _run(args)
        if isinstance(result, dict) and "presentation" not in result:
            result["presentation"] = {"locale": args._locale, "text": render(result, args._locale, args.command)}
        _emit(result, compact=args.json)
        return 0
    except (
        ConfigError,
        CodexBridgeError,
        LedgerError,
        MarketError,
        PolicyError,
        SecurityError,
        SpotGuardError,
        TelegramError,
        ValueError,
    ) as exc:
        locale = getattr(exc, "presentation_locale", args._locale)
        _emit({"ok": False, "error": str(exc), "type": type(exc).__name__,
               "presentation": {"locale": locale, "text": error_text(exc, locale)}}, compact=args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

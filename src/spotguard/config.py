from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .risk_policy.schema import (
    SizingPolicyConfigError,
    SizingPolicySettings,
    load_sizing_policy,
)
from .util import decimal_value, ensure_private_dir


class ConfigError(ValueError):
    pass


SENSITIVE_KEY_RE = re.compile(r"(?:api[_-]?key|secret|private[_-]?key|access[_-]?key|binance[_-]?token)", re.I)
ALLOWED_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"
}
ALLOWED_MARKET_HOSTS = {
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
}


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    chat_id: str
    channel: str
    account: str | None


@dataclass(frozen=True)
class MarketSettings:
    base_url: str
    symbols: tuple[str, ...]
    interval: str
    lookback: int
    request_timeout_seconds: int
    min_signal_score: int
    candidate_ttl_minutes: int
    candidate_cooldown_minutes: int
    max_entry_drift_pct: Decimal
    max_spread_pct: Decimal


@dataclass(frozen=True)
class RiskSettings:
    quote_asset: str
    default_quote_amount: Decimal
    min_quote_amount: Decimal
    max_quote_per_trade: Decimal
    max_daily_quote: Decimal
    max_active_proposals: int
    min_stop_distance_pct: Decimal
    max_stop_distance_pct: Decimal
    atr_stop_multiplier: Decimal
    min_reward_risk: Decimal
    proposal_ttl_minutes: int
    execution_lease_seconds: int
    max_live_arm_minutes: int
    paper_fee_pct: Decimal

    @property
    def default_order_size_usdt(self) -> Decimal:
        """Canonical analysis name; default_quote_amount remains compatible."""
        return self.default_quote_amount


@dataclass(frozen=True)
class PaperSettings:
    initial_balance_usdt: Decimal
    max_quote_per_entry_usdt: Decimal
    max_active_tranches: int
    max_economic_positions: int
    max_open_exposure_usdt: Decimal
    max_successful_entries_per_utc_day: int
    max_risk_per_position_usdt: Decimal
    max_aggregate_risk_usdt: Decimal
    daily_realized_loss_cap_usdt: Decimal
    close_proposal_ttl_seconds: int
    slippage_pct: Decimal

    @property
    def max_open_positions(self) -> int:  # legacy API compatibility
        return self.max_active_tranches

    @property
    def max_risk_per_trade_usdt(self) -> Decimal:  # legacy API compatibility
        return self.max_risk_per_position_usdt


@dataclass(frozen=True)
class LiveSettings:
    enabled: bool
    arm: bool
    max_quote_per_entry_usdt: Decimal
    allowed_symbols: tuple[str, ...]
    max_active_tranches: int
    max_economic_positions: int
    max_open_exposure_usdt: Decimal
    min_free_reserve_usdt: Decimal
    max_risk_per_position_usdt: Decimal
    max_aggregate_risk_usdt: Decimal
    max_successful_entries_per_utc_day: int
    max_pending_proposals: int
    approval_ttl_seconds: int
    daily_realized_loss_cap_usdt: Decimal
    weekly_loss_cap_usdt: Decimal
    protective_orders_available: bool

    @property
    def max_live_trade_usdt(self) -> Decimal:
        return self.max_quote_per_entry_usdt

    @property
    def max_open_positions(self) -> int:
        return self.max_economic_positions

    @property
    def max_risk_per_trade_usdt(self) -> Decimal:
        return self.max_risk_per_position_usdt

    @property
    def daily_loss_cap_usdt(self) -> Decimal:
        return self.daily_realized_loss_cap_usdt


@dataclass(frozen=True)
class OpenClawSettings:
    command: str
    telegram_owner_id: str


@dataclass(frozen=True)
class CodexSettings:
    command: str
    mcp_server: str
    endpoint: str
    timeout_seconds: int
    model: str | None
    read_only: bool
    # These are deliberately optional at configuration-load time so an existing
    # PAPER ledger remains inspectable.  The bridge refuses to run unless both
    # are present and have passed the dedicated-profile validation below.
    agent_os_home: Path | None
    agent_os_workspace: Path | None
    close_grace_seconds: int
    # Explicit, temporary opt-in for a pre-existing broad OAuth profile.  It
    # is never inferred from CODEX_HOME or caller environment.
    legacy_oauth_profile: bool = False


@dataclass(frozen=True)
class SecuritySettings:
    require_manual_approval: bool
    allowed_product: str
    allow_withdrawal: bool
    allow_futures: bool
    allow_margin: bool
    allow_transfer: bool


@dataclass(frozen=True)
class Settings:
    version: int
    mode: str
    workspace: Path
    state_dir: Path
    telegram: TelegramSettings
    market: MarketSettings
    risk: RiskSettings
    openclaw: OpenClawSettings
    codex: CodexSettings
    security: SecuritySettings
    live: LiveSettings
    paper: PaperSettings
    sizing_policy: SizingPolicySettings
    scheduled_proposal_mode: str
    execution_ready: bool
    config_path: Path
    default_locale: str = "id"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "spotguard.db"


def _required_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _int(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _decimal(raw: dict[str, Any], key: str, minimum: str = "0") -> Decimal:
    try:
        value = decimal_value(raw.get(key), key)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if value < Decimal(minimum):
        raise ConfigError(f"{key} must be at least {minimum}")
    return value


def _reject_embedded_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                raise ConfigError(
                    f"{path}.{key} is not allowed; credentials belong in Codex/Agent OS OAuth, not this file"
                )
            _reject_embedded_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_embedded_secrets(child, f"{path}[{index}]")


def default_config_path() -> Path:
    explicit = os.environ.get("RISKPILOT_CONFIG") or os.environ.get("SPOTGUARD_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    project_root = os.environ.get("RISKPILOT_PROJECT_ROOT") or os.environ.get("SPOTGUARD_PROJECT_ROOT")
    if project_root:
        return Path(project_root) / "config.json"
    return Path.cwd() / "config.json"


def load_settings(path: str | Path | None = None, create_state: bool = True) -> Settings:
    config_path = Path(path).expanduser() if path else default_config_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"config not found at {config_path}; copy config.example.json to config.json first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    _reject_embedded_secrets(raw)

    version = raw.get("version")
    if isinstance(version, bool) or version not in {1, 2}:
        raise ConfigError("version must be 1 or 2")
    mode = str(raw.get("mode", "")).lower()
    if mode not in {"paper", "live"}:
        raise ConfigError("mode must be paper or live")

    # Legacy field names are mapped below, field-for-field.  Values are never
    # rewritten or increased in memory: the mode-specific ceilings remain
    # authoritative exactly as configured.

    workspace = Path(str(raw.get("workspace", "~/.openclaw/workspace"))).expanduser().resolve(strict=False)
    state_dir = Path(str(raw.get("state_dir", workspace / "state/spotguard"))).expanduser().resolve(strict=False)
    if create_state:
        try:
            state_dir = ensure_private_dir(state_dir)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    telegram_raw = _required_mapping(raw, "telegram")
    chat_id = str(telegram_raw.get("chat_id", ""))
    if not re.fullmatch(r"-?\d{5,20}", chat_id):
        raise ConfigError("telegram.chat_id must be a numeric Telegram chat id")
    channel = str(telegram_raw.get("channel", ""))
    if channel != "telegram":
        raise ConfigError("telegram.channel must be telegram")
    account_value = telegram_raw.get("account")
    if account_value is not None and not isinstance(account_value, str):
        raise ConfigError("telegram.account must be null or a string")
    telegram = TelegramSettings(
        enabled=_bool(telegram_raw, "enabled"),
        chat_id=chat_id,
        channel=channel,
        account=account_value,
    )

    market_raw = _required_mapping(raw, "market")
    base_url = str(market_raw.get("base_url", "")).rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in ALLOWED_MARKET_HOSTS:
        raise ConfigError("market.base_url must be an official HTTPS Binance API host")
    symbols_raw = market_raw.get("symbols")
    if not isinstance(symbols_raw, list) or not 1 <= len(symbols_raw) <= 1000:
        raise ConfigError("market.symbols must contain 1 to 1000 symbols")
    symbols: list[str] = []
    for item in symbols_raw:
        symbol = str(item).upper()
        if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
            raise ConfigError(f"invalid market symbol: {item}")
        if symbol not in symbols:
            symbols.append(symbol)
    interval = str(market_raw.get("interval", ""))
    if interval not in ALLOWED_INTERVALS:
        raise ConfigError(f"unsupported market.interval: {interval}")
    market = MarketSettings(
        base_url=base_url,
        symbols=tuple(symbols),
        interval=interval,
        lookback=_int(market_raw, "lookback", 60, 1000),
        request_timeout_seconds=_int(market_raw, "request_timeout_seconds", 2, 60),
        min_signal_score=_int(market_raw, "min_signal_score", 1, 100),
        candidate_ttl_minutes=_int(market_raw, "candidate_ttl_minutes", 1, 1440),
        candidate_cooldown_minutes=_int(market_raw, "candidate_cooldown_minutes", 0, 10080),
        max_entry_drift_pct=_decimal(market_raw, "max_entry_drift_pct", "0.01"),
        max_spread_pct=_decimal(market_raw, "max_spread_pct", "0.001"),
    )

    risk_raw = _required_mapping(raw, "risk")
    quote_asset = str(risk_raw.get("quote_asset", "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", quote_asset):
        raise ConfigError("risk.quote_asset is invalid")
    default_order_key = ("default_order_size_usdt" if "default_order_size_usdt" in risk_raw
                         else "default_quote_amount")
    risk = RiskSettings(
        quote_asset=quote_asset,
        default_quote_amount=_decimal(risk_raw, default_order_key, "0.01"),
        min_quote_amount=_decimal(risk_raw, "min_quote_amount", "0.01"),
        max_quote_per_trade=_decimal(risk_raw, "max_quote_per_trade", "0.01"),
        max_daily_quote=_decimal(risk_raw, "max_daily_quote", "0.01"),
        max_active_proposals=_int(risk_raw, "max_active_proposals", 1, 20),
        min_stop_distance_pct=_decimal(risk_raw, "min_stop_distance_pct", "0.01"),
        max_stop_distance_pct=_decimal(risk_raw, "max_stop_distance_pct", "0.01"),
        atr_stop_multiplier=_decimal(risk_raw, "atr_stop_multiplier", "0.01"),
        min_reward_risk=_decimal(risk_raw, "min_reward_risk", "1"),
        proposal_ttl_minutes=_int(risk_raw, "proposal_ttl_minutes", 1, 1440),
        execution_lease_seconds=_int(risk_raw, "execution_lease_seconds", 30, 900),
        max_live_arm_minutes=_int(risk_raw, "max_live_arm_minutes", 1, 10080),
        paper_fee_pct=_decimal(risk_raw, "paper_fee_pct", "0") if "paper_fee_pct" in risk_raw else Decimal("0.1"),
    )
    # Legacy shorthand defaults remain under risk; per-mode ceilings are
    # canonical under paper/live.
    if not risk.min_quote_amount <= risk.default_quote_amount <= risk.max_quote_per_trade:
        raise ConfigError("risk.default_quote_amount must be between min_quote_amount and max_quote_per_trade")
    if risk.max_daily_quote < risk.max_quote_per_trade:
        raise ConfigError("risk.max_daily_quote must be at least max_quote_per_trade")
    if risk.min_stop_distance_pct >= risk.max_stop_distance_pct:
        raise ConfigError("risk.min_stop_distance_pct must be lower than max_stop_distance_pct")
    for symbol in symbols:
        if not symbol.endswith(quote_asset):
            raise ConfigError(f"{symbol} does not use configured quote asset {quote_asset}")

    openclaw_raw = _required_mapping(raw, "openclaw")
    command = str(openclaw_raw.get("command", ""))
    if not command or Path(command).name != command and not Path(command).is_absolute():
        raise ConfigError("openclaw.command must be a command name or absolute path")
    owner_id = str(openclaw_raw.get("telegram_owner_id", ""))
    if not re.fullmatch(r"\d{5,20}", owner_id):
        raise ConfigError("openclaw.telegram_owner_id must be numeric")
    if not chat_id.startswith("-") and owner_id != chat_id:
        raise ConfigError("for a direct chat, telegram_owner_id must match telegram.chat_id")
    openclaw = OpenClawSettings(command=command, telegram_owner_id=owner_id)

    codex_value = raw.get("codex", {})
    if not isinstance(codex_value, dict):
        raise ConfigError("codex must be an object")
    codex_command = str(codex_value.get("command", "codex"))
    if not codex_command or (
        Path(codex_command).name != codex_command and not Path(codex_command).is_absolute()
    ):
        raise ConfigError("codex.command must be a command name or absolute path")
    codex_mcp_server = str(codex_value.get("mcp_server", "binance-marketdata"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", codex_mcp_server):
        raise ConfigError("codex.mcp_server is invalid")
    codex_endpoint = str(
        codex_value.get("endpoint", "https://agent.binance.com/mcp/agentic")
    )
    if codex_endpoint != "https://agent.binance.com/mcp/agentic":
        raise ConfigError("codex.endpoint must be the official Binance Agent OS endpoint")
    codex_timeout = codex_value.get("timeout_seconds", 90)
    if (
        isinstance(codex_timeout, bool)
        or not isinstance(codex_timeout, int)
        or not 30 <= codex_timeout <= 300
    ):
        raise ConfigError("codex.timeout_seconds must be an integer between 30 and 300")
    model_value = codex_value.get("model")
    if model_value is not None:
        if not isinstance(model_value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{2,80}", model_value):
            raise ConfigError("codex.model must be null or a valid model name")
    read_only_value = codex_value.get("read_only", True)
    if not isinstance(read_only_value, bool):
        raise ConfigError("codex.read_only must be true or false")
    if not read_only_value:
        raise ConfigError("codex.read_only must remain true in RiskPilot")
    close_grace = _int(codex_value, "close_grace_seconds", 0, 10) if "close_grace_seconds" in codex_value else 2
    legacy_oauth_profile = codex_value.get("legacy_oauth_profile", False)
    if not isinstance(legacy_oauth_profile, bool):
        raise ConfigError("codex.legacy_oauth_profile must be true or false")
    agent_os_home, agent_os_workspace = _dedicated_codex_profile(codex_value, legacy_oauth_profile)
    codex = CodexSettings(
        command=codex_command,
        mcp_server=codex_mcp_server,
        endpoint=codex_endpoint,
        timeout_seconds=codex_timeout,
        model=model_value,
        read_only=read_only_value,
        agent_os_home=agent_os_home,
        agent_os_workspace=agent_os_workspace,
        close_grace_seconds=close_grace,
        legacy_oauth_profile=legacy_oauth_profile,
    )

    security_raw = _required_mapping(raw, "security")
    security = SecuritySettings(
        require_manual_approval=_bool(security_raw, "require_manual_approval"),
        allowed_product=str(security_raw.get("allowed_product", "")).lower(),
        allow_withdrawal=_bool(security_raw, "allow_withdrawal"),
        allow_futures=_bool(security_raw, "allow_futures"),
        allow_margin=_bool(security_raw, "allow_margin"),
        allow_transfer=_bool(security_raw, "allow_transfer"),
    )
    if not security.require_manual_approval:
        raise ConfigError("security.require_manual_approval must remain true in RiskPilot")
    if security.allowed_product != "spot":
        raise ConfigError("security.allowed_product must remain spot")
    if any((security.allow_withdrawal, security.allow_futures, security.allow_margin, security.allow_transfer)):
        raise ConfigError("withdrawal, futures, margin, and transfer permissions must remain false")

    paper_raw = raw.get("paper", {})
    if not isinstance(paper_raw, dict):
        raise ConfigError("paper must be an object")
    paper = PaperSettings(
        initial_balance_usdt=Decimal(str(paper_raw.get("initial_balance_usdt", "1000"))),
        max_quote_per_entry_usdt=Decimal(str(paper_raw.get("max_quote_per_entry_usdt", paper_raw.get("max_quote_per_trade", "100")))),
        max_active_tranches=int(paper_raw.get("max_active_tranches", paper_raw.get("max_open_positions", 10))),
        max_economic_positions=int(paper_raw.get("max_economic_positions", 5)),
        max_open_exposure_usdt=Decimal(str(paper_raw.get("max_open_exposure_usdt", "500"))),
        max_successful_entries_per_utc_day=int(paper_raw.get("max_successful_entries_per_utc_day", 10)),
        max_risk_per_position_usdt=Decimal(str(paper_raw.get("max_risk_per_position_usdt", paper_raw.get("max_risk_per_trade_usdt", "2")))),
        max_aggregate_risk_usdt=Decimal(str(paper_raw.get("max_aggregate_risk_usdt", "4"))),
        daily_realized_loss_cap_usdt=Decimal(str(paper_raw.get("daily_realized_loss_cap_usdt", "5"))),
        close_proposal_ttl_seconds=int(paper_raw.get("close_proposal_ttl_seconds", 300)),
        slippage_pct=Decimal(str(paper_raw.get("slippage_pct", "0.05"))))
    paper_numbers = (paper.initial_balance_usdt, paper.max_quote_per_entry_usdt, paper.max_open_exposure_usdt,
                     paper.max_risk_per_position_usdt, paper.max_aggregate_risk_usdt,
                     paper.daily_realized_loss_cap_usdt)
    if (any(not value.is_finite() or value <= 0 for value in paper_numbers)
            or paper.max_active_tranches < 1 or paper.max_economic_positions < 1
            or paper.max_successful_entries_per_utc_day < 1
            or paper.max_open_exposure_usdt < paper.max_quote_per_entry_usdt
            or paper.max_aggregate_risk_usdt < paper.max_risk_per_position_usdt
            or not 1 <= paper.close_proposal_ttl_seconds <= 3600
            or not paper.slippage_pct.is_finite() or paper.slippage_pct < 0):
        raise ConfigError("paper limits are invalid")

    live_raw = raw.get("live", {})
    if not isinstance(live_raw, dict):
        raise ConfigError("live must be an object")
    legacy_live_symbols = live_raw.get("allowed_symbols")
    if legacy_live_symbols is not None and tuple(str(x).upper() for x in legacy_live_symbols) != tuple(symbols):
        raise ConfigError("live.allowed_symbols, when present for compatibility, must match market.symbols")
    allowed_live = tuple(symbols)
    armed_config = live_raw.get("armed", live_raw.get("arm", False))
    live = LiveSettings(enabled=live_raw.get("enabled", False), arm=armed_config,
        max_quote_per_entry_usdt=Decimal(str(live_raw.get("max_quote_per_entry_usdt", live_raw.get("max_live_trade_usdt", "100")))),
        allowed_symbols=allowed_live,
        max_active_tranches=int(live_raw.get("max_active_tranches", 10)),
        max_economic_positions=int(live_raw.get("max_economic_positions", live_raw.get("max_open_positions", 5))),
        max_open_exposure_usdt=Decimal(str(live_raw.get("max_open_exposure_usdt", "500"))),
        min_free_reserve_usdt=Decimal(str(live_raw.get("min_free_reserve_usdt", "8"))),
        max_risk_per_position_usdt=Decimal(str(live_raw.get("max_risk_per_position_usdt", live_raw.get("max_risk_per_trade_usdt", "2")))),
        max_aggregate_risk_usdt=Decimal(str(live_raw.get("max_aggregate_risk_usdt", "4"))),
        max_successful_entries_per_utc_day=int(live_raw.get("max_successful_entries_per_utc_day", 10)),
        max_pending_proposals=int(live_raw.get("max_pending_proposals", 1)),
        approval_ttl_seconds=int(live_raw.get("approval_ttl_seconds", 60)),
        daily_realized_loss_cap_usdt=Decimal(str(live_raw.get("daily_realized_loss_cap_usdt", live_raw.get("daily_loss_cap_usdt", "5")))),
        weekly_loss_cap_usdt=Decimal(str(live_raw.get("weekly_loss_cap_usdt", "20"))),
        protective_orders_available=live_raw.get("protective_orders_available", False))
    if not isinstance(live.enabled, bool) or not isinstance(live.arm, bool):
        raise ConfigError("live.enabled and live.armed must be true or false")
    live_numbers = (live.max_quote_per_entry_usdt, live.max_open_exposure_usdt, live.min_free_reserve_usdt,
                    live.max_risk_per_position_usdt, live.max_aggregate_risk_usdt,
                    live.daily_realized_loss_cap_usdt, live.weekly_loss_cap_usdt)
    if (any(not value.is_finite() or value <= 0 for value in live_numbers)
            or live.max_active_tranches < 1 or live.max_economic_positions < 1
            or live.max_successful_entries_per_utc_day < 1 or live.max_pending_proposals < 1
            or live.max_open_exposure_usdt < live.max_quote_per_entry_usdt
            or live.max_aggregate_risk_usdt < live.max_risk_per_position_usdt
            or live.weekly_loss_cap_usdt < live.daily_realized_loss_cap_usdt):
        raise ConfigError("live limits are invalid")
    if not 1 <= live.approval_ttl_seconds <= 180:
        raise ConfigError("live approval TTL must be no more than 180 seconds")
    if live.arm:
        raise ConfigError("live.arm must remain false in config; use the VPS-local live arm command")
    if mode == "live" and not live.enabled:
        raise ConfigError("live mode requires live.enabled=true")
    scheduled_proposal_mode = str(raw.get("scheduled_proposal_mode", "paper")).lower()
    if scheduled_proposal_mode not in {"paper", "live"}:
        raise ConfigError("scheduled_proposal_mode must be paper or live")
    execution_ready = raw.get("execution_ready", False)
    # Older dormant configs used null. Treat it as the same fail-closed value
    # so the local admin migration can validate and atomically write false.
    if execution_ready is None:
        execution_ready = False
    if execution_ready is not False:
        raise ConfigError("execution_ready must remain false in RiskPilot")
    presentation = raw.get("presentation", {})
    if not isinstance(presentation, dict) or presentation.get("default_locale", "id") not in {"en", "id"}:
        raise ConfigError("presentation.default_locale must be en or id")
    if codex.legacy_oauth_profile and (
            live.enabled or live.arm or execution_ready or scheduled_proposal_mode != "paper"):
        raise ConfigError("legacy OAuth profile is PAPER-only and cannot be used with LIVE or scheduled non-PAPER mode")

    try:
        sizing_policy = load_sizing_policy(
            raw.get("sizing_policy"),
            quote_asset=risk.quote_asset,
            legacy_reference_equity=paper.initial_balance_usdt,
            risk_value=risk_raw,
            capital_value=raw.get("capital"),
            operations_value=raw.get("operations"),
            execution_value=raw.get("execution"),
            absolute_safety_caps_value=raw.get("absolute_safety_caps"),
        )
    except SizingPolicyConfigError as exc:
        raise ConfigError(str(exc)) from exc

    return Settings(
        version=version,
        mode=mode,
        workspace=workspace,
        state_dir=state_dir,
        telegram=telegram,
        market=market,
        risk=risk,
        openclaw=openclaw,
        codex=codex,
        security=security,
        live=live,
        paper=paper,
        sizing_policy=sizing_policy,
        scheduled_proposal_mode=scheduled_proposal_mode,
        execution_ready=execution_ready,
        config_path=config_path.resolve(strict=False),
        default_locale=presentation.get("default_locale", "id"),
    )


def initialize_config(
    destination: Path,
    source: Path,
    chat_id: str | None = None,
    workspace: Path | None = None,
) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise ConfigError(f"refusing to overwrite existing config: {destination}")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if chat_id:
        if not re.fullmatch(r"\d{5,20}", chat_id):
            raise ConfigError("chat id must be numeric")
        raw["telegram"]["chat_id"] = chat_id
        raw["openclaw"]["telegram_owner_id"] = chat_id
    if workspace:
        resolved_workspace = workspace.expanduser().resolve(strict=False)
        raw["workspace"] = str(resolved_workspace)
        raw["state_dir"] = str(resolved_workspace / "state" / "spotguard")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    try:
        destination.chmod(0o600)
    except PermissionError:
        pass
    return destination


def _dedicated_codex_profile(raw: dict[str, Any], legacy_oauth_profile: bool = False) -> tuple[Path | None, Path | None]:
    """Validate only an explicit, non-default Codex profile; never infer one.

    `None` is a deliberately disabled example value.  It is not a request to
    use `$CODEX_HOME`, `~/.codex`, or any previously logged-in profile.
    """
    home_value = raw.get("agent_os_home")
    workspace_value = raw.get("agent_os_workspace")
    if home_value is None and workspace_value is None:
        return None, None
    if not isinstance(home_value, str) or not isinstance(workspace_value, str):
        raise ConfigError("codex.agent_os_home and codex.agent_os_workspace must both be absolute paths or null")
    home = Path(home_value)
    workspace = Path(workspace_value)
    if not home.is_absolute() or not workspace.is_absolute():
        raise ConfigError("dedicated Codex paths must be absolute")
    if home.is_symlink() or workspace.is_symlink() or not home.is_dir() or not workspace.is_dir():
        raise ConfigError("dedicated Codex paths must be existing non-symlink directories")
    default_home = Path.home() / ".codex"
    is_default_home = home == default_home or home.resolve() == default_home.resolve()
    if is_default_home:
        if not legacy_oauth_profile:
            raise ConfigError("codex.agent_os_home must not use the default ~/.codex profile without explicit legacy_oauth_profile")
        # The old OAuth directory is not a neutral execution workspace.  Keep
        # process cwd in a separately dedicated directory so Codex cannot use
        # project rules or files from the broad profile directory.
        try:
            workspace.relative_to(home)
        except ValueError:
            pass
        else:
            raise ConfigError("legacy OAuth profile requires a neutral workspace outside ~/.codex")
    else:
        try:
            workspace.relative_to(home)
        except ValueError as exc:
            raise ConfigError("codex.agent_os_workspace must be inside codex.agent_os_home") from exc
    config_toml = home / "config.toml"
    if config_toml.is_symlink() or not config_toml.is_file():
        raise ConfigError("dedicated CODEX_HOME must contain a regular config.toml")
    return home, workspace


def openclaw_available(settings: Settings) -> bool:
    command = settings.openclaw.command
    return bool(Path(command).exists()) if Path(command).is_absolute() else shutil.which(command) is not None


def codex_available(settings: Settings) -> bool:
    command = settings.codex.command
    return bool(Path(command).exists()) if Path(command).is_absolute() else shutil.which(command) is not None

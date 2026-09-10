from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .strategy import Signal
from .util import canonical_json, isoformat, parse_time, utcnow


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path, initial_paper_balance: Decimal = Decimal("28")) -> None:
        self.path = path
        self.initial_paper_balance = initial_paper_balance
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=8.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=8000")
        return connection

    def presentation_locale(self, scope: str, identifier: str | None) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT locale FROM presentation_locales WHERE scope=? AND identifier=?", (scope, identifier)).fetchone()
        return row[0] if row else None

    def remember_chat_locale(self, chat_id: str, locale: str) -> None:
        with self.transaction() as connection:
            connection.execute("INSERT INTO presentation_locales VALUES('chat',?,?) ON CONFLICT(scope,identifier) DO UPDATE SET locale=excluded.locale", (chat_id, locale))

    def close_locale_entity(self, position_id: str, code_hash: str) -> str | None:
        # Include terminal proposals: replay/expiry must keep the original locale.
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM paper_close_proposals WHERE position_id=? AND code_hash=? ORDER BY created_at DESC LIMIT 1", (position_id, code_hash)).fetchone()
        return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS presentation_locales (
                    scope TEXT NOT NULL CHECK(scope IN ('chat','proposal')),
                    identifier TEXT NOT NULL,
                    locale TEXT NOT NULL CHECK(locale IN ('en','id')),
                    PRIMARY KEY(scope,identifier)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side = 'BUY'),
                    score INTEGER NOT NULL,
                    price REAL NOT NULL,
                    candle_close_time INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ACTIVE','PROPOSED','DISMISSED','EXPIRED')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    notified_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_status_created
                    ON candidates(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES candidates(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side = 'BUY'),
                    product TEXT NOT NULL CHECK (product = 'SPOT'),
                    order_type TEXT NOT NULL CHECK (order_type = 'MARKET'),
                    quote_amount TEXT NOT NULL,
                    entry_reference TEXT NOT NULL,
                    stop_reference TEXT NOT NULL,
                    take_profit_reference TEXT NOT NULL,
                    reward_risk TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    approval_token_hash TEXT NOT NULL,
                    confirmation_code_hash TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING','EXECUTING','EXECUTED','RECONCILE','REJECTED','EXPIRED','FAILED')
                    ),
                    mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT,
                    approved_by TEXT,
                    execution_lease_hash TEXT,
                    execution_lease_expires_at TEXT,
                    executed_at TEXT,
                    execution_order_id TEXT,
                    execution_status TEXT,
                    execution_summary_json TEXT,
                    failure_reason TEXT,
                    notified_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_proposals_status_created
                    ON proposals(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    initial_balance_usdt TEXT NOT NULL,
                    free_usdt TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    paid_fees_usdt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side='BUY'),
                    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSING','CLOSED')),
                    entry_reference TEXT NOT NULL,
                    average_fill_price TEXT NOT NULL,
                    gross_quantity TEXT NOT NULL,
                    net_quantity TEXT NOT NULL,
                    quote_spent TEXT NOT NULL,
                    entry_fee_base TEXT NOT NULL,
                    final_stop TEXT NOT NULL,
                    final_target TEXT NOT NULL,
                    risk_amount TEXT NOT NULL,
                    net_expected_reward_risk TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    last_checked_at TEXT NOT NULL,
                    exit_reason TEXT,
                    exit_price TEXT,
                    exit_fee TEXT,
                    gross_proceeds TEXT,
                    net_proceeds TEXT,
                    realized_pnl TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, opened_at);
                CREATE TABLE IF NOT EXISTS paper_close_proposals (
                    id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES paper_positions(id),
                    status TEXT NOT NULL CHECK(status IN ('PENDING','EXECUTING','EXECUTED','REJECTED','EXPIRED','FAILED')),
                    token_hash TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    entity_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
                """
            )
            now = isoformat()
            connection.execute("INSERT OR IGNORE INTO paper_account(id,initial_balance_usdt,free_usdt,realized_pnl,paid_fees_usdt,created_at,updated_at) VALUES(1,?,?,?,?,?,?)", (str(self.initial_paper_balance), str(self.initial_paper_balance), "0", "0", now, now))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(proposals)")}
            if "confirmation_code_hash" not in columns:
                connection.execute("ALTER TABLE proposals ADD COLUMN confirmation_code_hash TEXT")
            close_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_close_proposals)")}
            for name, kind in (("payload_json", "TEXT"), ("payload_hash", "TEXT"),
                               ("requested_percentage", "TEXT"), ("close_quantity", "TEXT"),
                               ("reference_bid", "TEXT"), ("failure_reason", "TEXT")):
                if name not in close_columns:
                    connection.execute(f"ALTER TABLE paper_close_proposals ADD COLUMN {name} {kind}")
        try:
            self.path.chmod(0o600)
        except PermissionError:
            pass

    @staticmethod
    def _decode_candidate(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    @staticmethod
    def _decode_proposal(row: sqlite3.Row | None, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["canonical"] = json.loads(result["canonical_json"])
        # Legacy schema only permits BUY/MARKET columns; immutable canonical terms
        # carry the actual live operation and are the sole execution authority.
        result["side"] = result["canonical"].get("side", result["side"])
        result["order_type"] = result["canonical"].get("order_type", result["order_type"])
        if result.get("execution_summary_json"):
            result["execution_summary"] = json.loads(result["execution_summary_json"])
        else:
            result["execution_summary"] = None
        for key in ("execution_summary_json",):
            result.pop(key, None)
        if not include_private:
            for key in ("approval_token_hash", "confirmation_code_hash", "execution_lease_hash"):
                result.pop(key, None)
        return result

    def add_event(self, kind: str, entity_id: str | None, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(kind, entity_id, payload_json, created_at) VALUES(?,?,?,?)",
                (kind, entity_id, canonical_json(payload), isoformat()),
            )

    def latest_event(self, kind: str) -> dict[str, Any] | None:
        """Return one audit payload; callers must store only redacted values."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json, created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise LedgerError("latest audit event payload is malformed")
        # Probe observation time is part of the immutable event payload; the
        # database timestamp remains useful for audit ordering but must not
        # turn a stale successful probe into a fresh one after restart.
        payload.setdefault("observed_at", row["created_at"])
        payload["recorded_at"] = row["created_at"]
        return payload

    def events_by_kind(self, kind: str) -> list[dict[str, Any]]:
        """Return redacted append-only event payloads for local accounting."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json, created_at FROM events WHERE kind=? ORDER BY id",
                (kind,),
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise LedgerError("audit event payload is malformed")
            payload.setdefault("recorded_at", row["created_at"])
            result.append(payload)
        return result

    def create_candidate(self, signal: Signal, ttl_minutes: int) -> tuple[dict[str, Any], bool]:
        now = utcnow()
        values = (
            signal.candidate_id,
            signal.fingerprint,
            signal.symbol,
            signal.interval,
            signal.side,
            signal.score,
            signal.price,
            signal.candle_close_time,
            canonical_json(list(signal.reasons)),
            canonical_json(signal.metrics),
            "ACTIVE",
            isoformat(now),
            isoformat(now + timedelta(minutes=ttl_minutes)),
        )
        created = False
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO candidates(
                    id,fingerprint,symbol,interval,side,score,price,candle_close_time,
                    reasons_json,metrics_json,status,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            created = cursor.rowcount == 1
            row = connection.execute("SELECT * FROM candidates WHERE fingerprint=?", (signal.fingerprint,)).fetchone()
        candidate = self._decode_candidate(row)
        if candidate is None:
            raise LedgerError("candidate insert failed")
        if created:
            self.add_event("candidate.created", candidate["id"], {"score": candidate["score"]})
        return candidate, created

    def has_recent_candidate(self, symbol: str, side: str, since: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM candidates
                WHERE symbol=? AND side=? AND created_at>=?
                  AND status IN ('ACTIVE','PROPOSED')
                LIMIT 1
                """,
                (symbol, side, since),
            ).fetchone()
        return row is not None

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        candidate = self._decode_candidate(row)
        if candidate is None:
            raise LedgerError(f"candidate not found: {candidate_id}")
        return candidate

    def list_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_candidate(row) for row in rows if row is not None]  # type: ignore[misc]

    def mark_candidate_notified(self, candidate_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE candidates SET notified_at=? WHERE id=?", (isoformat(), candidate_id))

    def dismiss_candidate(self, candidate_id: str, reason: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise LedgerError(f"candidate not found: {candidate_id}")
            if row["status"] not in {"ACTIVE", "PROPOSED"}:
                raise LedgerError(f"candidate cannot be dismissed from {row['status']}")
            connection.execute("UPDATE candidates SET status='DISMISSED' WHERE id=?", (candidate_id,))
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("candidate.dismissed", candidate_id, canonical_json({"reason": reason}), isoformat()),
            )
        return self.get_candidate(candidate_id)

    def _expire_stale_locked(self, connection: sqlite3.Connection, now=None) -> dict[str, int]:
        current = now or utcnow()
        event_time = isoformat(current)
        expired = {"proposals": 0, "candidates": 0}
        proposal_rows = connection.execute("SELECT id,expires_at FROM proposals WHERE status=\x27PENDING\x27 AND approved_at IS NULL AND executed_at IS NULL").fetchall()
        for row in proposal_rows:
            try:
                stale = parse_time(row["expires_at"]) <= current
            except (TypeError, ValueError):
                stale = False
            if stale:
                connection.execute("UPDATE proposals SET status=\x27EXPIRED\x27 WHERE id=? AND status=\x27PENDING\x27 AND approved_at IS NULL AND executed_at IS NULL", (row["id"],))
                if connection.execute("SELECT changes()").fetchone()[0]:
                    expired["proposals"] += 1
                    connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("proposal.auto_expired", row["id"], canonical_json({"previous_status": "PENDING", "reason": "ttl"}), event_time))
        candidate_rows = connection.execute("SELECT id,expires_at FROM candidates WHERE status=\x27ACTIVE\x27").fetchall()
        for row in candidate_rows:
            try:
                stale = parse_time(row["expires_at"]) <= current
            except (TypeError, ValueError):
                stale = False
            if stale:
                connection.execute("UPDATE candidates SET status=\x27EXPIRED\x27 WHERE id=? AND status=\x27ACTIVE\x27", (row["id"],))
                if connection.execute("SELECT changes()").fetchone()[0]:
                    expired["candidates"] += 1
                    connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("candidate.auto_expired", row["id"], canonical_json({"previous_status": "ACTIVE", "reason": "ttl"}), event_time))
        return expired

    def _recover_expired_paper_executions_locked(self, connection: sqlite3.Connection,
                                                   proposal_id: str | None = None,
                                                   failure_reason: str | None = None) -> list[dict[str, str]]:
        now = utcnow(); recovered: list[dict[str, str]] = []
        query = "SELECT * FROM proposals WHERE mode='paper' AND status='EXECUTING'"
        params: tuple[Any, ...] = ()
        if proposal_id is not None:
            query += " AND id=?"; params = (proposal_id,)
        for row in connection.execute(query, params).fetchall():
            expiry = row["execution_lease_expires_at"]
            if not isinstance(expiry, str) or parse_time(expiry) > now:
                continue
            fill = connection.execute("SELECT id FROM paper_positions WHERE proposal_id=?", (row["id"],)).fetchone()
            if fill is not None:
                status = "EXECUTED"; reason = "idempotent PAPER position already exists"
                connection.execute("""UPDATE proposals SET status='EXECUTED',execution_status='FILLED',
                    executed_at=COALESCE(executed_at,?), execution_order_id=COALESCE(execution_order_id,?),
                    failure_reason=NULL,execution_lease_hash=NULL,execution_lease_expires_at=NULL WHERE id=?""",
                    (isoformat(now), f"paper-{row['id'][2:]}", row["id"]))
            else:
                status = "FAILED"
                reason = failure_reason or row["failure_reason"] or "expired PAPER execution lease; no idempotent fill exists"
                connection.execute("""UPDATE proposals SET status='FAILED',failure_reason=?,
                    execution_lease_hash=NULL,execution_lease_expires_at=NULL WHERE id=?""", (reason, row["id"]))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("paper.execution_recovered", row["id"], canonical_json({"status": status, "reason": reason,
                 "fill_found": fill is not None}), isoformat(now)))
            recovered.append({"proposal_id": row["id"], "status": status, "reason": reason})
        return recovered

    def recover_expired_paper_executions(self, proposal_id: str | None = None,
                                         failure_reason: str | None = None) -> list[dict[str, str]]:
        with self.transaction() as connection:
            return self._recover_expired_paper_executions_locked(connection, proposal_id, failure_reason)

    def expire_stale_active_proposals(self) -> int:
        with self.transaction() as connection:
            result = self._expire_stale_locked(connection)
            self._recover_expired_paper_executions_locked(connection)
        return result["proposals"]

    def active_proposal_count(self) -> int:
        with self.transaction() as connection:
            self._expire_stale_locked(connection)
            self._recover_expired_paper_executions_locked(connection)
            row = connection.execute("SELECT COUNT(*) AS count FROM proposals WHERE status IN (\x27PENDING\x27,\x27EXECUTING\x27)").fetchone()
        return int(row["count"])

    def active_proposal_count_read_only(self) -> int:
        """Project expiry/recovery semantics without applying their writes."""
        now = utcnow()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,expires_at,execution_lease_expires_at FROM proposals "
                "WHERE status IN ('PENDING','EXECUTING')"
            ).fetchall()
        count = 0
        for row in rows:
            expiry = row["expires_at"] if row["status"] == "PENDING" else row["execution_lease_expires_at"]
            try:
                stale = isinstance(expiry, str) and parse_time(expiry) <= now
            except (TypeError, ValueError):
                stale = False
            if not stale:
                count += 1
        return count

    def has_active_paper_buy_read_only(self, symbol: str) -> bool:
        now = utcnow()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,expires_at,execution_lease_expires_at FROM proposals "
                "WHERE mode='paper' AND symbol=? AND status IN ('PENDING','EXECUTING')", (symbol,)
            ).fetchall()
        for row in rows:
            expiry = row["expires_at"] if row["status"] == "PENDING" else row["execution_lease_expires_at"]
            try:
                if not isinstance(expiry, str) or parse_time(expiry) > now:
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def active_proposals_status(self) -> list[dict[str, Any]]:
        now = utcnow()
        with self.transaction() as connection:
            self._expire_stale_locked(connection)
            self._recover_expired_paper_executions_locked(connection)
            rows = connection.execute("SELECT id,status,mode,symbol,created_at,expires_at,approved_at,executed_at,execution_status FROM proposals WHERE status IN (\x27PENDING\x27,\x27EXECUTING\x27) ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["stale"] = parse_time(item["expires_at"]) <= now if item["status"] == "PENDING" else False
            except (TypeError, ValueError):
                item["stale"] = None
            result.append(item)
        return result

    def daily_committed_quote(self, day_prefix: str) -> Decimal:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT quote_amount FROM proposals
                WHERE substr(COALESCE(approved_at, created_at),1,10)=?
                  AND status IN ('EXECUTING','EXECUTED','RECONCILE')
                """,
                (day_prefix,),
            ).fetchall()
        return sum((Decimal(row["quote_amount"]) for row in rows), Decimal("0"))

    def create_proposal(
        self,
        values: dict[str, Any],
        token_hash: str,
        max_active_proposals: int,
        confirmation_code_hash: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            self._expire_stale_locked(connection)
            self._recover_expired_paper_executions_locked(connection)
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM proposals WHERE status IN ('PENDING','EXECUTING')"
            ).fetchone()["count"]
            if int(active) >= max_active_proposals:
                raise LedgerError("maximum active proposal count has been reached")
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE id=?", (values["candidate_id"],)
            ).fetchone()
            if candidate is None:
                raise LedgerError(f"candidate not found: {values['candidate_id']}")
            if candidate["status"] != "ACTIVE":
                raise LedgerError(f"candidate is not active: {candidate['status']}")
            connection.execute(
                """
                INSERT INTO proposals(
                    id,candidate_id,symbol,side,product,order_type,quote_amount,
                    entry_reference,stop_reference,take_profit_reference,reward_risk,
                    rationale,canonical_json,approval_token_hash,confirmation_code_hash,status,mode,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    values["id"], values["candidate_id"], values["symbol"], values["side"],
                    values["product"], "MARKET", values["quote_amount"],
                    values["entry_reference"], values["stop_reference"], values["take_profit_reference"],
                    values["reward_risk"], values["rationale"], values["canonical_json"], token_hash, confirmation_code_hash,
                    "PENDING", values["mode"], values["created_at"], values["expires_at"],
                ),
            )
            connection.execute("UPDATE candidates SET status='PROPOSED' WHERE id=?", (values["candidate_id"],))
            if values.get("locale"):
                connection.execute("INSERT INTO presentation_locales VALUES('proposal',?,?)", (values["id"], values["locale"]))
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("proposal.created", values["id"], canonical_json({"candidate_id": values["candidate_id"]}), isoformat()),
            )
        return self.get_proposal(values["id"])

    def get_proposal(self, proposal_id: str, include_private: bool = False) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        proposal = self._decode_proposal(row, include_private=include_private)
        if proposal is None:
            raise LedgerError(f"proposal not found: {proposal_id}")
        return proposal

    def list_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_proposal(row) for row in rows if row is not None]  # type: ignore[misc]

    def mark_proposal_notified(self, proposal_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE proposals SET notified_at=? WHERE id=?", (isoformat(), proposal_id))

    def reject_proposal(self, proposal_id: str, token_hash: str, actor: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] != "PENDING":
                raise LedgerError(f"proposal cannot be rejected from {row['status']}")
            if row["approval_token_hash"] != token_hash:
                raise LedgerError("invalid proposal token")
            connection.execute("UPDATE proposals SET status='REJECTED' WHERE id=?", (proposal_id,))
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("proposal.rejected", proposal_id, canonical_json({"actor": actor}), isoformat()),
            )
        return self.get_proposal(proposal_id)

    def reject_proposal_by_policy(self, proposal_id: str, reason: str) -> dict[str, Any]:
        """Terminalize an invalid immutable proposal without approval credentials."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] not in {"PENDING", "EXECUTING"}:
                raise LedgerError(
                    f"proposal cannot be policy-rejected from {row['status']}"
                )
            connection.execute(
                """UPDATE proposals SET status='REJECTED',failure_reason=?,
                   execution_lease_hash=NULL,execution_lease_expires_at=NULL WHERE id=?""",
                (reason, proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("proposal.policy_rejected", proposal_id,
                 canonical_json({"reason": reason}), isoformat()),
            )
        return self.get_proposal(proposal_id)

    def claim_proposal(
        self,
        proposal_id: str,
        token_hash: str,
        actor: str,
        lease_hash: str,
        lease_expires_at: str,
        day_prefix: str,
        max_daily_quote: str | None,
    ) -> dict[str, Any]:
        now = isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] != "PENDING":
                raise LedgerError(f"proposal is not claimable: {row['status']}")
            if row["expires_at"] <= now:
                connection.execute("UPDATE proposals SET status='EXPIRED' WHERE id=?", (proposal_id,))
                raise LedgerError("proposal has expired")
            if row["approval_token_hash"] != token_hash:
                raise LedgerError("invalid approval token")
            committed_rows = connection.execute(
                """
                SELECT quote_amount FROM proposals
                WHERE substr(COALESCE(approved_at, created_at),1,10)=?
                  AND status IN ('EXECUTING','EXECUTED','RECONCILE')
                """,
                (day_prefix,),
            ).fetchall()
            committed = sum((Decimal(item["quote_amount"]) for item in committed_rows), Decimal("0"))
            if (row["mode"] == "live" and max_daily_quote is not None
                    and committed + Decimal(row["quote_amount"]) > Decimal(max_daily_quote)):
                raise LedgerError("proposal would exceed the current daily quote limit")
            connection.execute(
                """
                UPDATE proposals
                SET status='EXECUTING',approved_at=?,approved_by=?,execution_lease_hash=?,
                    execution_lease_expires_at=?
                WHERE id=?
                """,
                (now, actor, lease_hash, lease_expires_at, proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("proposal.claimed", proposal_id, canonical_json({"actor": actor}), now),
            )
        return self.get_proposal(proposal_id, include_private=True)

    def claim_proposal_by_confirmation_code(
        self, proposal_id: str, code_hash: str, actor: str, lease_hash: str,
        lease_expires_at: str, day_prefix: str, max_daily_quote: str,
    ) -> dict[str, Any]:
        """Atomically claim PAPER using its independent one-time text credential."""
        now = isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["mode"] != "paper":
                raise LedgerError("text confirmation is paper-only")
            if row["status"] != "PENDING":
                raise LedgerError(f"proposal is not claimable: {row['status']}")
            if parse_time(row["expires_at"]) <= utcnow():
                connection.execute("UPDATE proposals SET status='EXPIRED' WHERE id=?", (proposal_id,))
                raise LedgerError("proposal has expired")
            if not isinstance(row["confirmation_code_hash"], str) or not secrets.compare_digest(row["confirmation_code_hash"], code_hash):
                raise LedgerError("invalid paper confirmation code")
            connection.execute(
                "UPDATE proposals SET status='EXECUTING',approved_at=?,approved_by=?,execution_lease_hash=?,execution_lease_expires_at=? WHERE id=?",
                (now, actor, lease_hash, lease_expires_at, proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("proposal.claimed", proposal_id, canonical_json({"actor": actor, "authorization_method": "paper_confirmation_code"}), now),
            )
        return self.get_proposal(proposal_id, include_private=True)

    def finish_execution(
        self,
        proposal_id: str,
        lease_hash: str,
        final_status: str,
        order_id: str,
        execution_status: str,
        summary: dict[str, Any],
        allow_legacy_unresolved: bool = False,
    ) -> dict[str, Any]:
        if final_status not in {"EXECUTED", "RECONCILE"}:
            raise LedgerError("invalid final execution status")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] != "EXECUTING" and not (
                    allow_legacy_unresolved and row["status"] == "EXECUTED"
                    and row["execution_status"] in {"EXEC_STARTED", "EXECUTING"}):
                raise LedgerError(f"proposal is not executing: {row['status']}")
            if row["execution_lease_hash"] != lease_hash:
                raise LedgerError("invalid execution lease")
            connection.execute(
                """
                UPDATE proposals SET status=?,executed_at=?,execution_order_id=?,
                    execution_status=?,execution_summary_json=? WHERE id=?
                """,
                (final_status, isoformat(), order_id, execution_status, canonical_json(summary), proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("execution.completed", proposal_id, canonical_json({"status": final_status, "order_id": order_id}), isoformat()),
            )
        return self.get_proposal(proposal_id)

    def record_execution_submitted(
        self, proposal_id: str, lease_hash: str, order_id: str,
        execution_status: str, summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist exchange acceptance while the parent order is not filled."""
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] != "EXECUTING" or row["execution_lease_hash"] != lease_hash:
                raise LedgerError("proposal is not executing")
            now = isoformat()
            connection.execute(
                "UPDATE proposals SET execution_order_id=?, execution_status=?, execution_summary_json=? WHERE id=?",
                (order_id, execution_status, canonical_json(summary), proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("execution.submitted", proposal_id, canonical_json({"status": "EXECUTING", "order_id": order_id}), now),
            )
        return self.get_proposal(proposal_id)

    def mark_live_execution_unreconcilable(
        self, proposal_id: str, reason: str, summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a local, fail-closed accounting state without exchange I/O."""
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["mode"] != "live":
                raise LedgerError("unreconcilable execution requires a LIVE proposal")
            connection.execute(
                "UPDATE proposals SET status='RECONCILE', execution_status='RECONCILE', execution_summary_json=? WHERE id=?",
                (canonical_json({**(json.loads(row["execution_summary_json"]) if row["execution_summary_json"] else {}),
                                 **summary, "accounting_status": "RECONCILE", "accounting_reason": reason}), proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("execution.reconciliation_required", proposal_id,
                 canonical_json({"reason": reason, "accounting_status": "RECONCILE"}), isoformat()),
            )
        return self.get_proposal(proposal_id)

    def fail_execution(self, proposal_id: str, lease_hash: str, reason: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LedgerError(f"proposal not found: {proposal_id}")
            if row["status"] != "EXECUTING":
                raise LedgerError(f"proposal is not executing: {row['status']}")
            if row["execution_lease_hash"] != lease_hash:
                raise LedgerError("invalid execution lease")
            connection.execute(
                "UPDATE proposals SET status='FAILED',failure_reason=?,execution_lease_hash=NULL,execution_lease_expires_at=NULL WHERE id=?",
                (reason, proposal_id),
            )
            connection.execute(
                "INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("execution.failed", proposal_id, canonical_json({"reason": reason}), isoformat()),
            )
        return self.get_proposal(proposal_id)

    def paper_balance(self) -> dict[str, Any]:
        with self.connect() as connection:
            account = dict(connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            rows = connection.execute("SELECT symbol,net_quantity,quote_spent FROM paper_positions WHERE status IN ('OPEN','CLOSING')").fetchall()
        assets: dict[str, str] = {}
        locked = Decimal("0")
        for row in rows:
            asset = row["symbol"][:-4] if row["symbol"].endswith("USDT") else row["symbol"]
            assets[asset] = str(Decimal(assets.get(asset, "0")) + Decimal(row["net_quantity"]))
            locked += Decimal(row["quote_spent"])
        return {**account, "locked_usdt": str(locked), "assets": assets,
                "open_positions": len({row["symbol"] for row in rows}), "active_tranches": len(rows)}

    def backup(self, label: str) -> Path:
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        source_parent = self.path.resolve().parent
        target = None
        for suffix in range(100):
            filename = f"spotguard-{label}-{stamp}"
            if suffix:
                filename += f"-{suffix}"
            candidate = self.path.with_name(filename + ".sqlite3")
            if candidate.resolve().parent != source_parent:
                raise LedgerError("safe SQLite backup target is unavailable")
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                target = candidate
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise LedgerError("safe SQLite backup target is unavailable") from exc
        if target is None:
            raise LedgerError("safe SQLite backup target is unavailable")
        source = sqlite3.connect(self.path); destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise LedgerError("SQLite backup integrity check failed")
        finally:
            destination.close(); source.close()
        target.chmod(0o600)
        return target

    def reset_paper_account(self, new_balance: Decimal, reason: str) -> dict[str, Any]:
        """Start a clean PAPER epoch without deleting historical rows or events."""
        if new_balance <= 0:
            raise LedgerError("paper reset balance must be positive")
        now = isoformat()
        with self.transaction() as connection:
            account = dict(connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            positions = connection.execute("SELECT id,symbol,quote_spent FROM paper_positions WHERE status IN ('OPEN','CLOSING')").fetchall()
            buys = connection.execute("SELECT id FROM proposals WHERE mode='paper' AND status IN ('PENDING','EXECUTING','RECONCILE')").fetchall()
            closes = connection.execute("SELECT id FROM paper_close_proposals WHERE status IN ('PENDING','EXECUTING')").fetchall()
            if (Decimal(account["initial_balance_usdt"]) == new_balance and Decimal(account["free_usdt"]) == new_balance
                    and Decimal(account["realized_pnl"]) == 0 and Decimal(account["paid_fees_usdt"]) == 0
                    and not positions and not buys and not closes):
                raise LedgerError("paper account is already clean at the requested reset balance")
            previous = {"initial_balance_usdt": account["initial_balance_usdt"], "free_usdt": account["free_usdt"],
                "realized_pnl": account["realized_pnl"], "paid_fees_usdt": account["paid_fees_usdt"],
                "active_positions": len(positions), "active_buy_proposals": len(buys),
                "active_close_proposals": len(closes),
                "locked_usdt": str(sum((Decimal(row["quote_spent"]) for row in positions), Decimal("0")))}
            # ACCOUNT_RESET is deliberately not a sale and therefore has no exit price/proceeds/P&L.
            connection.execute("UPDATE paper_positions SET status='CLOSED',closed_at=?,last_checked_at=?,exit_reason='ACCOUNT_RESET' WHERE status IN ('OPEN','CLOSING')", (now, now))
            connection.execute("UPDATE proposals SET status='REJECTED',failure_reason='ACCOUNT_RESET: paper account reset' WHERE mode='paper' AND status IN ('PENDING','EXECUTING','RECONCILE')")
            connection.execute("UPDATE paper_close_proposals SET status='EXPIRED',failure_reason='ACCOUNT_RESET: paper account reset',consumed_at=? WHERE status IN ('PENDING','EXECUTING')", (now,))
            connection.execute("UPDATE paper_account SET initial_balance_usdt=?,free_usdt=?,realized_pnl='0',paid_fees_usdt='0',updated_at=? WHERE id=1", (str(new_balance), str(new_balance), now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("paper.account_reset", None, canonical_json({"timestamp": now, "previous": previous,
                 "new_balance_usdt": str(new_balance), "reason": reason}), now))
        return {"previous": previous, "current": self.paper_balance(), "reset_at": now}

    def expire_stale_paper_close_proposals(self) -> int:
        now = utcnow(); expired = 0
        with self.transaction() as connection:
            rows = connection.execute("SELECT id,expires_at FROM paper_close_proposals WHERE status='PENDING'").fetchall()
            for row in rows:
                if parse_time(row["expires_at"]) <= now:
                    connection.execute("UPDATE paper_close_proposals SET status='EXPIRED' WHERE id=? AND status='PENDING'", (row["id"],))
                    if connection.execute("SELECT changes()").fetchone()[0]:
                        expired += 1
                        connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                            ("paper.manual_close_expired", row["id"], canonical_json({"automatic": True}), isoformat(now)))
        return expired

    def successful_paper_entries(self, day_prefix: str) -> int:
        with self.connect() as connection:
            reset = connection.execute("SELECT created_at FROM events WHERE kind='paper.account_reset' ORDER BY id DESC LIMIT 1").fetchone()
            cutoff = max(day_prefix, reset["created_at"] if reset else day_prefix)
            return int(connection.execute("SELECT COUNT(*) FROM proposals WHERE mode='paper' AND status='EXECUTED' AND execution_status='FILLED' AND executed_at>=? AND substr(executed_at,1,10)=?", (cutoff, day_prefix)).fetchone()[0])

    def committed_live_executions(self, day_prefix: str) -> int:
        """Conservative quota evidence for LIVE writes recorded by this ledger.

        A protected order-list acknowledgement is not a fill confirmation, but
        it can still become an exchange fill. Count it against the daily entry
        budget rather than allowing a replay or an unavailable fill report to
        create extra capacity.
        """
        with self.connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM proposals WHERE mode='live' AND status='EXECUTED' "
                "AND executed_at IS NOT NULL AND substr(executed_at,1,10)=?", (day_prefix,)
            ).fetchone()[0])

    def terminalize_paper_proposal(self, proposal_id: str, reason: str) -> dict[str, Any]:
        now=isoformat()
        with self.transaction() as connection:
            row=connection.execute("SELECT status,mode FROM proposals WHERE id=?",(proposal_id,)).fetchone()
            if row is None or row["mode"] != "paper" or row["status"] not in ("PENDING","EXECUTING"):
                raise LedgerError("paper proposal is not terminalizable")
            connection.execute("UPDATE proposals SET status='REJECTED',failure_reason=? WHERE id=?",(reason,proposal_id))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.proposal_terminalized",proposal_id,canonical_json({"reason":reason}),now))
        return self.get_proposal(proposal_id)

    def reject_all_pending_paper(self, reason: str) -> int:
        now=isoformat()
        with self.transaction() as connection:
            rows=connection.execute("SELECT id FROM proposals WHERE mode='paper' AND status='PENDING'").fetchall()
            for row in rows:
                connection.execute("UPDATE proposals SET status='REJECTED',failure_reason=? WHERE id=?",(reason,row["id"]))
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.proposal_terminalized",row["id"],canonical_json({"reason":reason}),now))
        return len(rows)

    @staticmethod
    def _daily_paper_realized_loss_from(
        connection: sqlite3.Connection, day_prefix: str
    ) -> Decimal:
        reset = connection.execute(
            "SELECT created_at FROM events WHERE kind='paper.account_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cutoff = max(day_prefix, reset["created_at"] if reset else day_prefix)
        rows = connection.execute(
            """SELECT payload_json FROM events
               WHERE kind IN ('paper.position_closed','paper.position_partially_closed')
                 AND created_at>=? AND substr(created_at,1,10)=?""",
            (cutoff, day_prefix),
        ).fetchall()
        losses = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            pnl = Decimal(str(payload["realized_pnl"]))
            if pnl < 0:
                losses.append(-pnl)
        return sum(losses, Decimal("0"))

    def daily_paper_realized_loss(self, day_prefix: str) -> Decimal:
        with self.connect() as connection:
            return self._daily_paper_realized_loss_from(connection, day_prefix)

    @staticmethod
    def _paper_realized_loss_since(
        connection: sqlite3.Connection, start: str
    ) -> Decimal:
        reset = connection.execute(
            "SELECT created_at FROM events WHERE kind='paper.account_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cutoff = max(start, reset["created_at"] if reset else start)
        rows = connection.execute(
            """SELECT payload_json FROM events
               WHERE kind IN ('paper.position_closed','paper.position_partially_closed')
                 AND created_at>=?""",
            (cutoff,),
        ).fetchall()
        loss = Decimal("0")
        for row in rows:
            pnl = Decimal(str(json.loads(row["payload_json"])["realized_pnl"]))
            if pnl < 0:
                loss -= pnl
        return loss

    def weekly_paper_realized_loss(self, week_start: str) -> Decimal:
        with self.connect() as connection:
            return self._paper_realized_loss_since(connection, week_start)

    def has_active_paper_buy(self, symbol: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM proposals WHERE mode='paper' AND symbol=? AND status IN ('PENDING','EXECUTING') LIMIT 1", (symbol,)).fetchone() is not None

    def list_paper_positions(self, open_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_positions"
        if open_only:
            query += " WHERE status IN ('OPEN','CLOSING')"
        query += " ORDER BY opened_at DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def get_paper_position(self, position_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM paper_positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise LedgerError(f"paper position not found: {position_id}")
        return dict(row)

    @staticmethod
    def _insert_position(connection: sqlite3.Connection, position: dict[str, str]) -> None:
        connection.execute("""
            INSERT INTO paper_positions(
              id,proposal_id,symbol,side,status,entry_reference,average_fill_price,
              gross_quantity,net_quantity,quote_spent,entry_fee_base,final_stop,
              final_target,risk_amount,net_expected_reward_risk,opened_at,last_checked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (position["id"], position["proposal_id"], position["symbol"], "BUY", "OPEN",
              position["entry_reference"], position["average_fill_price"], position["gross_base_quantity"],
              position["net_base_quantity"], position["quote_spent"], position["entry_fee_base"],
              position["final_stop"], position["final_target"], position["risk_amount"],
              position["net_expected_reward_risk"], position["opened_at"], position["opened_at"]))

    def backfill_paper_position(self, position: dict[str, str], entry_fee_usdt: Decimal) -> bool:
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM paper_positions WHERE proposal_id=?", (position["proposal_id"],)).fetchone():
                return False
            proposal = connection.execute("SELECT status,mode,execution_status FROM proposals WHERE id=?", (position["proposal_id"],)).fetchone()
            if proposal is None or proposal["status"] != "EXECUTED" or proposal["mode"] != "paper" or proposal["execution_status"] != "FILLED":
                raise LedgerError("paper fill backfill source is not an executed paper fill")
            if connection.execute("SELECT COUNT(*) FROM paper_positions WHERE status IN ('OPEN','CLOSING')").fetchone()[0] >= 1:
                raise LedgerError("cannot backfill paper fill: an open position already exists")
            account = connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            spend = Decimal(position["quote_spent"])
            if Decimal(account["free_usdt"]) < spend:
                raise LedgerError("cannot backfill paper fill: virtual balance is insufficient")
            self._insert_position(connection, position)
            free = Decimal(account["free_usdt"]) - spend
            fees = Decimal(account["paid_fees_usdt"]) + entry_fee_usdt
            now = isoformat()
            connection.execute("UPDATE paper_account SET free_usdt=?,paid_fees_usdt=?,updated_at=? WHERE id=1", (str(free), str(fees), now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.position_opened", position["id"], canonical_json({"proposal_id": position["proposal_id"], "migration": True}), now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.balance_changed", position["id"], canonical_json({"free_usdt": str(free), "reason": "position_backfill"}), now))
        return True

    def finish_paper_and_open(self, proposal_id: str, lease_hash: str, order_id: str,
                              summary: dict[str, Any], position: dict[str, str],
                              entry_fee_usdt: Decimal, max_positions: int,
                              max_exposure: Decimal, max_symbol_risk: Decimal,
                              max_aggregate_risk: Decimal, fee_rate: Decimal,
                              slippage_rate: Decimal, fresh_bid: Decimal,
                              fresh_ask: Decimal, max_economic_positions: int,
                              minimum_free_reserve: Decimal,
                              max_daily_realized_loss: Decimal,
                              day_prefix: str,
                              max_symbol_exposure: Decimal | None = None,
                              max_weekly_realized_loss: Decimal | None = None,
                              week_start: str | None = None) -> dict[str, Any]:
        with self.transaction() as connection:
            proposal = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if proposal is None or proposal["status"] != "EXECUTING" or proposal["execution_lease_hash"] != lease_hash:
                raise LedgerError("proposal is not executing with the supplied lease")
            open_rows = connection.execute("SELECT * FROM paper_positions WHERE status IN ('OPEN','CLOSING')").fetchall()
            if len(open_rows) >= max_positions:
                raise LedgerError(f"active PAPER tranche limit reached ({max_positions})")
            economic_symbols = {row["symbol"] for row in open_rows}
            if (position["symbol"] not in economic_symbols
                    and len(economic_symbols) >= max_economic_positions):
                raise LedgerError(
                    f"economic PAPER position/distinct-symbol limit reached ({max_economic_positions})"
                )
            if sum((Decimal(row["quote_spent"]) for row in open_rows), Decimal("0")) + Decimal(position["quote_spent"]) > max_exposure:
                raise LedgerError(f"PAPER exposure limit reached: projected exposure would exceed {max_exposure} USDT")
            same = [row for row in open_rows if row["symbol"] == position["symbol"]]
            if (max_symbol_exposure is not None
                    and sum((Decimal(row["quote_spent"]) for row in same), Decimal("0"))
                    + Decimal(position["quote_spent"]) > max_symbol_exposure):
                raise LedgerError(
                    f"PAPER single-position exposure would exceed {max_symbol_exposure} USDT"
                )
            projected_rows = same + [position]
            def economic_risk(items: list[Any]) -> Decimal:
                quantity=sum((Decimal(row["net_quantity"] if "net_quantity" in row.keys() else row["net_base_quantity"]) for row in items),Decimal("0"))
                if quantity <= 0:
                    raise LedgerError("invalid PAPER quantity at atomic commit")
                cost=sum((Decimal(row["quote_spent"]) for row in items),Decimal("0"))
                stop=max(Decimal(row["final_stop"]) for row in items)
                entry=sum((Decimal(row["average_fill_price"])*Decimal(row["net_quantity"] if "net_quantity" in row.keys() else row["net_base_quantity"]) for row in items),Decimal("0"))/quantity
                if stop >= entry:
                    raise LedgerError("invalid PAPER bracket: stop must be below aggregate average entry")
                risk=cost-quantity*stop*(Decimal("1")-slippage_rate)*(Decimal("1")-fee_rate)
                if risk <= 0:
                    raise LedgerError("invalid PAPER risk: downside loss must be positive")
                return risk
            if same:
                old_qty=sum((Decimal(row["net_quantity"]) for row in same),Decimal("0"))
                old_entry=sum((Decimal(row["average_fill_price"])*Decimal(row["net_quantity"]) for row in same),Decimal("0"))/old_qty
                old_stop=max(Decimal(row["final_stop"]) for row in same)
                old_target=min(Decimal(row["final_target"]) for row in same)
                if not old_stop < old_entry < old_target:
                    raise LedgerError("invalid persisted PAPER bracket; position requires repair")
                total_qty=old_qty+Decimal(position["net_base_quantity"])
                weighted=(sum((Decimal(row["average_fill_price"])*Decimal(row["net_quantity"]) for row in same),Decimal("0"))+Decimal(position["average_fill_price"])*Decimal(position["net_base_quantity"]))/total_qty
                stop=max(Decimal(row["final_stop"]) for row in same)
                reward_risk=Decimal(json.loads(proposal["canonical_json"])["reward_risk"])
                target=weighted+(weighted-stop)*reward_risk
                if not (stop < weighted < target and stop < fresh_bid and target > fresh_ask):
                    raise LedgerError("invalid PAPER scale-in bracket at atomic commit")
                actual_rr=(target-weighted)/(weighted-stop)
                if actual_rr <= 0 or abs(actual_rr-reward_risk) > Decimal("0.0001"):
                    raise LedgerError("invalid PAPER scale-in reward/risk at atomic commit")
                projected_cost=sum((Decimal(row["quote_spent"]) for row in same),Decimal("0"))+Decimal(position["quote_spent"])
                symbol_risk=projected_cost-total_qty*stop*(Decimal("1")-slippage_rate)*(Decimal("1")-fee_rate)
                if symbol_risk <= 0:
                    raise LedgerError("invalid PAPER scale-in downside risk at atomic commit")
            else:
                entry=Decimal(position["average_fill_price"]); stop=Decimal(position["final_stop"]); target=Decimal(position["final_target"])
                if not (stop < entry < target and stop < fresh_bid and target > fresh_ask):
                    raise LedgerError("invalid PAPER bracket at atomic commit")
                reward_risk=Decimal(json.loads(proposal["canonical_json"])["reward_risk"])
                actual_rr=(target-entry)/(entry-stop)
                if actual_rr <= 0 or abs(actual_rr-reward_risk) > Decimal("0.0001"):
                    raise LedgerError("invalid PAPER reward/risk at atomic commit")
                symbol_risk=economic_risk(projected_rows)
            other_symbols={row["symbol"] for row in open_rows if row["symbol"] != position["symbol"]}
            current_other=sum((economic_risk([row for row in open_rows if row["symbol"]==symbol]) for symbol in other_symbols),Decimal("0"))
            projected_aggregate=current_other+symbol_risk
            if symbol_risk > max_symbol_risk or projected_aggregate > max_aggregate_risk:
                raise LedgerError(f"PAPER risk rejected: current aggregate risk {current_other + (economic_risk(same) if same else Decimal('0')):f} USDT; new economic-position risk {symbol_risk:f} USDT; projected aggregate risk {projected_aggregate:f} USDT; configured aggregate limit {max_aggregate_risk:f} USDT; configured per-position limit {max_symbol_risk:f} USDT")
            account = connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            spend = Decimal(position["quote_spent"])
            if Decimal(account["free_usdt"]) - spend < minimum_free_reserve:
                raise LedgerError("insufficient free paper USDT after reserve")
            daily_loss = self._daily_paper_realized_loss_from(
                connection, day_prefix
            )
            if daily_loss >= max_daily_realized_loss:
                raise LedgerError("paper daily realized loss cap is exhausted")
            if max_weekly_realized_loss is not None:
                if week_start is None:
                    raise LedgerError("weekly loss gate is missing its UTC boundary")
                weekly_loss = self._paper_realized_loss_since(connection, week_start)
                if weekly_loss >= max_weekly_realized_loss:
                    raise LedgerError("paper weekly realized loss cap is exhausted")
            self._insert_position(connection, position)
            if same:
                symbol_rows = connection.execute("SELECT * FROM paper_positions WHERE symbol=? AND status IN ('OPEN','CLOSING')", (position["symbol"],)).fetchall()
                total_qty = sum((Decimal(row["net_quantity"]) for row in symbol_rows), Decimal("0"))
                weighted = sum((Decimal(row["average_fill_price"]) * Decimal(row["net_quantity"]) for row in symbol_rows), Decimal("0")) / total_qty
                stop = max(Decimal(row["final_stop"]) for row in same)
                reward_risk = Decimal(json.loads(proposal["canonical_json"])["reward_risk"])
                target = weighted + (weighted - stop) * reward_risk
                economic_risk = symbol_risk
                if economic_risk <= 0 or economic_risk > max_symbol_risk:
                    raise LedgerError(f"paper combined symbol risk is invalid or exceeds {max_symbol_risk} USDT")
                for row in symbol_rows:
                    allocated = economic_risk * Decimal(row["net_quantity"]) / total_qty
                    connection.execute("UPDATE paper_positions SET final_stop=?,final_target=?,risk_amount=? WHERE id=?",
                        (str(stop), str(target), str(allocated), row["id"]))
            now = isoformat()
            connection.execute("UPDATE proposals SET status='EXECUTED',executed_at=?,execution_order_id=?,execution_status='FILLED',execution_summary_json=?,execution_lease_hash=NULL,execution_lease_expires_at=NULL WHERE id=?", (now, order_id, canonical_json(summary), proposal_id))
            free = Decimal(account["free_usdt"]) - spend
            fees = Decimal(account["paid_fees_usdt"]) + entry_fee_usdt
            connection.execute("UPDATE paper_account SET free_usdt=?,paid_fees_usdt=?,updated_at=? WHERE id=1", (str(free), str(fees), now))
            for kind, payload in (
                ("paper.position_opened", {"proposal_id": proposal_id, "position_id": position["id"]}),
                ("paper.balance_changed", {"free_usdt": str(free), "reason": "position_opened"}),
                ("execution.completed", {"status": "EXECUTED", "order_id": order_id}),
            ):
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", (kind, position["id"] if kind.startswith("paper.") else proposal_id, canonical_json(payload), now))
        return self.get_proposal(proposal_id)

    def repair_paper_bracket(self, symbol: str, expected_hash: str, stop: Decimal, target: Decimal, risk: Decimal) -> dict[str, Any]:
        now = isoformat()
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM paper_positions WHERE symbol=? AND status='OPEN' ORDER BY opened_at,id", (symbol,)).fetchall()
            if not rows:
                raise LedgerError("paper economic position is no longer open")
            snapshot = [{key: row[key] for key in ("id", "status", "net_quantity", "quote_spent", "average_fill_price", "final_stop", "final_target", "risk_amount")} for row in rows]
            if hashlib.sha256(canonical_json(snapshot).encode()).hexdigest() != expected_hash:
                raise LedgerError("paper position changed after repair validation; refusing repair")
            total_quantity = sum((Decimal(row["net_quantity"]) for row in rows), Decimal("0"))
            if total_quantity <= 0 or risk <= 0:
                raise LedgerError("paper bracket repair quantity or risk is invalid")
            for row in rows:
                allocated = risk * Decimal(row["net_quantity"]) / total_quantity
                connection.execute("UPDATE paper_positions SET final_stop=?,final_target=?,risk_amount=? WHERE id=? AND status='OPEN'", (str(stop), str(target), str(allocated), row["id"]))
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LedgerError("paper position changed during bracket repair")
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.bracket_repaired", symbol, canonical_json({"symbol": symbol, "tranches": len(rows), "stop": str(stop), "target": str(target), "risk_amount": str(risk), "reason": "restore last valid audited pre-scale-in stop"}), now))
        return {"symbol": symbol, "stop": str(stop), "target": str(target), "risk_amount": str(risk), "repaired_at": now, "tranches": len(rows)}

    def record_position_check(self, position_id: str, checked_at: str, payload: dict[str, Any]) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT status FROM paper_positions WHERE id=?", (position_id,)).fetchone()
            if row is None or row["status"] != "OPEN":
                return
            connection.execute("UPDATE paper_positions SET last_checked_at=? WHERE id=?", (checked_at, position_id))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.position_checked", position_id, canonical_json(payload), checked_at))

    def record_exit_incomplete(self, position_id: str, reason: str) -> None:
        self.add_event("paper.exit_data_incomplete", position_id, {"reason": reason})

    def close_paper_position(self, position_id: str, reason: str, values: dict[str, Decimal]) -> dict[str, Any]:
        now = isoformat()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM paper_positions WHERE id=?", (position_id,)).fetchone()
            if row is None:
                raise LedgerError(f"paper position not found: {position_id}")
            if row["status"] != "OPEN":
                raise LedgerError(f"paper position is not open: {row['status']}")
            pending=connection.execute("""SELECT c.id FROM paper_close_proposals c JOIN paper_positions p ON p.id=c.position_id WHERE p.symbol=? AND c.status='PENDING'""",(row["symbol"],)).fetchall()
            for item in pending:
                connection.execute("UPDATE paper_close_proposals SET status='FAILED',failure_reason='automatic position exit resolved economic position',consumed_at=? WHERE id=?",(now,item["id"]))
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.manual_close_terminalized",item["id"],canonical_json({"reason":"automatic_position_exit"}),now))
            connection.execute("UPDATE paper_positions SET status='CLOSING' WHERE id=? AND status='OPEN'", (position_id,))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LedgerError("paper position close was already claimed")
            pnl = values["net_proceeds"] - Decimal(row["quote_spent"])
            connection.execute("""UPDATE paper_positions SET status='CLOSED',closed_at=?,last_checked_at=?,
                exit_reason=?,exit_price=?,exit_fee=?,gross_proceeds=?,net_proceeds=?,realized_pnl=? WHERE id=?""",
                (now, now, reason, str(values["exit_price"]), str(values["exit_fee"]),
                 str(values["gross_proceeds"]), str(values["net_proceeds"]), str(pnl), position_id))
            account = connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            free = Decimal(account["free_usdt"]) + values["net_proceeds"]
            realized = Decimal(account["realized_pnl"]) + pnl
            fees = Decimal(account["paid_fees_usdt"]) + values["exit_fee"]
            connection.execute("UPDATE paper_account SET free_usdt=?,realized_pnl=?,paid_fees_usdt=?,updated_at=? WHERE id=1", (str(free), str(realized), str(fees), now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.position_closed", position_id, canonical_json({"reason": reason, "realized_pnl": str(pnl)}), now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.balance_changed", position_id, canonical_json({"free_usdt": str(free), "reason": "position_closed"}), now))
        return self.get_paper_position(position_id)

    def execute_paper_close(self, close_id: str, bid: Decimal, fee_rate: Decimal,
                            slippage_rate: Decimal, step: Decimal) -> dict[str, Any]:
        now=isoformat()
        with self.transaction() as connection:
            close=connection.execute("SELECT * FROM paper_close_proposals WHERE id=?",(close_id,)).fetchone()
            if close is None or close["status"] != "EXECUTING":
                raise LedgerError("paper close proposal is not executing")
            payload=json.loads(close["payload_json"]); symbol=payload["symbol"]
            rows=connection.execute("SELECT * FROM paper_positions WHERE symbol=? AND status='OPEN' ORDER BY opened_at,id",(symbol,)).fetchall()
            total=sum((Decimal(r["net_quantity"]) for r in rows),Decimal("0")); requested=Decimal(close["close_quantity"]); expected=Decimal(payload["aggregate_quantity"])
            if total != expected or requested<=0 or requested>total:
                raise LedgerError("paper economic position quantity changed; request a fresh close")
            raw=[]; allocated=Decimal("0")
            if requested == total:
                raw = [[row, Decimal(row["net_quantity"]), Decimal("0")] for row in rows]
            else:
                for row in rows:
                    exact=requested*Decimal(row["net_quantity"])/total; base=(exact//step)*step
                    base=min(base,Decimal(row["net_quantity"])); raw.append([row,base,exact-base]); allocated+=base
                residual=requested-allocated
                for item in sorted(raw,key=lambda x:(x[2],x[0]["id"]),reverse=True):
                    if residual<step: break
                    room=Decimal(item[0]["net_quantity"])-item[1]
                    if room>=step: item[1]+=step; residual-=step
                if residual != 0:
                    raise LedgerError("partial close residual cannot be allocated at quantity step")
            execution=bid*(Decimal("1")-slippage_rate); gross_total=fee_total=net_total=pnl_total=cost_total=Decimal("0")
            account=connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            for row,qty,_ in raw:
                if qty<=0: continue
                old_qty=Decimal(row["net_quantity"]); old_cost=Decimal(row["quote_spent"]); cost=old_cost*qty/old_qty
                gross=qty*execution; fee=gross*fee_rate; net=gross-fee; pnl=net-cost
                remaining=old_qty-qty; remaining_cost=old_cost-cost
                if remaining==0:
                    connection.execute("""UPDATE paper_positions SET status='CLOSED',net_quantity='0',quote_spent='0',risk_amount='0',closed_at=?,last_checked_at=?,exit_reason=?,exit_price=?,exit_fee=?,gross_proceeds=?,net_proceeds=?,realized_pnl=? WHERE id=? AND status='OPEN'""",(now,now,"MANUAL_FULL" if requested==total else "MANUAL_PARTIAL_FINAL",str(execution),str(fee),str(gross),str(net),str(pnl),row["id"]))
                else:
                    risk=Decimal(row["risk_amount"])*remaining/old_qty
                    connection.execute("UPDATE paper_positions SET net_quantity=?,quote_spent=?,risk_amount=?,last_checked_at=? WHERE id=? AND status='OPEN'",(str(remaining),str(remaining_cost),str(risk),now,row["id"]))
                if connection.execute("SELECT changes()").fetchone()[0]!=1: raise LedgerError("paper close tranche race detected")
                gross_total+=gross; fee_total+=fee; net_total+=net; pnl_total+=pnl; cost_total+=cost
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.position_partially_closed",row["id"],canonical_json({"close_id":close_id,"quantity":str(qty),"realized_pnl":str(pnl)}),now))
            free=Decimal(account["free_usdt"])+net_total; realized=Decimal(account["realized_pnl"])+pnl_total; fees=Decimal(account["paid_fees_usdt"])+fee_total
            connection.execute("UPDATE paper_account SET free_usdt=?,realized_pnl=?,paid_fees_usdt=?,updated_at=? WHERE id=1",(str(free),str(realized),str(fees),now))
            connection.execute("UPDATE paper_close_proposals SET status='EXECUTED',consumed_at=? WHERE id=? AND status='EXECUTING'",(now,close_id))
            remaining_total=total-requested
            result={"symbol":symbol,"requested_percentage":close["requested_percentage"],"actual_executed_percentage":str(requested / total * Decimal("100")),"quantity_closed":str(requested),"quantity_remaining":str(remaining_total),"economic_position_status":"CLOSED" if remaining_total == 0 else "OPEN","execution_status":"FILLED","average_cost":payload["average_cost"],"exit_price":str(execution),"gross_proceeds":str(gross_total),"fee_asset":"USDT","exit_fee_usdt":str(fee_total),"net_proceeds":str(net_total),"cost_basis_closed":str(cost_total),"realized_pnl":str(pnl_total),"closed_at":now,
                "closed_tranches":[item[0]["id"] for item in raw if item[1] > 0 and Decimal(item[0]["net_quantity"])-item[1] == 0]}
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.manual_close_executed",close_id,canonical_json(result),now))
        return result

    def close_paper_symbol(self, symbol: str, reason: str, values_by_id: dict[str, dict[str, Decimal]]) -> list[dict[str, Any]]:
        """Close every OPEN tranche for one symbol in one SQLite transaction."""
        now = isoformat(); closed_ids: list[str] = []
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM paper_positions WHERE symbol=? AND status='OPEN' ORDER BY opened_at", (symbol,)).fetchall()
            if not rows:
                raise LedgerError("paper economic position is not open")
            if {row["id"] for row in rows} != set(values_by_id):
                raise LedgerError("paper symbol close tranche set changed; request a fresh close")
            pending=connection.execute("""SELECT c.id FROM paper_close_proposals c JOIN paper_positions p ON p.id=c.position_id WHERE p.symbol=? AND c.status='PENDING'""",(symbol,)).fetchall()
            for item in pending:
                connection.execute("UPDATE paper_close_proposals SET status='FAILED',failure_reason='automatic position exit resolved economic position',consumed_at=? WHERE id=?",(now,item["id"]))
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.manual_close_terminalized",item["id"],canonical_json({"reason":"automatic_position_exit"}),now))
            account = connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            free=Decimal(account["free_usdt"]); realized=Decimal(account["realized_pnl"]); fees=Decimal(account["paid_fees_usdt"])
            for row in rows:
                values=values_by_id[row["id"]]; pnl=values["net_proceeds"]-Decimal(row["quote_spent"])
                connection.execute("""UPDATE paper_positions SET status='CLOSED',closed_at=?,last_checked_at=?,exit_reason=?,exit_price=?,exit_fee=?,gross_proceeds=?,net_proceeds=?,realized_pnl=? WHERE id=? AND status='OPEN'""",
                    (now,now,reason,str(values["exit_price"]),str(values["exit_fee"]),str(values["gross_proceeds"]),str(values["net_proceeds"]),str(pnl),row["id"]))
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LedgerError("paper tranche close race detected")
                free += values["net_proceeds"]; realized += pnl; fees += values["exit_fee"]; closed_ids.append(row["id"])
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.position_closed",row["id"],canonical_json({"reason":reason,"realized_pnl":str(pnl)}),now))
            connection.execute("UPDATE paper_account SET free_usdt=?,realized_pnl=?,paid_fees_usdt=?,updated_at=? WHERE id=1", (str(free),str(realized),str(fees),now))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.balance_changed",symbol,canonical_json({"free_usdt":str(free),"reason":"economic_position_closed","tranches":len(rows)}),now))
        return [self.get_paper_position(item) for item in closed_ids]

    def create_paper_close_proposal(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            now = utcnow()
            stale = connection.execute("SELECT id,expires_at FROM paper_close_proposals WHERE status='PENDING'").fetchall()
            for row in stale:
                if parse_time(row["expires_at"]) <= now:
                    connection.execute("UPDATE paper_close_proposals SET status='EXPIRED' WHERE id=? AND status='PENDING'", (row["id"],))
                    connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                        ("paper.manual_close_expired", row["id"], canonical_json({"automatic": True}), isoformat(now)))
            pos = connection.execute("SELECT status FROM paper_positions WHERE id=?", (values["position_id"],)).fetchone()
            if pos is None or pos["status"] != "OPEN":
                raise LedgerError("paper position is not open")
            existing = connection.execute("""SELECT 1 FROM paper_close_proposals c
                JOIN paper_positions cp ON cp.id=c.position_id JOIN paper_positions target ON target.id=?
                WHERE cp.symbol=target.symbol AND c.status IN ('PENDING','EXECUTING') LIMIT 1""", (values["position_id"],)).fetchone()
            if existing:
                raise LedgerError("an active close proposal already exists")
            created_at=isoformat(now); expires_at=isoformat(now+timedelta(seconds=int(values["ttl_seconds"])))
            connection.execute("""INSERT INTO paper_close_proposals(
                id,position_id,status,token_hash,code_hash,owner_id,chat_id,created_at,expires_at,
                payload_json,payload_hash,requested_percentage,close_quantity,reference_bid)
                VALUES(?,?, 'PENDING',?,?,?,?,?,?,?,?,?,?,?)""",
                (values["id"],values["position_id"],values["token_hash"],values["code_hash"],values["owner_id"],values["chat_id"],created_at,expires_at,
                 values["payload_json"],values["payload_hash"],values["requested_percentage"],values["close_quantity"],values["reference_bid"]))
            if values.get("locale"):
                connection.execute("INSERT INTO presentation_locales VALUES('proposal',?,?)", (values["id"], values["locale"]))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                ("paper.manual_close_proposed",values["id"],canonical_json({"position_id":values["position_id"],"percentage":values["requested_percentage"],"close_quantity":values["close_quantity"]}),created_at))
        return self.get_paper_close_proposal(values["id"], False)

    def get_paper_close_proposal(self, close_id: str, private: bool = False) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM paper_close_proposals WHERE id=?", (close_id,)).fetchone()
        if row is None:
            raise LedgerError(f"paper close proposal not found: {close_id}")
        result = dict(row)
        if not private:
            for key in ("token_hash","code_hash","owner_id","chat_id","payload_json","payload_hash"):
                result.pop(key,None)
        return result

    def claim_paper_close(self, close_id: str, token_hash: str, code_hash: str | None,
                          owner_id: str, chat_id: str) -> dict[str, Any]:
        self.expire_stale_paper_close_proposals()
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM paper_close_proposals WHERE id=?", (close_id,)).fetchone()
            if row is None or row["status"] != "PENDING":
                raise LedgerError("paper close proposal is not pending")
            if not row["payload_json"] or hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["payload_hash"]:
                raise LedgerError("paper close immutable payload hash is invalid")
            if row["owner_id"] != owner_id or row["chat_id"] != chat_id or row["token_hash"] != token_hash:
                raise LedgerError("paper close approval binding is invalid")
            if code_hash is not None and row["code_hash"] != code_hash:
                raise LedgerError("paper close confirmation code is invalid")
            pos = connection.execute("SELECT status FROM paper_positions WHERE id=?", (row["position_id"],)).fetchone()
            if pos is None or pos["status"] != "OPEN":
                raise LedgerError("paper position is no longer open")
            connection.execute("UPDATE paper_close_proposals SET status='EXECUTING',consumed_at=? WHERE id=?", (isoformat(now), close_id))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.manual_close_approved", close_id, canonical_json({"position_id": row["position_id"]}), isoformat(now)))
        return self.get_paper_close_proposal(close_id, False)

    def get_active_paper_close_for_position(self, position_id: str, private: bool = False) -> dict[str, Any]:
        self.expire_stale_paper_close_proposals()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM paper_close_proposals WHERE position_id=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1", (position_id,)).fetchone()
        if row is None:
            raise LedgerError("no pending paper close proposal exists for this position; it may have expired")
        result = dict(row)
        if not private:
            for key in ("token_hash", "code_hash", "owner_id", "chat_id"):
                result.pop(key, None)
        return result

    def terminalize_paper_close(self, close_id: str, reason: str) -> dict[str, Any]:
        now=isoformat()
        with self.transaction() as connection:
            row=connection.execute("SELECT status FROM paper_close_proposals WHERE id=?",(close_id,)).fetchone()
            if row is None or row["status"] not in ("PENDING","EXECUTING"):
                raise LedgerError("paper close proposal is already resolved")
            connection.execute("UPDATE paper_close_proposals SET status='FAILED',failure_reason=?,consumed_at=? WHERE id=?",(reason,now,close_id))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",("paper.manual_close_terminalized",close_id,canonical_json({"reason":reason}),now))
        return self.get_paper_close_proposal(close_id)

    def reject_paper_close(self, close_id: str, token_hash: str, owner_id: str, chat_id: str,
                           code_hash: str | None = None) -> dict[str, Any]:
        self.expire_stale_paper_close_proposals()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM paper_close_proposals WHERE id=?", (close_id,)).fetchone()
            if row is None or row["status"] != "PENDING":
                raise LedgerError("paper close proposal is not pending")
            if (row["token_hash"] != token_hash or row["owner_id"] != owner_id or row["chat_id"] != chat_id
                    or (code_hash is not None and row["code_hash"] != code_hash)):
                raise LedgerError("paper close rejection binding is invalid")
            connection.execute("UPDATE paper_close_proposals SET status='REJECTED',consumed_at=? WHERE id=?", (isoformat(), close_id))
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.manual_close_rejected", close_id, canonical_json({"position_id": row["position_id"]}), isoformat()))
        return self.get_paper_close_proposal(close_id)

    def record_exit_incomplete_once(self, position_id: str, reason: str) -> bool:
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM events WHERE kind='paper.exit_data_incomplete' AND entity_id=? LIMIT 1", (position_id,)).fetchone():
                return False
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.exit_data_incomplete", position_id, canonical_json({"reason": reason}), isoformat()))
        return True

    def record_invalid_bracket_once(self, position_id: str, reason: str) -> bool:
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM events WHERE kind='paper.invalid_bracket' AND entity_id=? LIMIT 1", (position_id,)).fetchone():
                return False
            connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)", ("paper.invalid_bracket", position_id, canonical_json({"reason": reason, "action": "repair_required"}), isoformat()))
        return True

    def finish_paper_close_proposal(self, close_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE paper_close_proposals SET status=? WHERE id=? AND status='EXECUTING'", (status, close_id))

    def reconcile_paper_state(self) -> dict[str, int]:
        recovered_positions = recovered_closes = 0
        with self.transaction() as connection:
            closing = connection.execute("SELECT id FROM paper_positions WHERE status='CLOSING'").fetchall()
            for row in closing:
                connection.execute("UPDATE paper_positions SET status='OPEN' WHERE id=?", (row["id"],))
                recovered_positions += 1
            rows = connection.execute("SELECT id,position_id FROM paper_close_proposals WHERE status='EXECUTING'").fetchall()
            for row in rows:
                position = connection.execute("SELECT status FROM paper_positions WHERE id=?", (row["position_id"],)).fetchone()
                status = "EXECUTED" if position is not None and position["status"] == "CLOSED" else "FAILED"
                connection.execute("UPDATE paper_close_proposals SET status=? WHERE id=?", (status, row["id"]))
                recovered_closes += 1
            if recovered_positions or recovered_closes:
                connection.execute("INSERT INTO events(kind,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                    ("paper.restart_reconciled", None, canonical_json({"positions": recovered_positions, "close_proposals": recovered_closes}), isoformat()))
        return {"positions": recovered_positions, "close_proposals": recovered_closes}

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            candidate_count = connection.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()["c"]
            proposal_count = connection.execute("SELECT COUNT(*) AS c FROM proposals").fetchone()["c"]
            pending_count = connection.execute(
                "SELECT COUNT(*) AS c FROM proposals WHERE status IN ('PENDING','EXECUTING')"
            ).fetchone()["c"]
        return {
            "candidates": int(candidate_count),
            "proposals": int(proposal_count),
            "pending_or_executing": int(pending_count),
        }

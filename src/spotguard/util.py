from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any


UTC = timezone.utc
SIMPLE_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    current = value or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def pretty_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def decimal_string(value: Decimal, places: int = 8) -> str:
    quantum = Decimal(1).scaleb(-places)
    rendered = format(value.quantize(quantum, rounding=ROUND_DOWN), "f")
    rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def ensure_private_dir(path: Path) -> Path:
    unresolved = path.expanduser()
    if unresolved.is_symlink():
        raise ValueError(f"state directory must not be a symlink: {unresolved}")
    expanded = unresolved.resolve(strict=False)
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        expanded.chmod(0o700)
    except PermissionError:
        pass
    return expanded


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def validate_simple_id(value: str, field: str) -> str:
    if not SIMPLE_ID_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def bounded_text(value: str, field: str, maximum: int = 500) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return normalized

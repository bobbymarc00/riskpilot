from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .util import atomic_write_text, canonical_json, isoformat, parse_time, utcnow


class SecurityError(RuntimeError):
    pass


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class ApprovalSigner:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.key_path = state_dir / "approval.key"

    def _key(self) -> bytes:
        if self.key_path.exists():
            if self.key_path.is_symlink():
                raise SecurityError("approval key must not be a symlink")
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise SecurityError("approval key has an invalid length")
            mode = self.key_path.stat().st_mode & 0o777
            if mode & 0o077:
                try:
                    self.key_path.chmod(0o600)
                except PermissionError as exc:
                    raise SecurityError("approval key permissions must be 0600") from exc
            return key
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            key = secrets.token_bytes(32)
            os.write(descriptor, key)
            os.fsync(descriptor)
            return key
        finally:
            os.close(descriptor)

    def approval_token(self, canonical_proposal: str) -> str:
        digest = hmac.new(self._key(), canonical_proposal.encode("utf-8"), hashlib.sha256).digest()
        return _urlsafe(digest[:16])

    def paper_confirmation_code(self, canonical_proposal: str) -> str:
        digest = hmac.new(self._key(), b"paper-confirmation:" + canonical_proposal.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:8].upper()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def verify_approval(self, canonical_proposal: str, supplied: str) -> bool:
        expected = self.approval_token(canonical_proposal)
        return hmac.compare_digest(expected, supplied)

    @staticmethod
    def new_lease() -> tuple[str, str]:
        lease = _urlsafe(secrets.token_bytes(18))
        return lease, hashlib.sha256(lease.encode("ascii")).hexdigest()

    @staticmethod
    def verify_lease(lease: str, stored_hash: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(lease.encode("ascii")).hexdigest(), stored_hash)

    def sign_object(self, value: dict[str, Any]) -> str:
        return _urlsafe(hmac.new(self._key(), canonical_json(value).encode("utf-8"), hashlib.sha256).digest())

    def verify_object(self, value: dict[str, Any], signature: str) -> bool:
        return hmac.compare_digest(self.sign_object(value), signature)


@dataclass(frozen=True)
class ArmStatus:
    armed: bool
    expires_at: str | None
    reason: str
    scope: str = "FULL"
    binding: dict[str, Any] | None = None


class LiveArm:
    def __init__(self, state_dir: Path, signer: ApprovalSigner) -> None:
        self.path = state_dir / "live-arm.json"
        self.signer = signer

    def arm(self, minutes: int, scope: str = "FULL", binding: dict[str, Any] | None = None) -> ArmStatus:
        if scope not in {"FULL", "EXIT_ONLY"}:
            raise SecurityError("invalid LIVE arm scope")
        body = {
            "schema": "spotguard.live-arm.v1",
            "created_at": isoformat(),
            "expires_at": isoformat(utcnow() + timedelta(minutes=minutes)),
            "nonce": _urlsafe(secrets.token_bytes(12)),
            "scope": scope,
        }
        if binding is not None:
            body["binding"] = dict(binding)
        document = {"body": body, "signature": self.signer.sign_object(body)}
        atomic_write_text(self.path, json.dumps(document, sort_keys=True, indent=2) + "\n", mode=0o600)
        return ArmStatus(True, body["expires_at"], "live execution is armed", scope, dict(binding or {}))

    def disarm(self) -> ArmStatus:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return ArmStatus(False, None, "live execution is disarmed")

    def status(self) -> ArmStatus:
        if not self.path.exists():
            return ArmStatus(False, None, "live arm file is absent")
        if self.path.is_symlink():
            return ArmStatus(False, None, "live arm file is a symlink")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            body = document["body"]
            signature = document["signature"]
            if not isinstance(body, dict) or not isinstance(signature, str):
                raise ValueError("invalid arm document")
            if not self.signer.verify_object(body, signature):
                return ArmStatus(False, None, "live arm signature is invalid")
            expiry = str(body["expires_at"])
            if parse_time(expiry) <= utcnow():
                return ArmStatus(False, expiry, "live arm has expired")
            scope = body.get("scope", "FULL")
            if scope not in {"FULL", "EXIT_ONLY"}:
                return ArmStatus(False, None, "live arm scope is invalid")
            binding = body.get("binding")
            if binding is not None and not isinstance(binding, dict):
                return ArmStatus(False, None, "live arm binding is invalid")
            return ArmStatus(True, expiry, "live execution is armed", scope, binding)
        except (KeyError, ValueError, json.JSONDecodeError):
            return ArmStatus(False, None, "live arm file is invalid")

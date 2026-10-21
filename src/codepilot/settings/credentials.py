"""Encrypted credential vault for provider API keys.

Only ciphertext is persisted. The master key comes from
``CODEPILOT_SETTINGS_MASTER_KEY`` or a 0600 key file inside the settings
directory. If neither can be established the vault reports itself unavailable
and callers must fall back to environment-variable references; this module
never writes a plaintext secret.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

__all__ = [
    "CredentialError",
    "CredentialVault",
    "SecretNotFoundError",
    "SecureStorageUnavailableError",
]

_KEY_FILE = "master.key"
_CREDENTIAL_SUFFIX = ".cred"
_MIN_SECRET_LENGTH = 4
_MAX_SECRET_LENGTH = 8192


class CredentialError(RuntimeError):
    """Base class for credential failures."""


class SecureStorageUnavailableError(CredentialError):
    """No master key / encryption backend is available."""


class SecretNotFoundError(CredentialError):
    """A referenced credential no longer exists."""


class CredentialVault:
    def __init__(self, directory: Path, *, master_key: str | None = None) -> None:
        self.directory = Path(directory)
        self.credentials_dir = self.directory / "credentials"
        self._master_key = master_key
        self._fernet: Fernet | None = None

    # ------------------------------------------------------------- availability
    @property
    def available(self) -> bool:
        try:
            self._cipher()
        except SecureStorageUnavailableError:
            return False
        return True

    def _cipher(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SecureStorageUnavailableError(
                "the cryptography package is not installed; only environment credentials are available"
            ) from exc

        key = self._master_key or os.environ.get("CODEPILOT_SETTINGS_MASTER_KEY")
        if key:
            raw = key.encode("ascii")
            try:
                cipher = Fernet(raw)
            except (ValueError, TypeError) as exc:
                raise SecureStorageUnavailableError(
                    "CODEPILOT_SETTINGS_MASTER_KEY is not a valid Fernet key"
                ) from exc
            self._fernet = cipher
            return cipher

        key_path = self.directory / _KEY_FILE
        if key_path.is_file():
            raw = key_path.read_text(encoding="ascii").strip().encode("ascii")
            try:
                cipher = Fernet(raw)
            except (ValueError, TypeError) as exc:
                raise SecureStorageUnavailableError("the stored master key is invalid") from exc
            self._fernet = cipher
            return cipher

        self.directory.mkdir(parents=True, exist_ok=True)
        raw = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw + b"\n")
        except BaseException:
            key_path.unlink(missing_ok=True)
            raise
        with contextlib.suppress(OSError):
            key_path.chmod(0o600)
        cipher = Fernet(raw)
        self._fernet = cipher
        return cipher

    # ------------------------------------------------------------------ values
    def put(self, value: str) -> str:
        secret = value.strip()
        if not (_MIN_SECRET_LENGTH <= len(secret) <= _MAX_SECRET_LENGTH):
            raise ValueError("credential length is outside the accepted range")
        cipher = self._cipher()
        credential_id = secrets.token_hex(16)
        self.credentials_dir.mkdir(parents=True, exist_ok=True)
        token = cipher.encrypt(
            json.dumps(
                {"value": secret, "created_at": datetime.now(UTC).isoformat()},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        target = self.credentials_dir / f"{credential_id}{_CREDENTIAL_SUFFIX}"
        staging = target.with_suffix(".tmp")
        staging.write_bytes(token)
        with contextlib.suppress(OSError):
            staging.chmod(0o600)
        staging.replace(target)
        return credential_id

    def get(self, credential_id: str) -> str:
        if not _valid_id(credential_id):
            raise SecretNotFoundError("invalid credential reference")
        path = self.credentials_dir / f"{credential_id}{_CREDENTIAL_SUFFIX}"
        if not path.is_file():
            raise SecretNotFoundError("the referenced credential no longer exists")
        cipher = self._cipher()
        try:
            payload = json.loads(cipher.decrypt(path.read_bytes()))
        except Exception as exc:
            raise CredentialError("the stored credential could not be decrypted") from exc
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise CredentialError("the stored credential is malformed")
        return value

    def delete(self, credential_id: str) -> None:
        if not _valid_id(credential_id):
            return
        (self.credentials_dir / f"{credential_id}{_CREDENTIAL_SUFFIX}").unlink(missing_ok=True)

    def exists(self, credential_id: str) -> bool:
        return (
            _valid_id(credential_id)
            and (self.credentials_dir / f"{credential_id}{_CREDENTIAL_SUFFIX}").is_file()
        )


def _valid_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)

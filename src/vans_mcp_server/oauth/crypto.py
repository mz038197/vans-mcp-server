from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptor:
    """Fernet wrapper for reversible OAuth token storage."""

    def __init__(self, key: str) -> None:
        key = (key or "").strip()
        if not key:
            raise ValueError("OAUTH_TOKEN_ENCRYPTION_KEY is required")
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)

    @classmethod
    def from_env(cls) -> TokenEncryptor | None:
        key = os.environ.get("OAUTH_TOKEN_ENCRYPTION_KEY") or ""
        if not key.strip():
            return None
        return cls(key.strip())

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("failed to decrypt oauth token") from exc

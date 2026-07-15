from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

STATE_MAX_AGE_SECONDS = 600


class DiscordConnectState:
    """HMAC-signed connect-link state (SESSION_SECRET). No Discord OAuth client."""

    def __init__(self, session_secret: str) -> None:
        self.session_secret = session_secret.strip()

    @classmethod
    def from_env(cls) -> DiscordConnectState | None:
        session_secret = (os.environ.get("SESSION_SECRET") or "").strip()
        if not session_secret:
            return None
        return cls(session_secret)

    def is_configured(self) -> bool:
        return bool(self.session_secret)

    def _encode_state(self, raw: str) -> str:
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    def _decode_state_candidates(self, state: str) -> list[str]:
        state = state.strip()
        candidates = [state]
        try:
            padded = state + "=" * (-len(state) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            if decoded and decoded not in candidates:
                candidates.append(decoded)
        except (ValueError, UnicodeDecodeError):
            pass
        return candidates

    def create_connect_state(self, user_id: int) -> str:
        nonce = secrets.token_urlsafe(24)
        ts = str(int(time.time()))
        payload = f"{int(user_id)}:{nonce}:{ts}"
        sig = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return self._encode_state(f"{payload}:{sig}")

    def verify_connect_state(self, state: str) -> int | None:
        for candidate in self._decode_state_candidates(state):
            parts = candidate.split(":")
            if len(parts) != 4:
                continue
            user_s, _nonce, ts_s, sig = parts
            payload = f"{user_s}:{_nonce}:{ts_s}"
            expected = hmac.new(
                self.session_secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                continue
            try:
                ts = int(ts_s)
                user_id = int(user_s)
            except ValueError:
                continue
            if abs(int(time.time()) - ts) > STATE_MAX_AGE_SECONDS:
                continue
            return user_id
        return None

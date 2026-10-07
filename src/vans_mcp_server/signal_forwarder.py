"""Post each ERROR log under vans_mcp_server to vans-signals. The caller does not wait."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

import httpx

PACKAGE_LOGGER = "vans_mcp_server"
SIGNAL_TIMEOUT_SEC = 2.0
_FAILURE_LOGGER = "vans_signals_forwarder"


def _signal_message(record: logging.LogRecord) -> str:
    message = record.getMessage()
    if record.exc_info and record.exc_info[0] is not None:
        stack = logging.Formatter().formatException(record.exc_info)
        if stack:
            return f"{message}\n{stack}"
    return message


class SignalForwarder(logging.Handler):
    def __init__(self, url: str, token: str, source: str):
        super().__init__(level=logging.ERROR)
        self._url = url.rstrip("/") + "/signals"
        self._token = token
        self._source = source
        self._inflight: list[threading.Thread] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        payload = {
            "log_time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger_name": record.name,
            "level": record.levelname,
            "message": _signal_message(record),
            "source": self._source,
        }
        worker = threading.Thread(target=self._post, args=(payload,), daemon=True)
        self._inflight.append(worker)
        worker.start()

    def _post(self, payload: dict) -> None:
        try:
            response = httpx.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=SIGNAL_TIMEOUT_SEC,
            )
            response.raise_for_status()
        except Exception as exc:
            logging.getLogger(_FAILURE_LOGGER).error(
                "Signal post failed: %s",
                type(exc).__name__,
            )

    def close(self) -> None:
        logging.getLogger(PACKAGE_LOGGER).removeHandler(self)
        self.acquire()
        try:
            workers = list(self._inflight)
        finally:
            self.release()
        for worker in workers:
            worker.join(timeout=SIGNAL_TIMEOUT_SEC + 1.0)
        super().close()


def _detach_existing_forwarders() -> None:
    package = logging.getLogger(PACKAGE_LOGGER)
    for handler in list(package.handlers):
        if isinstance(handler, SignalForwarder):
            package.removeHandler(handler)


def start_signal_forwarding(
    url: str | None = None,
    token: str | None = None,
    source: str | None = None,
) -> SignalForwarder | None:
    destination = (url if url is not None else os.getenv("VANS_SIGNALS_URL") or "").strip()
    bearer = (token if token is not None else os.getenv("VANS_SIGNALS_TOKEN") or "").strip()
    origin = (source if source is not None else os.getenv("VANS_SIGNALS_SOURCE") or "").strip()
    _detach_existing_forwarders()
    if not destination or not bearer or not origin:
        return None
    forwarder = SignalForwarder(destination, bearer, origin)
    logging.getLogger(PACKAGE_LOGGER).addHandler(forwarder)
    return forwarder

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_database_from_the_shell(monkeypatch):
    """A developer shell with a database URL must not reach a real database from tests."""
    monkeypatch.delenv("VANS_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

"""Key Check client: the router answers at its HTTP boundary from the contract examples."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from vans_mcp_server.key_check import KeyCheckClient, KeyIdentity, KeyRefusal

CONTRACTS = Path(__file__).parent / "fixtures" / "key_check"
CREDENTIAL = "svc-credential-for-tests"
STUDENT_KEY = "vcr_sk_student_secret_value"


def contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


class FakeRouter:
    """Answers POST /internal/key-check and records what it was asked."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else contract("classroom-accepted.json")
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def client_for(router: FakeRouter, **kwargs) -> KeyCheckClient:
    return KeyCheckClient(
        vcr_url="https://router.test",
        service_credential=CREDENTIAL,
        transport=router.transport,
        **kwargs,
    )


async def test_classroom_key_passes_with_the_student_identity():
    router = FakeRouter(body=contract("classroom-accepted.json"))

    result = await client_for(router).check(STUDENT_KEY)

    assert result == KeyIdentity(
        kind="classroom", user_id=1234, class_id=56, class_session_id=789
    )
    sent = router.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://router.test/internal/key-check"
    assert sent.headers["authorization"] == f"Bearer {CREDENTIAL}"
    assert json.loads(sent.content) == {"api_key": STUDENT_KEY}


async def test_personal_key_passes_with_the_user_and_their_classes():
    router = FakeRouter(body=contract("personal-accepted.json"))

    result = await client_for(router).check("vcr_sk_teacher_secret_value")

    assert result == KeyIdentity(
        kind="personal",
        user_id=42,
        is_admin=False,
        classes=((56, "週一 程式設計"), (57, "週三 進階班")),
    )


async def test_refusal_carries_the_router_message_unchanged():
    router = FakeRouter(body=contract("refused.json"))

    result = await client_for(router).check(STUDENT_KEY)

    assert result == KeyRefusal(reason="invalid", message="無效的 API 金鑰")


async def test_not_started_refusal_shows_the_router_wording():
    not_started = dict(
        contract("refused.json"),
        reason="not_started",
        message="課堂尚未開始，請在開始時間後再使用",
    )
    router = FakeRouter(body=not_started)

    result = await client_for(router).check(STUDENT_KEY)

    assert result == KeyRefusal(
        reason="not_started", message="課堂尚未開始，請在開始時間後再使用"
    )


@pytest.mark.parametrize("presented", ["sk-openai-style", "", "   ", "VCR_SK_upper"])
async def test_key_without_the_vcr_sk_prefix_is_invalid_without_asking_the_router(
    presented,
):
    router = FakeRouter()

    result = await client_for(router).check(presented)

    assert result == KeyRefusal(reason="invalid", message="無效的 API 金鑰")
    assert router.requests == []


async def test_surrounding_whitespace_is_trimmed_before_the_router_sees_the_key():
    router = FakeRouter()

    await client_for(router).check(f"  {STUDENT_KEY}\n")

    assert json.loads(router.requests[0].content) == {"api_key": STUDENT_KEY}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_a_pass_is_reused_for_60_seconds_then_asked_again():
    router = FakeRouter()
    clock = FakeClock()
    client = client_for(router, clock=clock)

    first = await client.check(STUDENT_KEY)
    clock.advance(59.9)
    second = await client.check(STUDENT_KEY)

    assert second == first
    assert len(router.requests) == 1

    clock.advance(0.2)
    await client.check(STUDENT_KEY)
    assert len(router.requests) == 2


async def test_cached_passes_are_per_key():
    router = FakeRouter()
    client = client_for(router, clock=FakeClock())

    await client.check(STUDENT_KEY)
    await client.check("vcr_sk_another_student")

    assert len(router.requests) == 2


async def test_a_refusal_is_asked_again_every_time():
    router = FakeRouter(body=contract("refused.json"))
    client = client_for(router, clock=FakeClock())

    await client.check(STUDENT_KEY)
    await client.check(STUDENT_KEY)

    assert len(router.requests) == 2


UNAVAILABLE = KeyRefusal(reason="unavailable", message="驗證服務暫時無法使用，請稍後再試")


def error_signals(caplog) -> list:
    """ERROR records under vans_mcp_server: the forwarder posts each as a Signal."""
    return [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and r.name.startswith("vans_mcp_server")
    ]


def assert_one_signal_without_the_key(caplog, key: str = STUDENT_KEY) -> None:
    signals = error_signals(caplog)
    assert len(signals) == 1
    text = signals[0].getMessage()
    assert key[:12] in text
    assert key not in text
    for record in caplog.records:
        assert key not in record.getMessage()


@pytest.mark.parametrize(
    "status, body",
    [
        (500, {"detail": "boom"}),
        (503, {"detail": "down"}),
        (429, {"error": {"code": "rate_limited"}}),
        (401, {"error": {"code": "invalid_service_credential"}}),
    ],
)
async def test_router_failure_is_unavailable_and_sends_one_error_signal(
    caplog, status, body
):
    router = FakeRouter(status=status, body=body)

    result = await client_for(router).check(STUDENT_KEY)

    assert result == UNAVAILABLE
    assert_one_signal_without_the_key(caplog)


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
)
async def test_unreachable_router_is_unavailable_and_sends_one_error_signal(
    caplog, error
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    client = KeyCheckClient(
        vcr_url="https://router.test",
        service_credential=CREDENTIAL,
        transport=httpx.MockTransport(handler),
    )

    result = await client.check(STUDENT_KEY)

    assert result == UNAVAILABLE
    assert_one_signal_without_the_key(caplog)


async def test_an_expired_pass_is_not_extended_while_the_router_is_down(caplog):
    router = FakeRouter()
    clock = FakeClock()
    client = client_for(router, clock=clock)
    await client.check(STUDENT_KEY)

    router.status, router.body = 503, {"detail": "down"}
    clock.advance(61)

    assert await client.check(STUDENT_KEY) == UNAVAILABLE
    router.status, router.body = 200, contract("classroom-accepted.json")
    assert len(router.requests) == 2


async def test_a_router_slower_than_3_seconds_is_unavailable(caplog):
    import asyncio

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=contract("classroom-accepted.json"))

    client = KeyCheckClient(
        vcr_url="https://router.test",
        service_credential=CREDENTIAL,
        transport=httpx.MockTransport(slow),
    )

    started = time.monotonic()
    result = await client.check(STUDENT_KEY)
    waited = time.monotonic() - started

    assert result == UNAVAILABLE
    assert 2.9 <= waited < 4.0
    assert_one_signal_without_the_key(caplog)

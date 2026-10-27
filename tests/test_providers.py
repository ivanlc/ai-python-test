import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.models import Notification
from app.ai.prompts import SYSTEM_PROMPT
from app.providers import CircuitBreaker, CircuitOpenError, HTTPProvider, ProviderError, SlidingWindowLimiter
from app.providers.resilience import retry_after_seconds


def ai_response(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


async def test_exact_provider_contract_and_api_key():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "test-dev-2026"
        if request.url.path == "/v1/ai/extract":
            payload = json.loads(request.content)
            assert [message["role"] for message in payload["messages"]] == ["system", "user"]
            assert payload["messages"][0]["content"] == SYSTEM_PROMPT
            assert payload["messages"][1]["content"] == "Email a juan@example.com: hola"
            return httpx.Response(200, json=ai_response(
                '{"to":"juan@example.com","message":"hola","type":"email"}'))
        assert request.url.path == "/v1/notify"
        assert json.loads(request.content) == {
            "to": "juan@example.com", "message": "hola", "type": "email"}
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1234"})

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url,
                                headers={"X-API-Key": settings.provider_api_key}) as client:
        provider = HTTPProvider(client, settings)
        assert "juan@example.com" in await provider.extract("Email a juan@example.com: hola")
        await provider.notify(Notification(to="juan@example.com", message="hola", type="email"))
    assert len(calls) == 2


async def test_ai_429_is_retried():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=ai_response("{}"))

    settings = Settings(ai_retries=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        assert await HTTPProvider(client, settings).extract("test") == "{}"
    assert attempts == 2


async def test_ai_timeout_retries_then_fails():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    settings = Settings(ai_retries=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError):
            await HTTPProvider(client, settings).extract("test")
    assert attempts == 2


async def test_notify_429_retries_but_timeout_does_not():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})

    settings = Settings(notify_429_retries=1)
    notification = Notification(to="juan@example.com", message="hola", type="email")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        await HTTPProvider(client, settings).notify(notification)
    assert attempts == 2

    attempts = 0

    def timeout(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("unknown outcome", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError):
            await HTTPProvider(client, settings).notify(notification)
    assert attempts == 1


async def test_notify_500_does_not_retry():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(500)

    settings = Settings(notify_429_retries=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError):
            await HTTPProvider(client, settings).notify(
                Notification(to="juan@example.com", message="hola", type="email"))
    assert attempts == 1


async def test_sliding_window_bounds_concurrent_attempts():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=0.05)
    timestamps = []

    async def reserve():
        await limiter.acquire()
        timestamps.append(time.monotonic())

    await asyncio.gather(*(reserve() for _ in range(5)))
    timestamps.sort()
    assert timestamps[2] - timestamps[0] >= 0.05
    assert timestamps[4] - timestamps[2] >= 0.05


@pytest.mark.parametrize("payload", [
    {"choices": []},
    {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"role": "user", "content": "{}"}}]},
    {"choices": [{"message": {"content": "{}"}}, {"message": {"content": "{}"}}]},
])
async def test_incomplete_or_ambiguous_chat_completion_is_rejected(payload):
    settings = Settings(ai_retries=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError):
            await HTTPProvider(client, settings).extract("test")


def test_retry_after_is_not_shortened_and_nonfinite_values_are_ignored():
    assert retry_after_seconds("30") == 30
    assert retry_after_seconds("nan") is None
    assert retry_after_seconds("inf") is None


def test_retry_after_http_date(monkeypatch):
    monkeypatch.setattr("app.providers.resilience.time.time", lambda: 1445412470.0)
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 10.0


async def test_excessive_retry_after_fails_without_retrying_early():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError, match="retry budget"):
            await HTTPProvider(client, settings).notify(
                Notification(to="juan@example.com", message="hola", type="email"))
    assert len(attempts) == 1


async def test_provider_cooldown_applies_to_all_workers():
    limiter = SlidingWindowLimiter(limit=50, window_seconds=10)
    start = time.monotonic()
    await limiter.defer(0.03)
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    assert time.monotonic() - start >= 0.03


@pytest.mark.parametrize("status_code, expected_attempts", [(401, 1), (422, 1), (503, 2)])
async def test_ai_http_retry_classification(status_code, expected_attempts):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status_code)

    settings = Settings(ai_retries=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        with pytest.raises(ProviderError):
            await HTTPProvider(client, settings).extract("test")
    assert len(attempts) == expected_attempts


async def test_429_without_retry_after_waits_for_provider_window():
    attempts = []

    def handler(request):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})

    settings = Settings(notify_429_retries=1, notify_window_seconds=0.02)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        await HTTPProvider(client, settings).notify(
            Notification(to="juan@example.com", message="hola", type="email"))
    assert attempts[1] - attempts[0] >= settings.notify_window_seconds


@pytest.mark.parametrize("operation, response", [
    ("extract", httpx.Response(200, content=b"not JSON")),
    ("extract", httpx.Response(200, json={"choices": [{"message": {"content": None}}]})),
    ("notify", httpx.Response(200, content=b"not JSON")),
    ("notify", httpx.Response(200, json={"status": "delivered"})),
    ("notify", httpx.Response(200, json={"status": "queued", "provider_id": "p-1"})),
])
async def test_malformed_success_response_is_not_retried(operation, response):
    attempts = []

    def handler(request):
        attempts.append(request)
        return response

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url) as client:
        provider = HTTPProvider(client, settings)
        with pytest.raises(ProviderError):
            if operation == "extract":
                await provider.extract("test")
            else:
                await provider.notify(Notification(to="juan@example.com", message="hola", type="email"))
    assert len(attempts) == 1


def mock_provider(handler, **overrides):
    settings = Settings(**overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.provider_base_url)
    return client, HTTPProvider(client, settings)


async def test_re_ask_appends_only_the_fixed_reason_to_the_system_prompt():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json=ai_response("REFUSED"))

    client, provider = mock_provider(handler)
    async with client:
        await provider.extract("Email a juan@example.com: hola", feedback="Model refused the request")
    system, user = seen[0]
    assert system["content"].startswith(SYSTEM_PROMPT)
    assert "rejected by validation: Model refused the request." in system["content"]
    # The user message is unchanged, so the mock extracts exactly as on the first call.
    assert user == {"role": "user", "content": "Email a juan@example.com: hola"}


async def test_ai_circuit_opens_fails_fast_and_recovers_with_one_probe(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.providers.resilience.time.monotonic", lambda: now[0])
    healthy = False
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if not healthy:
            return httpx.Response(503)
        return httpx.Response(200, json=ai_response("{}"))

    client, provider = mock_provider(handler, ai_retries=0, breaker_failure_threshold=2,
                                     breaker_cooldown_seconds=5)
    async with client:
        for _ in range(2):
            with pytest.raises(ProviderError):
                await provider.extract("x")
        assert provider.ai_breaker.state == "open" and calls == 2
        with pytest.raises(CircuitOpenError):
            await provider.extract("x")
        assert calls == 2  # failed fast, no request sent
        now[0] += 5
        assert provider.ai_breaker.state == "half-open"
        with pytest.raises(ProviderError):
            await provider.extract("x")  # failed probe re-opens the circuit
        assert provider.ai_breaker.state == "open" and calls == 3
        now[0] += 5
        healthy = True
        assert await provider.extract("x") == "{}"
        assert provider.ai_breaker.state == "closed"


def test_half_open_allows_a_single_probe_and_cancelled_probe_releases_it(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.providers.resilience.time.monotonic", lambda: now[0])
    breaker = CircuitBreaker("test", threshold=1, cooldown_seconds=1)
    breaker.on_failure()
    now[0] = 1.0
    breaker.before_call()
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.on_abort()
    breaker.before_call()


async def test_notify_429_does_not_open_the_circuit():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"})

    client, provider = mock_provider(handler, notify_429_retries=1, breaker_failure_threshold=1)
    async with client:
        with pytest.raises(ProviderError, match="rate limit"):
            await provider.notify(Notification(to="a@example.com", message="hola", type="email"))
    assert provider.notify_breaker.state == "closed"


async def test_open_notify_circuit_does_not_send():
    sent = 0

    def handler(request):
        nonlocal sent
        sent += 1
        return httpx.Response(500)

    client, provider = mock_provider(handler, breaker_failure_threshold=1)
    notification = Notification(to="a@example.com", message="hola", type="email")
    async with client:
        with pytest.raises(ProviderError):
            await provider.notify(notification)
        with pytest.raises(CircuitOpenError):
            await provider.notify(notification)
    assert sent == 1


async def test_limiter_fails_before_sending_when_the_deadline_would_pass():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    await limiter.acquire()
    with pytest.raises(ProviderError, match="deadline"):
        await limiter.acquire(deadline=time.monotonic() + 1)

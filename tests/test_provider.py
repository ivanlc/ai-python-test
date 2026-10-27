import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.models import Notification
from app.provider import HTTPProvider, ProviderError, SlidingWindowLimiter, SYSTEM_PROMPT


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

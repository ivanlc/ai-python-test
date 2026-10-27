"""HTTP adapter for the exact mock-provider contracts."""

import asyncio
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from ..ai.prompts import RETRY_INSTRUCTIONS, SYSTEM_PROMPT
from ..config import Settings
from ..models import Notification
from .resilience import CircuitBreaker, ProviderError, SlidingWindowLimiter, retry_after_seconds


class _RateLimited(Exception):
    """Internal signal: the provider declined this attempt with 429."""

    def __init__(self, fatal_reason: Optional[str] = None) -> None:
        super().__init__(fatal_reason)
        self.fatal_reason = fatal_reason


class _ChatMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class _Choice(BaseModel):
    message: _ChatMessage
    finish_reason: Literal["stop"] = "stop"


class _AIResponse(BaseModel):
    choices: list[_Choice] = Field(min_length=1, max_length=1)


class _NotificationResponse(BaseModel):
    status: str
    provider_id: str


class HTTPProvider:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.limiter = SlidingWindowLimiter(settings.notify_limit, settings.notify_window_seconds)
        self.ai_breaker = CircuitBreaker("AI provider", settings.breaker_failure_threshold,
                                         settings.breaker_cooldown_seconds)
        self.notify_breaker = CircuitBreaker("Notification provider", settings.breaker_failure_threshold,
                                             settings.breaker_cooldown_seconds)

    async def extract(self, user_input: str, feedback: Optional[str] = None) -> str:
        """Call the AI once (with transport retries) behind the circuit breaker.

        `feedback` is a fixed validation message, never model or user text, so it
        can be appended to the system prompt without elevating untrusted data.
        The user message is unchanged on a re-ask.
        """
        system = SYSTEM_PROMPT + (RETRY_INSTRUCTIONS.format(reason=feedback) if feedback else "")
        payload = {"messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]}
        self.ai_breaker.before_call()
        try:
            content = await self._extract(payload)
        except ProviderError:
            self.ai_breaker.on_failure()
            raise
        except BaseException:
            self.ai_breaker.on_abort()
            raise
        self.ai_breaker.on_success()
        return content

    async def _extract(self, payload: dict) -> str:
        for attempt in range(self.settings.ai_retries + 1):
            try:
                response = await self.client.post(
                    "/v1/ai/extract", json=payload,
                    timeout=self.settings.ai_timeout_seconds,
                )
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.settings.ai_retries:
                    retry_after = (retry_after_seconds(response.headers.get("Retry-After"))
                                   if response.status_code == 429 else None)
                    await asyncio.sleep(retry_after if retry_after is not None else 0.2 * 2 ** attempt)
                    continue
                response.raise_for_status()
                parsed = _AIResponse.model_validate(response.json())
                return parsed.choices[0].message.content
            except httpx.RequestError as exc:
                if attempt == self.settings.ai_retries:
                    raise ProviderError("AI request failed") from exc
                await asyncio.sleep(0.2 * 2 ** attempt)
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise ProviderError("AI response was invalid") from exc
        raise ProviderError("AI retries exhausted")

    async def notify(self, notification: Notification, deadline: Optional[float] = None) -> None:
        """Send once; only an explicit 429 (a known non-delivery) is retried.

        The deadline is only checked before a request is sent: an in-flight
        notification is never cancelled, because its outcome would be unknown.
        """
        for attempt in range(self.settings.notify_429_retries + 1):
            await self.limiter.acquire(deadline)
            self.notify_breaker.before_call()
            try:
                await self._send(notification)
            except _RateLimited as limited:
                # The provider is up and declined explicitly; not a breaker failure.
                self.notify_breaker.on_success()
                if limited.fatal_reason:
                    raise ProviderError(limited.fatal_reason) from None
                if attempt < self.settings.notify_429_retries:
                    continue
                raise ProviderError("Notification rate limit retries exhausted") from None
            except ProviderError:
                self.notify_breaker.on_failure()
                raise
            except BaseException:
                self.notify_breaker.on_abort()
                raise
            self.notify_breaker.on_success()
            return
        raise AssertionError("unreachable: every attempt returns or raises")

    async def _send(self, notification: Notification) -> None:
        # A timeout or 5xx may follow acceptance. Retrying could send twice.
        try:
            response = await self.client.post(
                "/v1/notify", json=notification.model_dump(),
                timeout=self.settings.notify_timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise ProviderError("Notification outcome is unknown") from exc
        if response.status_code == 429:
            try:
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            except ProviderError as exc:
                raise _RateLimited(str(exc)) from None
            await self.limiter.defer(retry_after if retry_after is not None
                                     else self.settings.notify_window_seconds)
            # An explicit 429 means the provider declined: the outcome is known.
            raise _RateLimited()
        try:
            response.raise_for_status()
            parsed = _NotificationResponse.model_validate(response.json())
        except (httpx.HTTPStatusError, ValueError) as exc:
            raise ProviderError("Notification was rejected or its outcome is unknown") from exc
        if parsed.status != "delivered" or not parsed.provider_id:
            raise ProviderError("Notification delivery was not confirmed")

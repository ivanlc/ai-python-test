"""In-memory application service and bounded worker queue."""

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Dict, List, Optional, Protocol
from uuid import uuid4

from .config import Settings
from .models import Notification, RequestStatus
from .guardrails import ExtractionError, ModelDeclined, RequestRejected, check_request, extract_notification
from .providers import ProviderError


logger = logging.getLogger(__name__)


class Provider(Protocol):
    async def extract(self, user_input: str, feedback: Optional[str] = None) -> str: ...
    async def notify(self, notification: Notification, deadline: Optional[float] = None) -> None: ...


class NotFoundError(LookupError):
    pass


class CapacityError(RuntimeError):
    pass


@dataclass
class _Record:
    user_input: str
    status: RequestStatus = RequestStatus.queued
    enqueued: bool = False
    updated_at: float = 0.0


class RequestService:
    def __init__(self, provider: Provider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self._records: Dict[str, _Record] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_capacity)
        self._lock = asyncio.Lock()
        self._workers: List[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker(), name=f"notification-worker-{i}")
                         for i in range(self.settings.worker_count)]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def create(self, user_input: str) -> str:
        async with self._lock:
            if len(self._records) >= self.settings.max_records:
                self._prune_expired()
            if len(self._records) >= self.settings.max_records:
                raise CapacityError("Request store is full")
            request_id = str(uuid4())
            self._records[request_id] = _Record(user_input=user_input, updated_at=time.monotonic())
            return request_id

    async def process(self, request_id: str) -> RequestStatus:
        async with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise NotFoundError(request_id)
            if record.status != RequestStatus.queued or record.enqueued:
                return record.status
            try:
                self._queue.put_nowait(request_id)
            except asyncio.QueueFull as exc:
                raise CapacityError("Processing queue is full") from exc
            record.enqueued = True
            record.updated_at = time.monotonic()
            return record.status

    async def get_status(self, request_id: str) -> RequestStatus:
        async with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise NotFoundError(request_id)
            return record.status

    async def _worker(self) -> None:
        while True:
            request_id = await self._queue.get()
            try:
                await self._process_one(request_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Last line of defence: a bookkeeping bug must not shrink the pool.
                logger.exception("Worker bookkeeping failed for %s", request_id)
            finally:
                self._queue.task_done()

    async def _process_one(self, request_id: str) -> None:
        async with self._lock:
            record = self._records.get(request_id)
            if record is None:
                logger.error("Queued request %s no longer exists", request_id)
                return
            record.status = RequestStatus.processing
            record.updated_at = time.monotonic()
            user_input = record.user_input
        # The deadline bounds how long one job can occupy a worker. Extraction has
        # no side effect and may be cancelled; a notification already sent is not.
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        try:
            check_request(user_input)
            notification = await asyncio.wait_for(self._extract(request_id, user_input),
                                                  self.settings.job_timeout_seconds)
            await self.provider.notify(notification, deadline=deadline)
        except asyncio.TimeoutError:
            logger.warning("Request %s failed: job deadline expired during extraction", request_id)
            terminal = RequestStatus.failed
        except (ExtractionError, ProviderError) as exc:
            logger.warning("Request %s failed: %s", request_id, exc)
            terminal = RequestStatus.failed
        except Exception:
            logger.exception("Unexpected processing failure for %s", request_id)
            terminal = RequestStatus.failed
        else:
            logger.info("Request %s sent via %s", request_id, notification.type)
            terminal = RequestStatus.sent
        async with self._lock:
            record.status = terminal
            record.updated_at = time.monotonic()

    async def _extract(self, request_id: str, user_input: str) -> Notification:
        """Ask the model; re-ask a bounded number of times when its output fails validation.

        Not retried: request-level rejections (no model output could pass them) and a
        protocol refusal (`REFUSED`), which is a deliberate decision. Off-protocol
        output, such as free-text refusals or malformed objects, is treated as noise.
        The re-ask carries only the fixed validation reason, never model or user text.
        """
        feedback: Optional[str] = None
        for attempt in range(self.settings.extraction_retries + 1):
            content = await self.provider.extract(user_input, feedback)
            try:
                return extract_notification(content, user_input)
            except (RequestRejected, ModelDeclined):
                raise
            except ExtractionError as exc:
                if attempt == self.settings.extraction_retries:
                    raise
                feedback = str(exc)
                logger.info("Request %s: re-asking the model after validation failure: %s",
                            request_id, feedback)
        raise AssertionError("unreachable")

    def _prune_expired(self) -> None:
        cutoff = time.monotonic() - self.settings.record_ttl_seconds
        expired = [request_id for request_id, record in self._records.items()
                   if (record.status in (RequestStatus.sent, RequestStatus.failed)
                       or (record.status == RequestStatus.queued and not record.enqueued))
                   and record.updated_at < cutoff]
        for request_id in expired:
            del self._records[request_id]

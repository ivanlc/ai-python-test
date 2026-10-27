"""In-memory application service and bounded worker queue."""

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Dict, List, Protocol
from uuid import uuid4

from .config import Settings
from .models import RequestStatus
from .parsing import ExtractionError, extract_notification
from .provider import ProviderError


logger = logging.getLogger(__name__)


class Provider(Protocol):
    async def extract(self, user_input: str) -> str: ...
    async def notify(self, notification) -> None: ...


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
                async with self._lock:
                    record = self._records[request_id]
                    record.status = RequestStatus.processing
                    record.updated_at = time.monotonic()
                    user_input = record.user_input
                try:
                    content = await self.provider.extract(user_input)
                    notification = extract_notification(content, user_input)
                    await self.provider.notify(notification)
                except (ExtractionError, ProviderError) as exc:
                    logger.warning("Request %s failed: %s", request_id, exc)
                    terminal = RequestStatus.failed
                except Exception:
                    logger.exception("Unexpected processing failure for %s", request_id)
                    terminal = RequestStatus.failed
                else:
                    terminal = RequestStatus.sent
                async with self._lock:
                    record.status = terminal
                    record.updated_at = time.monotonic()
            finally:
                self._queue.task_done()

    def _prune_expired(self) -> None:
        cutoff = time.monotonic() - self.settings.record_ttl_seconds
        expired = [request_id for request_id, record in self._records.items()
                   if (record.status in (RequestStatus.sent, RequestStatus.failed)
                       or (record.status == RequestStatus.queued and not record.enqueued))
                   and record.updated_at < cutoff]
        for request_id in expired:
            del self._records[request_id]

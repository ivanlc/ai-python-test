"""Validated environment configuration."""

from dataclasses import dataclass
import math
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    provider_base_url: str = "http://localhost:3001"
    provider_api_key: str = "test-dev-2026"  # Public key of the bundled mock provider.
    worker_count: int = 32
    queue_capacity: int = 4096
    max_records: int = 10000
    record_ttl_seconds: int = 600
    notify_limit: int = 50
    notify_window_seconds: float = 10.0
    ai_timeout_seconds: float = 6.0
    notify_timeout_seconds: float = 3.0
    ai_retries: int = 2
    notify_429_retries: int = 2

    def __post_init__(self) -> None:
        url = urlsplit(self.provider_base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("PROVIDER_BASE_URL must be an HTTP URL with a host")
        if not self.provider_api_key.strip():
            raise ValueError("PROVIDER_API_KEY cannot be empty")
        capacities = (self.worker_count, self.queue_capacity, self.max_records,
                      self.record_ttl_seconds, self.notify_limit)
        if any(type(value) is not int or value <= 0 for value in capacities):
            raise ValueError("Capacity and lifetime settings must be positive integers")
        durations = (self.notify_window_seconds, self.ai_timeout_seconds, self.notify_timeout_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in durations):
            raise ValueError("Timeout and rate window settings must be positive and finite")
        if any(type(value) is not int or value < 0
               for value in (self.ai_retries, self.notify_429_retries)):
            raise ValueError("Retry counts must be nonnegative integers")

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            "provider_base_url": os.getenv("PROVIDER_BASE_URL", cls.provider_base_url),
            "provider_api_key": os.getenv("PROVIDER_API_KEY", cls.provider_api_key),
            "worker_count": int(os.getenv("WORKER_COUNT", cls.worker_count)),
            "queue_capacity": int(os.getenv("QUEUE_CAPACITY", cls.queue_capacity)),
            "max_records": int(os.getenv("MAX_RECORDS", cls.max_records)),
            "record_ttl_seconds": int(os.getenv("RECORD_TTL_SECONDS", cls.record_ttl_seconds)),
            "notify_limit": int(os.getenv("NOTIFY_LIMIT", cls.notify_limit)),
            "notify_window_seconds": float(os.getenv("NOTIFY_WINDOW_SECONDS", cls.notify_window_seconds)),
            "ai_timeout_seconds": float(os.getenv("AI_TIMEOUT_SECONDS", cls.ai_timeout_seconds)),
            "notify_timeout_seconds": float(os.getenv("NOTIFY_TIMEOUT_SECONDS", cls.notify_timeout_seconds)),
            "ai_retries": int(os.getenv("AI_RETRIES", cls.ai_retries)),
            "notify_429_retries": int(os.getenv("NOTIFY_429_RETRIES", cls.notify_429_retries)),
        }
        return cls(**values)

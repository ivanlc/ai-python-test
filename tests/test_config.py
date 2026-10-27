import pytest

from app.config import Settings


@pytest.mark.parametrize("overrides", [
    {"queue_capacity": 0},
    {"worker_count": 0},
    {"ai_retries": -1},
    {"notify_window_seconds": float("nan")},
    {"ai_timeout_seconds": float("inf")},
    {"notify_timeout_seconds": 0},
    {"provider_api_key": " "},
    {"provider_base_url": "http://"},
    {"extraction_retries": -1},
    {"job_timeout_seconds": 0},
    {"breaker_failure_threshold": 0},
    {"breaker_cooldown_seconds": float("inf")},
])
def test_invalid_configuration_is_rejected_at_construction(overrides):
    with pytest.raises(ValueError):
        Settings(**overrides)


def test_nonfinite_environment_value_is_rejected(monkeypatch):
    monkeypatch.setenv("NOTIFY_WINDOW_SECONDS", "nan")
    with pytest.raises(ValueError):
        Settings.from_env()

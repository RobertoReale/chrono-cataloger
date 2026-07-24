"""Transport-level behaviour: retry headers and per-provider defaults."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from src.llm_client import LLMError, _retry_after_seconds, get_client


def test_retry_after_in_seconds():
    assert _retry_after_seconds("12") == 12.0


def test_retry_after_as_http_date():
    """RFC 9110 allows a date here; float() used to raise straight out of the retry loop."""
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    delay = _retry_after_seconds(format_datetime(when))
    assert 20 <= delay <= 40


def test_retry_after_in_the_past_is_zero():
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _retry_after_seconds(format_datetime(when)) == 0.0


def test_retry_after_is_capped():
    assert _retry_after_seconds("99999") == 300.0


def test_retry_after_absent_or_nonsense():
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("soon") is None


def test_unset_timeout_uses_the_provider_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    http = get_client({"provider": "anthropic", "timeout_seconds": None})
    local = get_client({"provider": "ollama", "timeout_seconds": None})
    assert http._timeout == 120.0
    assert local._timeout == 300.0


def test_configured_timeout_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert get_client({"provider": "anthropic", "timeout_seconds": 45})._timeout == 45.0
    assert get_client({"provider": "ollama", "timeout_seconds": 45})._timeout == 45.0


def test_missing_api_key_raises_llm_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError):
        get_client({"provider": "anthropic"})

"""
Tests — Automatic USDT/EGP Quote Fetcher (rate_api)
====================================================

Requirement coverage (fail-closed transport boundary):

- a valid payload becomes an exact immutable RateQuote (provider
  ``coingecko``, local aware-UTC capture instant);
- every bad payload (missing / zero / negative / malformed /
  exponent / non-JSON / non-200) raises ``RateProviderError`` and
  never becomes a quote;
- transport failures (timeout, connection errors) fail closed;
- the timeout is actually forwarded to the opener;
- ``TASKCOIN_RATE_API_URL`` / ``COINGECKO_API_KEY`` env wiring works
  and the key is only sent when configured;
- a naive injected ``now`` is rejected; the default capture is aware
  UTC.

No test performs real network I/O — ``urlopen`` is always patched.

Run:
    python3 -m pytest test_rate_api.py -v
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import rate_api
from rate_quote import PROVIDER_COINGECKO


class FakeResponse:
    """Minimal context-managed response double."""

    def __init__(self, payload=None, *, status=200, raw=None):
        self.status = status
        self.payload = payload
        self.raw = raw
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        self.read_limit = limit
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload).encode()


def test_fetches_and_validates_quote():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    response = FakeResponse({"tether": {"egp": 50.25}})
    with patch.object(rate_api, "urlopen", return_value=response) as opener:
        quote = rate_api.fetch_usdt_egp_quote(now=now)
    assert str(quote.rate_usdt_egp) == "50.25"
    assert quote.provider == PROVIDER_COINGECKO
    assert quote.captured_at == now
    # Bounded body read — a huge payload is never buffered whole.
    assert response.read_limit == rate_api.MAX_RESPONSE_BYTES
    # The default timeout reaches the opener.
    assert opener.call_args.kwargs["timeout"] == (
        rate_api.DEFAULT_TIMEOUT_SECONDS
    )


def test_integer_value_is_exact():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    with patch.object(
        rate_api, "urlopen",
        return_value=FakeResponse({"tether": {"egp": 51}}),
    ):
        quote = rate_api.fetch_usdt_egp_quote(now=now)
    assert quote.rate_usdt_egp == 51       # exact Decimal, not float


@pytest.mark.parametrize("payload", [
    {}, {"tether": {}}, {"tether": {"egp": 0}},
    {"tether": {"egp": -1}}, {"tether": {"egp": "bad"}},
    {"tether": {"egp": "1e2"}},              # exponent form rejected
    {"bitcoin": {"egp": 100}},               # wrong asset
])
def test_rejects_invalid_provider_payload(payload):
    with patch.object(
        rate_api, "urlopen", return_value=FakeResponse(payload)
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote()


def test_rejects_non_json_body():
    with patch.object(
        rate_api, "urlopen",
        return_value=FakeResponse(raw=b"<html>maintenance</html>"),
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote()


def test_rejects_non_200_status():
    with patch.object(
        rate_api, "urlopen",
        return_value=FakeResponse({"tether": {"egp": 50}}, status=503),
    ):
        with pytest.raises(rate_api.RateProviderError) as excinfo:
            rate_api.fetch_usdt_egp_quote()
    assert "503" in str(excinfo.value)


def test_rejects_network_failure():
    with patch.object(
        rate_api, "urlopen", side_effect=TimeoutError("timeout")
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote()
    with patch.object(
        rate_api, "urlopen", side_effect=OSError("connection refused")
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote()


def test_timeout_is_forwarded():
    with patch.object(
        rate_api, "urlopen", return_value=FakeResponse(
            {"tether": {"egp": 50}}
        )
    ) as opener:
        rate_api.fetch_usdt_egp_quote(timeout=3)
    assert opener.call_args.kwargs["timeout"] == 3


def test_env_endpoint_override():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return FakeResponse({"tether": {"egp": 50}})

    with patch.dict(
        os.environ, {"TASKCOIN_RATE_API_URL": "https://mirror.test/price"}
    ):
        with patch.object(rate_api, "urlopen", side_effect=fake_urlopen):
            rate_api.fetch_usdt_egp_quote()
    assert captured["url"].startswith("https://mirror.test/price?")
    assert "ids=tether" in captured["url"]
    assert "vs_currencies=egp" in captured["url"]


def test_api_key_header_only_when_configured():
    def header_items():
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            return FakeResponse({"tether": {"egp": 50}})

        with patch.object(rate_api, "urlopen", side_effect=fake_urlopen):
            rate_api.fetch_usdt_egp_quote()
        return captured["headers"]

    with patch.dict(os.environ, {"COINGECKO_API_KEY": "k-123"}):
        assert header_items()["x-cg-demo-api-key"] == "k-123"
    with patch.dict(os.environ, {"COINGECKO_API_KEY": ""}):
        assert "x-cg-demo-api-key" not in header_items()


def test_naive_now_rejected():
    with patch.object(
        rate_api, "urlopen", return_value=FakeResponse(
            {"tether": {"egp": 50}}
        )
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote(now=datetime(2026, 10, 12))


def test_default_capture_is_aware_utc():
    with patch.object(
        rate_api, "urlopen", return_value=FakeResponse(
            {"tether": {"egp": 50}}
        )
    ):
        quote = rate_api.fetch_usdt_egp_quote()
    assert quote.captured_at.tzinfo is not None
    assert quote.captured_at.utcoffset() is not None

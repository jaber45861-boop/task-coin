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
from rate_quote import PROVIDER_COINGECKO, PROVIDER_COINBASE


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


# ══════════════════════════════════════════════════════════════════
# Coinbase provider — a DIRECT USDT/EGP quote with no API key.
# Every case below patches `urlopen`: no real network I/O.
# ══════════════════════════════════════════════════════════════════


def _coinbase_payload(amount="50.25", base="USDT", currency="EGP"):
    return {"data": {"amount": amount, "base": base, "currency": currency}}


def _patch_coinbase(response):
    return patch.object(rate_api, "urlopen", return_value=response)


def test_coinbase_fetches_and_validates_quote():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    response = FakeResponse(_coinbase_payload("50.25"))
    with patch.object(rate_api, "urlopen", return_value=response) as opener:
        quote = rate_api.fetch_usdt_egp_quote_coinbase(now=now)
    assert str(quote.rate_usdt_egp) == "50.25"
    assert quote.provider == PROVIDER_COINBASE
    assert quote.captured_at == now
    # Bounded body read — a huge payload is never buffered whole.
    assert response.read_limit == rate_api.MAX_RESPONSE_BYTES
    # The default timeout reaches the opener.
    assert opener.call_args.kwargs["timeout"] == rate_api.DEFAULT_TIMEOUT_SECONDS
    # The URL requests the documented public spot endpoint, with no key.
    request = opener.call_args.args[0]
    assert request.full_url.endswith(
        "/v2/prices/USDT-EGP/spot"
    )
    assert request.get_method() == "GET"


def test_coinbase_url_and_headers_carry_no_secret():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        return FakeResponse(_coinbase_payload())

    with patch.object(rate_api, "urlopen", side_effect=fake_urlopen):
        rate_api.fetch_usdt_egp_quote_coinbase()
    # No key material is placed in the URL or any header.
    assert "key" not in captured["url"].lower()
    assert not any("key" in name for name in captured["headers"])


def test_coinbase_env_endpoint_override():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return FakeResponse(_coinbase_payload())

    with patch.dict(
        os.environ, {"TASKCOIN_RATE_API_URL": "https://mirror.test/v2/prices"}
    ):
        with patch.object(rate_api, "urlopen", side_effect=fake_urlopen):
            rate_api.fetch_usdt_egp_quote_coinbase()
    assert captured["url"].startswith("https://mirror.test/v2/prices/USDT-EGP/spot")


def test_coinbase_naive_now_rejected():
    with _patch_coinbase(FakeResponse(_coinbase_payload())):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase(
                now=datetime(2026, 10, 12)
            )


def test_coinbase_default_capture_is_aware_utc():
    with _patch_coinbase(FakeResponse(_coinbase_payload())):
        quote = rate_api.fetch_usdt_egp_quote_coinbase()
    assert quote.captured_at.tzinfo is not None
    assert quote.captured_at.utcoffset() is not None


@pytest.mark.parametrize(
    "payload",
    [
        {},                                    # no data object at all
        {"data": None},                         # data is null
        {"data": "50.25"},                      # data is a bare string
        {"data": {}},                           # empty data object
        {"data": {"amount": "50.25"}},          # base missing
        {"data": {"base": "USDT", "currency": "EGP"}},  # amount missing
        {"data": {"amount": None}},             # amount is null
    ],
)
def test_coinbase_missing_fields_rejected(payload):
    with _patch_coinbase(FakeResponse(payload)):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


@pytest.mark.parametrize(
    "base,currency",
    [
        ("BTC", "EGP"),     # wrong base asset
        ("USDT", "USD"),    # wrong quote asset
        ("usdt", "egp"),    # correct pair, wrong letter case
        ("", ""),           # empty labels
    ],
)
def test_coinbase_wrong_pair_labels_rejected(base, currency):
    payload = {"data": {"amount": "50.25", "base": base, "currency": currency}}
    with _patch_coinbase(FakeResponse(payload)):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


@pytest.mark.parametrize(
    "amount",
    [
        "abc",        # not numeric
        "",           # empty
        "0",          # zero
        "0.00",       # zero with decimals
        "-5",         # negative
        "1e3",        # scientific notation
        " 50.25 ",    # surrounding whitespace
        "50,25",      # grouping separator
        "NaN",        # not a number
        "Infinity",   # not finite
    ],
)
def test_coinbase_invalid_amounts_rejected(amount):
    with _patch_coinbase(FakeResponse(_coinbase_payload(amount))):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
def test_coinbase_http_error_statuses_rejected(status):
    with _patch_coinbase(FakeResponse(_coinbase_payload(), status=status)):
        with pytest.raises(rate_api.RateProviderError) as excinfo:
            rate_api.fetch_usdt_egp_quote_coinbase()
    assert "HTTP" in str(excinfo.value)


def test_coinbase_timeout_rejected():
    with patch.object(
        rate_api, "urlopen", side_effect=TimeoutError("read timed out")
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


def test_coinbase_connection_error_rejected():
    with patch.object(
        rate_api, "urlopen", side_effect=OSError("connection refused")
    ):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


def test_coinbase_invalid_json_rejected():
    with _patch_coinbase(FakeResponse(raw=b"<html>oops</html>")):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


def test_coinbase_oversized_body_rejected():
    # A body at/over the bound may have been truncated — never parsed.
    oversized = b"x" * (rate_api.MAX_RESPONSE_BYTES + 1)
    with _patch_coinbase(FakeResponse(raw=oversized)):
        with pytest.raises(rate_api.RateProviderError) as excinfo:
            rate_api.fetch_usdt_egp_quote_coinbase()
    assert "size" in str(excinfo.value).lower()


def test_coinbase_non_dict_response_rejected():
    with _patch_coinbase(FakeResponse(raw=b"[]")):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


def test_coinbase_bool_amount_rejected():
    # JSON `true` must never be coerced into a money value.
    with _patch_coinbase(FakeResponse(_coinbase_payload(True))):
        with pytest.raises(rate_api.RateProviderError):
            rate_api.fetch_usdt_egp_quote_coinbase()


def test_coinbase_numeric_amount_is_accepted_as_text():
    # A JSON number is coerced with str() and then canonical-checked;
    # 50.25 stays exact, while 50.20 must NOT be invented.
    with _patch_coinbase(FakeResponse(_coinbase_payload(50.25))):
        quote = rate_api.fetch_usdt_egp_quote_coinbase()
    assert str(quote.rate_usdt_egp) == "50.25"

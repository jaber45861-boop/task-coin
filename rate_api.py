"""
Automatic USDT/EGP Quote Fetcher
================================

Fetches the current USDT/EGP market quote from a crypto price provider
— the HTTP boundary of the automatic rate path (``rate_worker`` →
this module → ``rate_store.set_automatic_rate``).

Two fetchers live here, both with an identical fail-closed contract:

- :func:`fetch_usdt_egp_quote` — CoinGecko
  (``api.coingecko.com``).  NOTE: CoinGecko's PUBLIC tier does not
  return the ``egp`` field for tether (the response is
  ``{"tether":{}}``), so this fetcher succeeds only with a plan/endpoint
  that serves it — verified against the live API, not assumed.
- :func:`fetch_usdt_egp_quote_coinbase` — Coinbase's public price API
  (``api.coinbase.com``).  This is the provider that actually supplies
  a DIRECT USDT/EGP spot quote with no API key, and is the one the
  automatic rate worker uses.

Contract (both fetchers):

- fetches and VALIDATES only; it writes no financial state and never
  touches the database — persistence belongs to ``rate_store``;
- the returned ``RateQuote`` carries the provider constant
  (``rate_quote.PROVIDER_COINGECKO`` / ``PROVIDER_COINBASE`` — the
  single shared constants) and
  ``captured_at`` = the LOCAL aware-UTC fetch instant, never a
  provider-supplied timestamp;
- every failure (timeout, connection error, HTTP status != 200,
  non-JSON body, missing/zero/negative/malformed value, naive
  timestamp) raises ``RateProviderError`` — fail-closed: a bad
  response never becomes a quote, and callers keep the previous quote
  only for audit until its TTL expires (``rate_store``).

Configuration (environment, all optional):

- ``TASKCOIN_RATE_API_URL``  override the endpoint base (tests /
  mirrors).  NOTE: this single variable is read by BOTH fetchers, so a
  mirror/base override applies to whichever fetcher is invoked.
- ``COINGECKO_API_KEY``      sent as ``x-cg-demo-api-key`` when set
                              (CoinGecko fetcher only; the Coinbase
                              fetcher needs no key)

Meaning and units: ``rate_usdt_egp`` is EGP per 1 USDT
(``1 USDT = rate EGP``), the ``rate_quote.RateQuote`` contract.  A
market quote is a REFERENCE price from a single venue, not a
guaranteed execution price.

Run:
    python3 -m pytest test_rate_api.py -v
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from rate_quote import (
    PROVIDER_COINGECKO,
    PROVIDER_COINBASE,
    RateQuote,
    RateValidationError,
    parse_rate,
)

DEFAULT_ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"
DEFAULT_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 65536

# Coinbase's public price API serves a DIRECT USDT/EGP spot quote and,
# unlike CoinGecko's public tier, actually returns the ``egp`` field.
# No API key and no subscription are required.
COINBASE_ENDPOINT = "https://api.coinbase.com/v2/prices"
COINBASE_PAIR = "USDT-EGP"
COINBASE_BASE_ASSET = "USDT"
COINBASE_QUOTE_ASSET = "EGP"


class RateProviderError(RuntimeError):
    """The external quote could not be retrieved or trusted."""


def fetch_usdt_egp_quote(
    *,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RateQuote:
    """Fetch the current tether/EGP market quote from CoinGecko.

    The endpoint, optional API key and timeout are configurable by
    env.  Invalid, missing, non-positive, malformed or non-JSON
    responses fail closed.  The capture timestamp is the local UTC
    fetch time (or the injected ``now``), never a provider timestamp.

    Raises:
        RateProviderError: any transport or payload problem — the
            caller must NOT persist anything.
        RateStoreError-style argument failures are impossible here:
            a naive ``now`` is rejected as ``RateProviderError`` too.
    """
    endpoint = os.environ.get("TASKCOIN_RATE_API_URL", DEFAULT_ENDPOINT)
    api_key = os.environ.get("COINGECKO_API_KEY", "").strip()
    params = urlencode({"ids": "tether", "vs_currencies": "egp"})
    url = f"{endpoint}?{params}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "task-coin-rate/1.0",
    }
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RateProviderError(
                    f"rate provider returned HTTP {response.status}"
                )
            payload = json.loads(
                response.read(MAX_RESPONSE_BYTES).decode("utf-8")
            )
    except RateProviderError:
        raise
    except Exception as exc:
        # Transport, timeout, truncated body or malformed JSON — one
        # fail-closed bucket, never a half-built quote.
        raise RateProviderError("rate provider request failed") from exc

    try:
        value = payload["tether"]["egp"]
        rate = parse_rate(str(value))
    except (KeyError, TypeError, ValueError, RateValidationError) as exc:
        raise RateProviderError(
            "rate provider response has no valid tether/egp quote"
        ) from exc

    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RateProviderError("quote timestamp must be timezone-aware")
    return RateQuote(rate, PROVIDER_COINGECKO, captured_at)


def fetch_usdt_egp_quote_coinbase(
    *,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    endpoint: str | None = None,
) -> RateQuote:
    """Fetch the current USDT/EGP spot quote from Coinbase.

    Contract (mirrors :func:`fetch_usdt_egp_quote`):

    - fetches and VALIDATES only; it writes no financial state and never
      touches the database — persistence belongs to ``rate_store``;
    - the returned ``RateQuote`` carries provider ``coinbase``
      (``rate_quote.PROVIDER_COINBASE``) and ``captured_at`` = the LOCAL
      aware-UTC fetch instant, never a provider-supplied timestamp;
    - the response is checked for HTTP 200, a bounded body, and the
      expected ``data.amount`` / ``data.base`` / ``data.currency``
      shape BEFORE the price is parsed — a partial, mislabelled or
      non-numeric payload is never half-trusted;
    - every failure (timeout, connection error, HTTP status != 200,
      non-JSON body, oversized body, missing/zero/negative/malformed
      value, wrong base or quote asset) raises ``RateProviderError`` —
      fail-closed: a bad response never becomes a quote, and the caller
      must NOT persist anything;
    - the price is a REFERENCE market price from one venue (Coinbase),
      not a guaranteed execution price — see ``rate_quote``.

    Raises:
        RateProviderError: any transport or payload problem.
    """
    base_url = endpoint or os.environ.get(
        "TASKCOIN_RATE_API_URL", COINBASE_ENDPOINT
    )
    url = f"{base_url.rstrip('/')}/{COINBASE_PAIR}/spot"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "task-coin-rate/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RateProviderError(
                    f"rate provider returned HTTP {response.status}"
                )
            raw = response.read(MAX_RESPONSE_BYTES)
            if len(raw) >= MAX_RESPONSE_BYTES:
                # Refuse a body that may have been truncated: a partial
                # JSON document must never be parsed into a quote.
                raise RateProviderError(
                    "rate provider response exceeded the size bound"
                )
            payload = json.loads(raw.decode("utf-8"))
    except RateProviderError:
        raise
    except Exception as exc:
        # Transport, timeout, oversized/truncated body or malformed JSON
        # — one fail-closed bucket, never a half-built quote.
        raise RateProviderError("rate provider request failed") from exc

    if not isinstance(payload, dict):
        raise RateProviderError("rate provider response is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RateProviderError(
            "rate provider response has no data object"
        )

    base_asset = data.get("base")
    quote_asset = data.get("currency")
    # The pair is explicit in the URL; a mismatched label means the
    # provider answered with something other than the requested quote.
    if base_asset != COINBASE_BASE_ASSET:
        raise RateProviderError(
            f"rate provider returned base asset {base_asset!r}, "
            f"expected {COINBASE_BASE_ASSET!r}"
        )
    if quote_asset != COINBASE_QUOTE_ASSET:
        raise RateProviderError(
            f"rate provider returned quote asset {quote_asset!r}, "
            f"expected {COINBASE_QUOTE_ASSET!r}"
        )

    value = data.get("amount")
    try:
        rate = parse_rate(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError, RateValidationError) as exc:
        raise RateProviderError(
            "rate provider response has no valid USDT/EGP amount"
        ) from exc

    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RateProviderError("quote timestamp must be timezone-aware")
    return RateQuote(rate, PROVIDER_COINBASE, captured_at)

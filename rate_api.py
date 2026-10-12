"""
Automatic USDT/EGP Quote Fetcher
================================

Fetches the current tether/EGP market quote from CoinGecko — the
HTTP boundary of the automatic rate path (``rate_worker`` → this
module → ``rate_store.set_automatic_rate``).

Contract:

- fetches and VALIDATES only; it writes no financial state and never
  touches the database — persistence belongs to ``rate_store``;
- the returned ``RateQuote`` carries provider ``coingecko``
  (``rate_quote.PROVIDER_COINGECKO`` — the single shared constant) and
  ``captured_at`` = the LOCAL aware-UTC fetch instant, never a
  provider-supplied timestamp;
- every failure (timeout, connection error, HTTP status != 200,
  non-JSON body, missing/zero/negative/malformed value, naive
  timestamp) raises ``RateProviderError`` — fail-closed: a bad
  response never becomes a quote, and callers keep the previous quote
  only for audit until its TTL expires (``rate_store``).

Configuration (environment, all optional):

- ``TASKCOIN_RATE_API_URL``  override the endpoint (tests / mirrors)
- ``COINGECKO_API_KEY``      sent as ``x-cg-demo-api-key`` when set

Meaning and units: ``rate_usdt_egp`` is EGP per 1 USDT
(``1 USDT = rate EGP``), the ``rate_quote.RateQuote`` contract.  A
market quote is a REFERENCE price, not a guaranteed execution price.

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
    RateQuote,
    RateValidationError,
    parse_rate,
)

DEFAULT_ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"
DEFAULT_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 65536


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

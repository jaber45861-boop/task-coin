"""
Withdrawal Rate Quote Contract (MT-ADMIN-20)
============================================

Rate-contract FOUNDATION only: the small, exact boundary that future
withdrawal code can consume without inventing a live rate provider.

No rate fetching, no HTTP client, no external integration, no wallet /
ledger / withdrawal mutation, and no database writes happen in this
module — every function here is pure.

Rate meaning
------------
``rate_usdt_egp`` is **EGP per 1 USDT**:  ``1 USDT = rate_usdt_egp EGP``.
Example: rate 48.5 means one USDT is worth 48.5 EGP.

Representation
--------------
- Internal: ``decimal.Decimal`` — exact.  ``float`` and ``bool`` are
  rejected at every entry point; there is no binary floating-point
  conversion anywhere (``test_rate_quote.py`` AST-scans this source).
- Persistence boundary: canonical plain-decimal TEXT, compatible with
  the existing ``withdrawal_requests.wallet_rate_usdt_egp TEXT`` /
  ``rate_provider TEXT`` / ``rate_captured_at TIMESTAMP`` columns.

Canonical parsing (``parse_rate``)
----------------------------------
- ``str`` input must be PLAIN decimal text matching
  ``[0-9]+(?:\\.[0-9]+)?`` — no surrounding whitespace, no signs, no
  malformed forms, and **no scientific notation**.  This follows the
  project's canonical decimal-text convention established by
  ``platform_settings._DECIMAL_TEXT_RE`` (scientific notation is
  rejected there too).  NOTE: ``withdrawal_rules._parse_decimal``
  historically accepts ``"1e5"``-style strings; that looser behavior
  is deliberately NOT copied here — string inputs at a persistence
  boundary must be plain text, and this difference is pinned by test.
- ``Decimal`` input is accepted as an exact VALUE regardless of the
  notation it was constructed with (``Decimal("1E+2")`` == 100) and
  is serialized in plain notation, value-preserving.
- ``int`` is accepted (an exact whole rate); ``bool`` is rejected even
  though ``bool`` subclasses ``int``.
- ``float``, empty/malformed strings, NaN, Infinity, zero and negative
  values are always rejected — never rounded, never quantized to an
  invented precision.
- ``canonical_rate_text`` renders the exact value in plain (never
  scientific) notation, stripping trailing zeros with an exact
  precision-safe ``normalize()`` — same value always yields the same
  text, and no rounding can occur.

Providers
---------
The ONLY approved source today is ``"manual"`` (``PROVIDER_MANUAL``).
There are no fake providers and nothing here fetches anything.  A
future provider simply constructs the same ``RateQuote(rate, provider,
captured_at)`` — the consumer contract never changes.

Timestamp
---------
``captured_at`` must be a timezone-aware ``datetime``; naive local time
is rejected (no local-time ambiguity).  It is immutable with the rest
of the quote.  ``canonical_timestamp_text`` serializes deterministically
as UTC ISO-8601 with an explicit offset (``...+00:00``).

Conversion helpers (pure, optional convenience — NOT wired into
``WithdrawalService``)
------------------------------------------------------------------------
- ``egp_to_wallet_units``: EGP → USDT atomic units, ROUND_CEILING —
  never under-holds (matches ``withdrawal_rules.egp_to_usdt``).
- ``usdt_units_to_egp_display``: USDT atomic units → EGP display,
  ROUND_HALF_UP to 2 dp (matches ``withdrawal_rules.egp_equivalent``).
Both require an explicit ``RateQuote``, fetch nothing, and mutate
nothing.  A USDT withdrawal never round-trips USDT → EGP → USDT: it
simply does not call the EGP helpers.

Run:
    python -m pytest test_rate_quote.py -v
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    ROUND_CEILING,
    ROUND_HALF_UP,
    Decimal,
    localcontext,
)

import wallet
from withdrawal_rules import EGP_QUANTUM

# ── Rate semantics / precision ────────────────────────────────────────

DIVISION_PRECISION = 60          # same as withdrawal_rules
WALLET_UNITS_SCALE = wallet.USDT_SCALE   # 100_000_000 units per USDT

# Canonical plain-decimal text (platform_settings convention: no
# scientific notation, no signs, no whitespace, no grouping).
_CANONICAL_DECIMAL_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


# ── Provider boundary ────────────────────────────────────────────────

PROVIDER_MANUAL = "manual"
APPROVED_RATE_PROVIDERS = frozenset({PROVIDER_MANUAL})


# ── Errors ───────────────────────────────────────────────────────────


class RateQuoteError(Exception):
    """Base class for every rate-contract failure."""


class RateValidationError(RateQuoteError):
    """A rate/amount value failed exact validation (no rounding)."""


class UnknownRateProviderError(RateQuoteError):
    """The provider id is not an approved rate source."""


class NaiveTimestampError(RateQuoteError):
    """``captured_at`` must be timezone-aware — local time is ambiguous."""


# ── Canonical parsing ────────────────────────────────────────────────


def parse_rate(value: object) -> Decimal:
    """Exact input → validated positive finite ``Decimal`` rate.

    Raises:
        RateValidationError: float/bool, unsupported type, empty or
            malformed text (including scientific notation), NaN,
            Infinity, zero or a negative value.  Nothing is ever
            rounded or invented.
    """
    if isinstance(value, bool):
        raise RateValidationError(
            "rate: bool is not a rate value; pass Decimal or str"
        )
    if isinstance(value, float):
        raise RateValidationError(
            "rate: float is forbidden in financial math; "
            "pass Decimal or str"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        # Plain decimal text only — deliberately stricter than
        # withdrawal_rules._parse_decimal: no scientific notation,
        # signs or surrounding whitespace at a persistence boundary.
        if not _CANONICAL_DECIMAL_RE.fullmatch(value):
            raise RateValidationError(
                "rate: not canonical plain decimal text "
                f"(no scientific notation/signs/whitespace): {value!r}"
            )
        dec = Decimal(value)
    else:
        raise RateValidationError(
            f"rate: unsupported type {type(value).__name__}; "
            "pass Decimal or str"
        )
    if not dec.is_finite():
        raise RateValidationError(f"rate: must be finite, got {value!r}")
    if dec <= 0:
        raise RateValidationError(f"rate: must be > 0, got {dec}")
    return dec


def canonical_rate_text(rate: object) -> str:
    """Exact rate → canonical plain-decimal TEXT for SQLite storage.

    Same value always produces the same text (``"48.500"`` →
    ``"48.5"``), the output NEVER contains scientific notation, and no
    rounding is possible: the precision-safe normalize only strips
    trailing zeros.
    """
    dec = parse_rate(rate)
    sign, digits, exponent = dec.as_tuple()
    with localcontext() as ctx:
        # prec > digit count ⇒ normalize() can only strip zeros,
        # never round the coefficient.
        ctx.prec = max(len(digits) + 1, 28)
        stripped = dec.normalize()
    return format(stripped, "f")


# ── Provider validation ──────────────────────────────────────────────


def validate_rate_provider(provider: object) -> str:
    """Return an approved provider id or raise.

    The only approved source today is ``"manual"`` — no other id is
    accepted, so no fake provider can ever back a rate quote.
    """
    if not isinstance(provider, str) or provider == "":
        raise UnknownRateProviderError(
            f"rate provider must be a non-empty str, got {provider!r}"
        )
    if provider not in APPROVED_RATE_PROVIDERS:
        raise UnknownRateProviderError(
            f"unknown rate provider {provider!r}; "
            f"approved: {sorted(APPROVED_RATE_PROVIDERS)}"
        )
    return provider


# ── Timestamp handling ───────────────────────────────────────────────


def _require_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise RateValidationError(
            f"captured_at must be a datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise NaiveTimestampError(
            "captured_at must be timezone-aware; naive local time is "
            "ambiguous and is never accepted"
        )
    return value


def canonical_timestamp_text(captured_at: object) -> str:
    """Timezone-aware datetime → deterministic UTC ISO-8601 TEXT.

    Example: ``2026-09-27T10:00:00+00:00``.  The explicit offset makes
    the value unambiguous regardless of the host's local timezone, and
    the same instant always serializes to the same string.
    """
    aware = _require_aware_timestamp(captured_at)
    return aware.astimezone(timezone.utc).isoformat()


# ── The immutable value object ───────────────────────────────────────


@dataclass(frozen=True)
class RateQuote:
    """Immutable, exact snapshot of an EGP-per-USDT rate.

    Constructed once (validated and normalized in ``__post_init__``)
    and never mutable afterward: the rate, provider and captured
    timestamp can not change after creation, so a consumer can always
    trust the quote it holds.

    ``rate_usdt_egp``: 1 USDT = ``rate_usdt_egp`` EGP.
    ``provider``: approved source id (only ``"manual"`` today).
    ``captured_at``: timezone-aware instant the rate applies from.
    """

    rate_usdt_egp: Decimal
    provider: str
    captured_at: datetime

    def __post_init__(self) -> None:
        # Construction-time validation/normalization only (the frozen
        # instance itself is never mutated afterwards).
        object.__setattr__(
            self, "rate_usdt_egp", parse_rate(self.rate_usdt_egp)
        )
        object.__setattr__(
            self, "provider", validate_rate_provider(self.provider)
        )
        _require_aware_timestamp(self.captured_at)

    @property
    def rate_text(self) -> str:
        """Canonical plain-decimal TEXT of the pinned rate."""
        return canonical_rate_text(self.rate_usdt_egp)


def rate_persistence_fields(quote: RateQuote) -> dict:
    """Pure serialization of a quote into the existing column values.

    Returns exactly::

        {"wallet_rate_usdt_egp": <plain-decimal TEXT>,
         "rate_provider":        <approved provider id TEXT>,
         "rate_captured_at":     <UTC ISO-8601 TEXT>}

    compatible with the existing ``withdrawal_requests`` TEXT/TIMESTAMP
    columns.  Nothing is written to the database here — the caller
    decides if/when to persist.  (The payout-side ``rate_usdt_egp``
    column's keep/retire semantics are deliberately not decided by
    this contract task.)
    """
    if not isinstance(quote, RateQuote):
        raise RateQuoteError(
            f"quote must be a RateQuote, got {type(quote).__name__}"
        )
    return {
        "wallet_rate_usdt_egp": canonical_rate_text(quote.rate_usdt_egp),
        "rate_provider": quote.provider,
        "rate_captured_at": canonical_timestamp_text(quote.captured_at),
    }


# ── Pure conversion helpers (explicit rounding, never wired in yet) ──


def _require_quote(quote: object) -> RateQuote:
    if not isinstance(quote, RateQuote):
        raise RateQuoteError(
            "an explicit RateQuote is required — rates are never "
            f"fetched or invented here (got {type(quote).__name__})"
        )
    return quote


def _require_amount_decimal(value: object, *, field: str) -> Decimal:
    """Exact non-negative amount (float/bool/NaN/Inf rejected)."""
    if isinstance(value, bool):
        raise RateValidationError(f"{field}: bool is not a money value")
    if isinstance(value, float):
        raise RateValidationError(
            f"{field}: float is forbidden in financial math"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        if not _CANONICAL_DECIMAL_RE.fullmatch(value):
            raise RateValidationError(
                f"{field}: not canonical plain decimal text: {value!r}"
            )
        dec = Decimal(value)
    else:
        raise RateValidationError(
            f"{field}: unsupported type {type(value).__name__}"
        )
    if not dec.is_finite():
        raise RateValidationError(f"{field}: must be finite, got {value!r}")
    if dec < 0:
        raise RateValidationError(f"{field}: must not be negative, got {dec}")
    return dec


def egp_to_wallet_units(amount_egp: object, quote: RateQuote) -> int:
    """EGP amount → USDT atomic units at the quote's rate.

    ROUND_CEILING (established MT-ADMIN-17 contract): the wallet never
    under-holds — 10 EGP at 48.5 EGP/USDT requires 20,618,557 units
    (0.20618557 USDT), never 1 unit less.

    Pure: requires an explicit ``RateQuote``, fetches nothing, mutates
    nothing (no wallet/ledger/withdrawal state is touched).  A zero
    amount converts to exactly 0 units.
    """
    _require_quote(quote)
    amount = _require_amount_decimal(amount_egp, field="amount_egp")
    if amount == 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = DIVISION_PRECISION
        units = (amount / quote.rate_usdt_egp * WALLET_UNITS_SCALE)
        integral = units.to_integral_value(rounding=ROUND_CEILING)
    return int(integral)


def usdt_units_to_egp_display(amount_units: object, quote: RateQuote) -> Decimal:
    """USDT atomic units → EGP display amount at the quote's rate.

    ROUND_HALF_UP to ``EGP_QUANTUM`` (0.01 EGP — established
    MT-ADMIN-17 contract): display/payout calculation only.  This
    helper is ONE-WAY; a USDT withdrawal must never call it as part of
    a USDT → EGP → USDT round trip.

    Pure: requires an explicit ``RateQuote``, fetches nothing, mutates
    nothing.  Zero units display as ``Decimal("0.00")``.
    """
    _require_quote(quote)
    if isinstance(amount_units, bool) or not isinstance(amount_units, int):
        raise RateValidationError(
            f"amount_units: must be an int of USDT atomic units, "
            f"got {type(amount_units).__name__}"
        )
    if amount_units < 0:
        raise RateValidationError(
            f"amount_units: must not be negative, got {amount_units}"
        )
    if amount_units == 0:
        return Decimal("0.00")
    with localcontext() as ctx:
        ctx.prec = DIVISION_PRECISION
        egp = Decimal(amount_units) / WALLET_UNITS_SCALE * quote.rate_usdt_egp
    return egp.quantize(EGP_QUANTUM, rounding=ROUND_HALF_UP)

"""
Withdrawal Currency Contract & Error Boundary (MT-ADMIN-21)
===========================================================

CONTRACT + PURE HELPERS + ERROR TRANSLATION ONLY — this is NOT the
production withdrawal service: no SQLite repository, no creation /
rejection / completion flow, no wallet reservation, no ledger writes,
no settings consumption, no payment-method selection, no DB connection
is ever opened here.

Currency / unit contract
------------------------
1. INTERNAL WALLET (``wallets`` / ``ledger.amount_units``)
   - Currency: USDT only.  Unit: integer atomic units.
   - ``1 USDT = 100,000,000 units`` (``wallet.USDT_SCALE``).
   - EGP values never enter wallet/ledger money movement.

2. VODAFONE CASH (EGP payout)
   - User amount is EGP, represented as INTEGER minor units:
     ``1 EGP = 100 minor units``.
   - A ``RateQuote`` (MT-ADMIN-20, ``1 USDT = rate EGP``) converts the
     EGP amount + EGP fee into the wallet debit, ROUND_CEILING —
     never under-hold, ONE conversion of the total (no
     USDT → EGP → USDT round trip).
   - The resulting int is the authoritative ``wallet_debit_units``.

3. USDT CRYPTO (USDT payout)
   - User amount is USDT atomic units; ``wallet_debit_units`` = amount
     + fee as a plain integer sum.  This path takes NO rate and calls
     NO EGP conversion — it cannot round-trip through EGP even by
     accident (``usdt_wallet_debit`` has no rate parameter at all).

4. RATE
   - Uses the existing ``RateQuote`` (MT-ADMIN-20) exclusively; no
     second rate representation exists here.

5. UNIT BOUNDARY
   - ``UsdtAtomicUnits`` / ``EgpMinorUnits`` are frozen,
     integer-validated value objects with DISTINCT attribute names
     (``.units`` vs ``.minor``).  The validators accept either raw
     ``int`` or the MATCHING wrapper and reject the wrong wrapper with
     a domain ``ValidationError`` — passing EGP minor where USDT units
     are expected (or vice versa) fails loudly.

6. ROUNDING (established MT-ADMIN-17 contract, reused from
   ``rate_quote`` — never re-implemented here):
   - EGP → USDT atomic units: ROUND_CEILING
   - USDT → EGP display: ROUND_HALF_UP to 2 EGP decimals
   - no float, no implicit rounding, no hidden conversion.

Every monetary argument and return value is documented with its unit.
Every ``wallet_debit_units`` result is a plain ``int`` of USDT atomic
units.  ``minimum_withdrawal_units`` / ``withdrawal_fee_units`` are
deliberately NOT consumed (deferred to MT-ADMIN-24).

Error boundary
--------------
Lower-level errors (``wallet``, ``rate_quote``, ``payment_method_store``,
SQLite) are mapped into ``withdrawal_rules`` domain errors by
:func:`translate_to_domain_error` / :func:`translated_errors`:

============================================ =========================================
source error                                 withdrawal-domain target
============================================ =========================================
``wallet.InsufficientBalanceError``          ``InsufficientBalanceError`` (available)
``wallet.InsufficientHeldBalanceError``      ``InsufficientHeldBalanceError`` (held)
``wallet.InvalidWalletAmountError``          ``InvalidAmountError``
``rate_quote.RateQuoteError`` family         ``InvalidRateError``
``ledger.InvalidLedgerEntryError``           ``ValidationError``
``payment_method_store...NotFoundError``     ``PaymentMethodUnavailableError``
``payment_method_store...InactiveError``     ``PaymentMethodUnavailableError``
``payment_method_store...ValidationError``   ``ValidationError``
``sqlite3.IntegrityError`` on                ``PendingWithdrawalExistsError``
  ``UNIQUE constraint failed:                (the ux_withdrawals_one_pending
  withdrawal_requests.user_id``               partial index — see below)
``withdrawal_rules.WithdrawalError``         passed through unchanged
anything else                                returned/re-raised UNCHANGED
============================================ =========================================

- Invalid *state* is already a domain concept (``InvalidStateError``)
  and passes through untouched; the future service raises it directly.
- The duplicate-pending match is exact-string and deterministic: per
  the current schema the message ``UNIQUE constraint failed:
  withdrawal_requests.user_id`` can ONLY come from the partial unique
  index ``ux_withdrawals_one_pending`` (``user_id`` itself is not
  UNIQUE; PK violations name ``request_id`` instead).
- Unexpected exceptions are NEVER swallowed and NEVER re-labeled: the
  translator returns them as-is and the context manager re-raises the
  original object.

Run:
    python -m pytest test_withdrawal_contract.py -v
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

import payment_method_store
import rate_quote
import wallet
import withdrawal_rules
from ledger import InvalidLedgerEntryError
from rate_quote import RateQuote

# ── Unit scales (single sources of truth live elsewhere) ─────────────

USDT_UNITS_PER_USDT: int = wallet.USDT_SCALE      # 100_000_000 units
EGP_MINOR_PER_EGP: int = 100                      # minor units per EGP

# Mirrors db's signed-SQLite-INTEGER bound (copied, not imported, so
# this module depends on public surface only — same convention as
# platform_settings._SQLITE_INT64_MAX).
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807


# ── Frozen integer unit value objects (the explicit boundary) ────────


def _validate_unit_value(value: object, *, name: str) -> int:
    """Type half of unit validation: exact int only.

    Type/unit problems raise domain ``ValidationError``; value
    problems (negative / out of int64 range) raise domain
    ``InvalidAmountError``.  Zero is representable at the type level
    (positive requirements are the amount validators' job).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise withdrawal_rules.ValidationError(
            f"{name}: float/bool are forbidden in financial math; "
            f"pass an exact int"
        )
    if not isinstance(value, int):
        raise withdrawal_rules.ValidationError(
            f"{name}: must be an exact int, "
            f"got {type(value).__name__}"
        )
    if value < 0:
        raise withdrawal_rules.InvalidAmountError(
            f"{name}: must not be negative, got {value}"
        )
    if value > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            f"{name}: exceeds the SQLite INTEGER range, got {value}"
        )
    return value


@dataclass(frozen=True)
class UsdtAtomicUnits:
    """USDT atomic wallet units — integer only (1 USDT = 100,000,000).

    Frozen and int-validated; the attribute is ``.units`` (distinct
    from :attr:`EgpMinorUnits.minor`, so the wrong wrapper cannot be
    used where this one is expected — its type check fails first).
    """

    units: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "units",
            _validate_unit_value(self.units, name="units"),
        )


@dataclass(frozen=True)
class EgpMinorUnits:
    """EGP minor units — integer only (1 EGP = 100 minor units).

    Frozen and int-validated; the attribute is ``.minor`` (distinct
    from :attr:`UsdtAtomicUnits.units`).
    """

    minor: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "minor",
            _validate_unit_value(self.minor, name="minor"),
        )


# ── Amount validators (raw int or the MATCHING wrapper) ───────────────


def _coerce_units(
    value: object,
    *,
    expected: type,
    expected_label: str,
    wrong: type,
    wrong_label: str,
    field: str,
) -> int:
    """Accept ``int`` or the matching wrapper; reject the wrong unit."""
    if isinstance(value, wrong):
        raise withdrawal_rules.ValidationError(
            f"{field}: {wrong_label} passed where {expected_label} "
            "expected — the two units are never interchangeable"
        )
    if isinstance(value, expected):
        value = value.units if expected is UsdtAtomicUnits else value.minor
    if isinstance(value, bool) or isinstance(value, float):
        raise withdrawal_rules.ValidationError(
            f"{field}: float/bool are forbidden in financial math; "
            f"pass exact {expected_label}"
        )
    if not isinstance(value, int):
        raise withdrawal_rules.ValidationError(
            f"{field}: must be exact {expected_label} (int), "
            f"got {type(value).__name__}"
        )
    return value


def _require_range(value: int, *, minimum: int, field: str) -> int:
    """Value half: zero/negative and int64 overflow → InvalidAmountError."""
    if value < minimum:
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: "
            + ("must be positive" if minimum == 1 else "must not be negative")
            + f", got {value}"
        )
    if value > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: exceeds the SQLite INTEGER range, got {value}"
        )
    return value


def require_positive_usdt_units(
    value: object, *, field: str = "amount_units"
) -> int:
    """Validate a POSITIVE USDT atomic-unit amount → plain ``int``.

    Accepts an exact ``int`` or :class:`UsdtAtomicUnits`; rejects EGP
    minor wrappers, float/bool, zero, negatives and int64 overflow.
    Unit: USDT atomic units (1 USDT = 100,000,000).
    """
    units = _coerce_units(
        value,
        expected=UsdtAtomicUnits, expected_label="USDT atomic units",
        wrong=EgpMinorUnits, wrong_label="EGP minor units",
        field=field,
    )
    return _require_range(units, minimum=1, field=field)


def require_non_negative_usdt_units(
    value: object, *, field: str = "fee_units"
) -> int:
    """Validate a NON-NEGATIVE USDT atomic-unit amount (fees) → ``int``.

    Zero is allowed (the schema's ``fee_native_minor >= 0`` allows a
    zero fee); negatives, wrong units, float/bool and overflow are not.
    """
    units = _coerce_units(
        value,
        expected=UsdtAtomicUnits, expected_label="USDT atomic units",
        wrong=EgpMinorUnits, wrong_label="EGP minor units",
        field=field,
    )
    return _require_range(units, minimum=0, field=field)


def require_positive_egp_minor(
    value: object, *, field: str = "amount_egp_minor"
) -> int:
    """Validate a POSITIVE EGP minor-unit amount → plain ``int``.

    Unit: EGP minor units (1 EGP = 100 minor).  Accepts an exact
    ``int`` or :class:`EgpMinorUnits`; rejects USDT wrappers, float/
    bool, zero, negatives and int64 overflow.
    """
    minor = _coerce_units(
        value,
        expected=EgpMinorUnits, expected_label="EGP minor units",
        wrong=UsdtAtomicUnits, wrong_label="USDT atomic units",
        field=field,
    )
    return _require_range(minor, minimum=1, field=field)


def require_non_negative_egp_minor(
    value: object, *, field: str = "fee_egp_minor"
) -> int:
    """Validate a NON-NEGATIVE EGP minor-unit amount (fees) → ``int``."""
    minor = _coerce_units(
        value,
        expected=EgpMinorUnits, expected_label="EGP minor units",
        wrong=UsdtAtomicUnits, wrong_label="USDT atomic units",
        field=field,
    )
    return _require_range(minor, minimum=0, field=field)


# ── Wallet-debit calculators (the authoritative wallet amount) ────────


def egp_minor_to_wallet_debit(
    *,
    amount_egp_minor: object,
    fee_egp_minor: object,
    quote: RateQuote,
) -> int:
    """Vodafone path: EGP minor + EGP minor → **USDT atomic units**.

    Converts ``amount + fee`` (EGP) in ONE ROUND_CEILING conversion via
    ``rate_quote.egp_to_wallet_units`` — the established
    MT-ADMIN-17 "never under-hold" rule, reused not re-implemented.
    No USDT → EGP → USDT round trip: the total crosses the currency
    boundary exactly once.

    Args:
        amount_egp_minor: user payout, EGP minor units (1 EGP = 100).
        fee_egp_minor: fee, EGP minor units (>= 0 allowed).
        quote: explicit MT-ADMIN-20 ``RateQuote`` (1 USDT = rate EGP).
            Never fetched or invented here.

    Returns:
        int — the authoritative ``wallet_debit_units`` in USDT atomic
        units (fits the SQLite INTEGER range).
    """
    amount = require_positive_egp_minor(
        amount_egp_minor, field="amount_egp_minor"
    )
    fee = require_non_negative_egp_minor(fee_egp_minor, field="fee_egp_minor")
    total_minor = amount + fee
    if total_minor > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            "total_egp_minor: exceeds the SQLite INTEGER range"
        )
    total_egp = Decimal(total_minor) / EGP_MINOR_PER_EGP  # exact, 2 dp
    units = rate_quote.egp_to_wallet_units(total_egp, quote)  # CEILING
    if units > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            "wallet_debit_units: exceeds the SQLite INTEGER range"
        )
    return int(units)


def usdt_wallet_debit(
    *,
    amount_units: object,
    fee_units: object,
) -> int:
    """USDT path: USDT atomic units + USDT atomic units → same units.

    ``wallet_debit_units`` is the plain integer sum — NO rate, NO EGP
    conversion, structurally no round trip (this function does not
    even accept a quote).  That is the contract for ``usdt_bep20``:
    the rate exists for display only, never for the wallet debit.

    Args:
        amount_units: user payout, USDT atomic units (> 0).
        fee_units: fee, USDT atomic units (>= 0 allowed).

    Returns:
        int — ``wallet_debit_units`` in USDT atomic units (fits the
        SQLite INTEGER range).
    """
    amount = require_positive_usdt_units(amount_units, field="amount_units")
    fee = require_non_negative_usdt_units(fee_units, field="fee_units")
    total = amount + fee
    if total > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            "wallet_debit_units: exceeds the SQLite INTEGER range"
        )
    return int(total)


# ── Error translation boundary ────────────────────────────────────────

# Exact SQLite text produced by the partial unique index
# ``ux_withdrawals_one_pending`` (verified): for a partial index SQLite
# reports the indexed column, NOT the index name; ``user_id`` itself is
# not UNIQUE, and PK violations name ``request_id`` instead — so this
# string identifies the duplicate-pending guard and nothing else.
_PENDING_VIOLATION = (
    "UNIQUE constraint failed: withdrawal_requests.user_id"
)

# Deterministic source → domain-target table (isinstance match, first
# hit wins; sources are listed before any broader base of theirs).
_TRANSLATIONS: tuple[tuple[tuple[type, ...], type], ...] = (
    (
        (wallet.InsufficientBalanceError,),
        withdrawal_rules.InsufficientBalanceError,
    ),
    (
        (wallet.InsufficientHeldBalanceError,),
        withdrawal_rules.InsufficientHeldBalanceError,
    ),
    (
        (wallet.InvalidWalletAmountError,),
        withdrawal_rules.InvalidAmountError,
    ),
    (
        # whole RateQuoteError family → domain rate error
        (rate_quote.RateQuoteError,),
        withdrawal_rules.InvalidRateError,
    ),
    (
        # invalid ledger entry data (bad ids/amounts) → domain input
        # error.  Ledger reference/idempotency CONFLICTS are deliberately
        # left unmapped: they are already precise ledger-domain errors
        # and re-labeling them would lose fidelity.
        (InvalidLedgerEntryError,),
        withdrawal_rules.ValidationError,
    ),
    (
        (
            payment_method_store.PaymentMethodNotFoundError,
            payment_method_store.PaymentMethodInactiveError,
        ),
        withdrawal_rules.PaymentMethodUnavailableError,
    ),
    (
        (payment_method_store.PaymentMethodValidationError,),
        withdrawal_rules.ValidationError,
    ),
)


def translate_to_domain_error(exc: BaseException) -> BaseException:
    """Map a lower-level error to its withdrawal-domain equivalent.

    Deterministic and side-effect free:

    - an error that is already a ``withdrawal_rules`` domain error is
      returned unchanged (covers ``InvalidStateError``, ``CooldownError``,
      request-not-found — invalid *state* is a domain concept);
    - the duplicate-pending ``sqlite3.IntegrityError`` (exact message
      match, see ``_PENDING_VIOLATION``) maps to
      ``PendingWithdrawalExistsError``;
    - known ``wallet`` / ``rate_quote`` / ``payment_method_store``
      errors map per the module docstring table;
    - ANY other exception is returned unchanged — never swallowed,
      never re-labeled.  Callers must re-raise it as-is.
    """
    if isinstance(exc, withdrawal_rules.WithdrawalError):
        return exc
    if isinstance(exc, sqlite3.IntegrityError) and _PENDING_VIOLATION in str(exc):
        return withdrawal_rules.PendingWithdrawalExistsError(str(exc))
    for sources, target in _TRANSLATIONS:
        if isinstance(exc, sources):
            return target(str(exc) or target.__name__)
    return exc


@contextmanager
def translated_errors():
    """Context manager form of the error boundary.

    Known lower-level errors are re-raised as their withdrawal-domain
    equivalent (chained with ``raise ... from exc``); domain errors
    pass through untouched; EVERYTHING else is re-raised as the
    original object — unexpected exceptions are never swallowed.
    """
    try:
        yield
    except withdrawal_rules.WithdrawalError:
        raise
    except Exception as exc:
        mapped = translate_to_domain_error(exc)
        if mapped is exc:
            raise  # unexpected → original object, original traceback
        raise mapped from exc

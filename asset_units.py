"""
Asset Units Registry (the ONE per-asset scale source)
=====================================================

Single source of truth for how many decimal places each supported
asset uses when an exact amount is parsed to integer atomic units
(and rendered back).  Every deposit-path conversion goes through
this module — no other module may hard-code an asset's scale or an
asset symbol to decide a scale.

Contract (fail-closed, no invented defaults):

- ``decimals_for`` NEVER guesses: an unknown or non-string asset
  raises ``UnknownAssetScaleError``.  There is no default scale for
  any asset, known or not.
- Lookup is NORMALIZED (trim + casefold) while the stored asset
  identity is never rewritten — ``"USDT"`` and ``" usdt "``
  resolve to the same scale, and nothing on disk is changed.
- Integer math only: exact decimal text, no float, no rounding,
  no silent precision loss.  Over-precision is rejected, never
  truncated.
- ``is_wallet_credit_asset`` names the internal wallet's ONE credit
  currency (the ledger's ``CHECK (currency = 'USDT')``) — the credit
  boundary uses it instead of an asset literal.

Registered scales:

- ``USDT`` → 8 decimals — authority: ``wallet.USDT_DECIMALS``.
- ``EGP``  → 2 decimals — authority: ``withdrawal_rules.EGP_QUANTUM``
  (0.01 → 2 dp); pinned by test_asset_units, not imported here to
  keep this registry free of the withdrawal domain.

Run:
    python3 -m pytest test_asset_units.py -q
"""

from __future__ import annotations

import re
from decimal import Decimal

from task_taxonomy import normalize_digits
import wallet

# Signed SQLite INTEGER bound — atomic units must fit, never REAL
# (mirrors db._SQLITE_INT64_MAX; copied so this module depends only
# on public foundations).
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

# ── The registry: NORMALIZED asset key → decimal places ──────────────

USDT_DECIMALS = wallet.USDT_DECIMALS          # 8 — wallet authority
EGP_DECIMALS = 2                              # == -EGP_QUANTUM exponent

_ASSET_DECIMALS: dict[str, int] = {
    "usdt": USDT_DECIMALS,
    "egp": EGP_DECIMALS,
}

# The internal wallet/ledger credit currency (normalized key only).
_WALLET_CREDIT_ASSET = "usdt"

# Canonical decimal text (same shape as the platform-settings parser):
# digits with an optional fractional part — no signs, no exponent.
_DECIMAL_TEXT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


# ── Errors (typed — callers map by type, never by message) ───────────


class AssetUnitsError(Exception):
    """Base class for every asset-scale/units failure."""


class UnknownAssetScaleError(AssetUnitsError):
    """The asset has no registered decimal scale — fail closed."""


class AssetAmountError(AssetUnitsError):
    """The value cannot be converted exactly at the asset's scale."""


# ── Registry reads ────────────────────────────────────────────────────


def _normalize(asset: object) -> str | None:
    """Normalized lookup key, or None for non-string/blank input."""
    if not isinstance(asset, str):
        return None
    key = asset.strip().casefold()
    return key or None


def decimals_for(asset: object) -> int:
    """Registered decimal places for *asset* — never a default.

    Raises:
        UnknownAssetScaleError: the asset is not a registered string
            (empty, non-string, or simply unknown).
    """
    key = _normalize(asset)
    decimals = _ASSET_DECIMALS.get(key) if key is not None else None
    if decimals is None:
        raise UnknownAssetScaleError(
            f"no registered decimal scale for asset {asset!r}"
        )
    return decimals


def is_supported(asset: object) -> bool:
    """True only when *asset* has a registered scale (never raises)."""
    try:
        decimals_for(asset)
    except UnknownAssetScaleError:
        return False
    return True


def is_wallet_credit_asset(asset: object) -> bool:
    """True only for the wallet's ONE credit currency.

    The internal wallet/ledger is single-currency by schema
    (``CHECK (currency = 'USDT')``); crediting any other asset would
    be a currency mix, so the credit boundary gates on this predicate
    instead of an asset literal.
    """
    key = _normalize(asset)
    return key == _WALLET_CREDIT_ASSET


# ── Exact conversion (integer only — no float anywhere) ───────────────


def parse_units(
    value: object, asset: object, *, field: str = "amount"
) -> int:
    """Exact value → integer atomic units at *asset*'s registered scale.

    Accepts ``str`` (canonical decimal text), ``int`` and ``Decimal``;
    rejects ``float``/``bool`` (financial math forbids them), malformed
    or signed/exponent text, more fractional digits than the asset's
    scale (rejected — NEVER rounded), non-positive results and values
    beyond the signed SQLite INTEGER bound.  Arabic-Indic digits are
    normalized first (same convention as the admin settings parser).

    Raises:
        UnknownAssetScaleError: no registered scale for *asset*.
        AssetAmountError: the value is not an exact in-range amount.
    """
    decimals = decimals_for(asset)
    if isinstance(value, bool) or isinstance(value, float):
        raise AssetAmountError(
            f"{field}: float/bool are forbidden in financial math"
        )
    if isinstance(value, Decimal):
        text = str(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise AssetAmountError(
            f"{field}: unsupported type {type(value).__name__}"
        )
    text = normalize_digits(text)
    if not text or not _DECIMAL_TEXT_RE.fullmatch(text):
        raise AssetAmountError(f"{field}: not a valid decimal: {value!r}")
    whole, _, frac = text.partition(".")
    if len(frac) > decimals:
        raise AssetAmountError(
            f"{field}: at most {decimals} decimal place(s) allowed "
            f"for this asset"
        )
    # Exact integer math from the digit strings — never Decimal
    # context rounding: frac is already proven <= scale length, and
    # it is padded RIGHT to the asset's scale so "0.001" USDT is
    # 100000 units (not 1) while "0.99" EGP is 99 (not 9900).
    units = int(whole) * 10 ** decimals + (
        int(frac.ljust(decimals, "0")) if frac else 0
    )
    if units <= 0:
        raise AssetAmountError(f"{field}: must be positive")
    if units > _SQLITE_INT64_MAX:
        raise AssetAmountError(f"{field}: exceeds the supported maximum")
    return units


def units_to_asset_decimal(units: object, asset: object) -> Decimal:
    """Integer atomic units → exact Decimal at *asset*'s scale.

    The exact inverse of :func:`parse_units` for whole inputs: the
    Decimal keeps the asset's scale (``5000`` EGP → ``Decimal('50.00')``,
    ``100000000`` USDT → ``Decimal('1.00000000')``) so display never
    invents or loses precision.

    Raises:
        UnknownAssetScaleError: no registered scale for *asset*.
        AssetAmountError: *units* is not an in-range non-negative int.
    """
    decimals = decimals_for(asset)
    if isinstance(units, bool) or not isinstance(units, int):
        raise AssetAmountError("units: integer atomic units required")
    if units < 0 or units > _SQLITE_INT64_MAX:
        raise AssetAmountError("units: out of range")
    return Decimal((0, tuple(int(ch) for ch in str(units)), -decimals))

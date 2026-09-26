"""
Admin Platform Settings Foundation (MT-ADMIN-15)
================================================

Persistent, admin-configurable platform settings — the FOUNDATION only
(no Mini App UI, no bot command, no user-facing mutation path).

The ``platform_settings`` table created by ``db.init_db`` is the ONLY
place these values live: nothing about them is a Python constant any
more, and no hard-coded wallet address, provider, network or secret is
involved anywhere in this module.

Schema (created by ``db.init_db``)::

    platform_settings (
        key         TEXT PRIMARY KEY,        -- stable string key
        value       INTEGER NOT NULL         -- exact integer value
                    CHECK (typeof(value) = 'integer'),
        updated_by  INTEGER,                 -- acting admin id
        created_at  TIMESTAMP ...,
        updated_at  TIMESTAMP ...
    )

Representation — EXACT integers only, never float, never REAL:

* ``*_units`` keys are **USDT atomic units**: 1 USDT = ``wallet.USDT_SCALE``
  = 100,000,000 units (``wallet.USDT_DECIMALS`` = 8).  One unit is
  0.00000001 USDT, so values far below 0.01 USDT (e.g. 1 unit, 500,000
  units = 0.005 USDT) are representable and exact — no 2-dp rounding
  ever happens on the way in or out.
* ``advertiser_commission`` is an integer **basis-point** value with an
  explicit, documented scale: ``COMMISSION_SCALE`` = 10,000 basis
  points == 100 %, so 1 basis point == 0.01 % and the agreed initial
  30 % commission is stored exactly as ``3000``.

Audited defaults (MT-ADMIN-15 audit of the current contract):

* ``advertiser_commission`` → ``COMMISSION_DEFAULT`` = 3000 bp (30 %),
  the currently agreed initial value.
* ``minimum_withdrawal_units`` / ``minimum_deposit_units`` /
  ``withdrawal_fee_units`` → registered but **unseeded**.  The only
  pre-existing constants are ``withdrawal_rules.MIN_WITHDRAW_EGP``
  (10 EGP) and ``withdrawal_rules.WITHDRAW_FEE_EGP`` (1 EGP): both are
  EGP-denominated while these settings are USDT atomic units, so they
  do not define a USDT default and no exchange rate may be invented
  here; no deposit minimum exists anywhere in the codebase.  Production
  defaults are therefore NOT invented — an admin sets them, and
  :func:`get_required_setting` raises :class:`SettingNotFoundError`
  until then.

Authorization: mutation is admin-only and reuses the ONE existing model
(``config.is_admin``) — this module never re-implements authorization.
Reads are ungated (a minimum withdrawal may be displayed to users
later); there is no public or user-facing *mutation* path at all.

Storage conventions reused from the repository:
- reads open ``db.get_connection()``; writes go through
  ``db.transaction()`` (``BEGIN IMMEDIATE``) unless the caller passes
  its own open connection (``conn=``), which is exactly how a caller
  wraps several writes in ONE transaction;
- exact USDT text conversion reuses ``wallet.decimal_to_units`` (the
  MT-ADMIN-14 exact parser's primitive): no float, no ``round()``,
  over-precision is rejected, never silently rounded.

Run:
    python -m pytest test_platform_settings.py -v
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

import db
import wallet
from config import is_admin
from task_taxonomy import normalize_digits

logger = logging.getLogger(__name__)


# ── Representation scales (documented, exact, integer) ─────────────────

# USDT atomic units come from the wallet authority: 1 USDT = 100,000,000
# units (8 decimal places).  Re-exported here so callers of this module
# never have to guess the scale.
USDT_UNITS_PER_USDT = wallet.USDT_SCALE          # 100_000_000
USDT_DECIMALS = wallet.USDT_DECIMALS             # 8

# Commission scale: INTEGER BASIS POINTS, where 10,000 bp == 100 %.
# 1 bp = 0.01 %.  30 % is stored as 3000 — exact, never a float.
COMMISSION_SCALE = 10_000
COMMISSION_DEFAULT = 3_000                       # 30 % (agreed initial value)


# Mirrors db._SQLITE_INT64_MAX — the signed SQLite INTEGER bound a stored
# value must fit (value stays INTEGER, never REAL).  Copied rather than
# imported so this module depends on db's public surface only.
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807


# ── Stable setting keys ────────────────────────────────────────────────

MINIMUM_WITHDRAWAL_UNITS = "minimum_withdrawal_units"
MINIMUM_DEPOSIT_UNITS = "minimum_deposit_units"
WITHDRAWAL_FEE_UNITS = "withdrawal_fee_units"
ADVERTISER_COMMISSION = "advertiser_commission"

KIND_USDT_UNITS = "usdt_units"
KIND_BASIS_POINTS = "basis_points"


@dataclass(frozen=True)
class SettingSpec:
    """What one registered setting is and how its value is read."""

    key: str
    kind: str                 # KIND_USDT_UNITS | KIND_BASIS_POINTS
    minimum: int
    maximum: int
    default: int | None        # None = no production default is invented
    description: str


_USDT_SPEC = dict(kind=KIND_USDT_UNITS, minimum=0, maximum=_SQLITE_INT64_MAX)

SETTINGS: dict[str, SettingSpec] = {
    MINIMUM_WITHDRAWAL_UNITS: SettingSpec(
        key=MINIMUM_WITHDRAWAL_UNITS,
        **_USDT_SPEC,
        default=None,
        description="حد أدنى للسحب بوحدات USDT الذرية",
    ),
    MINIMUM_DEPOSIT_UNITS: SettingSpec(
        key=MINIMUM_DEPOSIT_UNITS,
        **_USDT_SPEC,
        default=None,
        description="حد أدنى للإيداع بوحدات USDT الذرية",
    ),
    WITHDRAWAL_FEE_UNITS: SettingSpec(
        key=WITHDRAWAL_FEE_UNITS,
        **_USDT_SPEC,
        default=None,
        description="رسوم السحب بوحدات USDT الذرية",
    ),
    ADVERTISER_COMMISSION: SettingSpec(
        key=ADVERTISER_COMMISSION,
        kind=KIND_BASIS_POINTS,
        minimum=0,
        maximum=COMMISSION_SCALE,
        default=COMMISSION_DEFAULT,
        description="عمولة المعلن بالنقطة العشرية (10000 = 100%)",
    ),
}

REGISTERED_SETTINGS: tuple[str, ...] = tuple(SETTINGS)

# Defaults that get seeded by ``db.init_db`` (INSERT OR IGNORE — an
# admin-saved value is never overwritten).  Only settings that the
# existing product contract actually defines appear here.
DEFAULT_SETTINGS: dict[str, int] = {
    spec.key: spec.default
    for spec in SETTINGS.values()
    if spec.default is not None
}


# ── Errors (Arabic — shown directly to the admin) ──────────────────────


class PlatformSettingError(Exception):
    """Base class for every platform-settings failure."""


class UnknownSettingError(PlatformSettingError):
    """The key is not part of the registered settings registry."""


class SettingValidationError(PlatformSettingError):
    """The value is not an exact, in-range representation for the key."""


class SettingNotFoundError(PlatformSettingError):
    """A required setting has never been configured."""


class SettingPermissionError(PlatformSettingError):
    """Mutation attempted by a non-admin (authorization reuse, not logic)."""


# ── Registry access ────────────────────────────────────────────────────


def get_spec(key: object) -> SettingSpec:
    """Return the registered spec for ``key``.

    Raises:
        UnknownSettingError: ``key`` is not a registered setting.  An
            unknown key is never silently treated as "missing" or
            written into the table.
    """
    if isinstance(key, str) and key in SETTINGS:
        return SETTINGS[key]
    raise UnknownSettingError(f"❌ إعداد غير معروف: {key!r}")


def _range_check(spec: SettingSpec, value: int) -> int:
    """Deterministically accept or reject an in-type value."""
    if value < spec.minimum or value > spec.maximum:
        raise SettingValidationError(
            f"❌ {spec.key}: القيمة خارج النطاق المسموح "
            f"({spec.minimum} .. {spec.maximum})."
        )
    return value


# ── Exact input parsing (no float, no rounding) ────────────────────────

# Canonical decimal text guard (same shape as the MT-ADMIN-14 reward
# parser): digits with an optional fractional part.  Scientific
# notation, signs, spaces and anything else are rejected up front.
_DECIMAL_TEXT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def parse_setting_value(key: object, value: object) -> int:
    """Exact input → the canonical integer representation of ``key``.

    Accepts:

        int  — already-canonical value: atomic units for the ``*_units``
               keys, basis points for ``advertiser_commission``
               (``bool`` is rejected).
        str  — exact decimal text in the setting's *display* unit:
               USDT text for monetary keys (``"0.005"`` → 500,000 units,
               ``"0.00000001"`` → 1 unit) and percent text for the
               commission (``"30"`` → 3000 bp, ``"30.5"`` → 3050 bp).
               Arabic-Indic digits are normalized first, matching the
               other admin-input parsers.

    Rejects (deterministically, never rounding): unknown keys, ``bool``,
    ``float``, ``None``/other types, empty or malformed text, negatives,
    scientific notation, more than 8 decimal places for USDT amounts,
    more than 2 decimal places for the percent commission, and any value
    outside the key's registered range.

    Returns:
        int — the exact value that will be persisted.
    """
    spec = get_spec(key)

    if value is None or isinstance(value, bool) or isinstance(value, float):
        raise SettingValidationError(
            f"❌ {spec.key}: القيمة يجب أن تكون رقمًا صحيحًا أو نصًا "
            f"عشريًا دقيقًا (لا تُقبل float/bool)."
        )
    if isinstance(value, int):
        return _range_check(spec, value)
    if not isinstance(value, str):
        raise SettingValidationError(
            f"❌ {spec.key}: نوع قيمة غير مدعوم "
            f"({type(value).__name__})."
        )

    text = normalize_digits(value.strip())
    if not text:
        raise SettingValidationError(f"❌ {spec.key}: القيمة مطلوبة.")
    if text.startswith("-"):
        raise SettingValidationError(f"❌ {spec.key}: القيمة لا يمكن أن تكون سالبة.")
    if not _DECIMAL_TEXT_RE.fullmatch(text):
        raise SettingValidationError(
            f"❌ {spec.key}: قيمة رقمية غير صالحة ({value!r})."
        )

    if spec.kind == KIND_USDT_UNITS:
        try:
            # Existing exact primitive: text → atomic units (rejects
            # >8 dp, NaN, Infinity, float and bool internally).
            units = wallet.decimal_to_units(text, field=spec.key)
        except wallet.InvalidWalletAmountError as exc:
            raise SettingValidationError(f"❌ {exc}") from exc
    else:  # KIND_BASIS_POINTS — integer percent text, max 2 dp
        whole, _, frac = text.partition(".")
        if len(frac) > 2:
            raise SettingValidationError(
                f"❌ {spec.key}: منزلتان عشريتان كحد أقصى "
                f"(0.01% = نقطة عشرية واحدة) — لا تُقرَّب القيم."
            )
        units = int(whole) * (COMMISSION_SCALE // 100) + int(
            frac.ljust(2, "0")[:2]
        )
    return _range_check(spec, units)


# ── Connection handling (caller-owned or repository-owned) ─────────────


@contextmanager
def _connection(
    conn: sqlite3.Connection | None,
    *,
    db_path: str | None,
    write: bool = False,
):
    """Yield the caller's connection, or open one for this operation.

    ``conn`` given → the caller owns the transaction boundary (the
    caller's ``db.transaction()`` commits/rolls back).  Otherwise reads
    use ``db.get_connection()`` and writes use ``db.transaction()``
    (``BEGIN IMMEDIATE``) — the repository's existing conventions.
    """
    if conn is not None:
        yield conn
        return
    if write:
        with db.transaction(db_path) as owned:
            yield owned
    else:
        with db.get_connection(db_path) as owned:
            yield owned


# ── Reads ──────────────────────────────────────────────────────────────


def get_setting(
    key: object,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> int | None:
    """Return the stored integer value, or ``None`` when never set.

    Raises:
        UnknownSettingError: ``key`` is not registered.
    """
    get_spec(key)
    with _connection(conn, db_path=db_path) as active:
        row = active.execute(
            "SELECT value FROM platform_settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    # typeof(value)='integer' already guarantees an int is stored.
    return int(row["value"])


def get_required_setting(
    key: object,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> int:
    """Return the stored value or raise — never a silent fallback.

    Raises:
        UnknownSettingError: ``key`` is not registered.
        SettingNotFoundError: the key is registered but has never been
            configured (e.g. a monetary setting with no production
            default).  Callers must treat this as "configuration is
            incomplete", not as zero.
    """
    spec = get_spec(key)
    value = get_setting(key, conn=conn, db_path=db_path)
    if value is None:
        raise SettingNotFoundError(
            f"❌ الإعداد {spec.key} غير مهيأ بعد — يجب تحديد قيمته أولًا."
        )
    return value


def list_settings(
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, int]:
    """Every configured setting (registered keys only), key → value."""
    with _connection(conn, db_path=db_path) as active:
        rows = active.execute(
            "SELECT key, value FROM platform_settings ORDER BY key"
        ).fetchall()
    return {
        row["key"]: int(row["value"])
        for row in rows
        if row["key"] in SETTINGS
    }


# ── Mutation (admin-only) ──────────────────────────────────────────────


def _require_admin(user_id: object) -> int:
    """Authorization reuse: ``config.is_admin`` is the ONE model.

    The platform-settings layer deliberately implements NO authorization
    logic of its own — only the existing admin check, plus the type
    guard every caller in this repository applies to actor ids.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise SettingPermissionError("⛔ هذا الإجراء للمشرفين فقط.")
    if not is_admin(user_id):
        raise SettingPermissionError("⛔ هذا الإجراء للمشرفين فقط.")
    return user_id


def set_setting(
    key: object,
    value: object,
    *,
    admin_user_id: object,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> int:
    """Validate and persist one setting — ADMIN-ONLY.

    Args:
        key: a registered setting key.
        value: exact input (see :func:`parse_setting_value`).
        admin_user_id: acting admin, authorized through ``config.is_admin``.
        conn: optional open connection — the caller's transaction owns
            commit/rollback (transactional access when needed).
        db_path: database path when no connection is supplied.

    Returns:
        int — the exact value that was stored.

    Raises:
        UnknownSettingError, SettingPermissionError,
        SettingValidationError (nothing is written on any failure).
    """
    spec = get_spec(key)
    _require_admin(admin_user_id)          # authorize BEFORE parsing
    units = parse_setting_value(key, value)

    with _connection(conn, db_path=db_path, write=True) as active:
        cursor = active.execute(
            "UPDATE platform_settings "
            "SET value = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE key = ?",
            (units, admin_user_id, spec.key),
        )
        if cursor.rowcount == 0:
            active.execute(
                "INSERT INTO platform_settings (key, value, updated_by) "
                "VALUES (?, ?, ?)",
                (spec.key, units, admin_user_id),
            )

    # Audit line: key + value + actor only (settings hold no secrets).
    logger.info(
        "Platform setting set: key=%s value=%d admin=%d",
        spec.key, units, admin_user_id,
    )
    return units


# ── Seeding (called by db.init_db) ─────────────────────────────────────


def ensure_default_settings(conn: sqlite3.Connection) -> None:
    """Seed every default that has no row yet — idempotent.

    ``INSERT OR IGNORE`` guarantees a value an admin has already saved
    is NEVER overwritten by a migration or a restart.  Called by
    ``db.init_db`` on the very connection that creates the table, so a
    fresh database and an existing one converge on the same state.
    """
    for key in REGISTERED_SETTINGS:
        default = SETTINGS[key].default
        if default is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO platform_settings (key, value) "
            "VALUES (?, ?)",
            (key, _range_check(SETTINGS[key], default)),
        )

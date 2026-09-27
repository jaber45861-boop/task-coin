"""
Atomic Withdrawal Service (MT-ADMIN-23)
=======================================

The first PRODUCTION withdrawal orchestration layer.  Every flow runs
inside ONE ``db.transaction()`` (``BEGIN IMMEDIATE``) and moves money
only through the already-approved boundaries — this module owns the
business orchestration and the transaction boundary and duplicates no
accounting, no INSERT logic, no row mapping, no rate parsing and no
payment-method selection:

- ``SqliteWithdrawalRepository``   — request persistence + status CAS
- ``SqliteWalletAdapter``         — wallet reserve / release / settle
- ``SqliteLedgerAdapter``         — hold / release / settlement entries
- ``rate_quote.RateQuote``        — immutable rate snapshot + helpers
- ``withdrawal_contract``         — wallet-debit calculators + error
                                    translation boundary (MT-ADMIN-21)
- ``payment_method_store``        — strict active-method resolver
- ``withdrawal_rules``            — approved policy (cooldown; the
                                    Vodafone EGP minimum/fee) — rules
                                    1-11 unchanged
- ``platform_settings``           — configured withdrawal minimum/fee
                                    (MT-ADMIN-24, read on the same
                                    transaction connection)

Deliberately NOT in this task: Mini App endpoints, Telegram handlers,
admin review UI, deposit processing, live rate fetching (the caller
supplies an explicit ``RateQuote``; rates are never fetched or invented
here), provider auto-routing and notifications.

Create flow — ONE atomic transaction (part B)
---------------------------------------------
    validate user + amount -> (inside the transaction) read the
    platform withdrawal settings on the SAME connection -> validate
    method -> resolve rate quote -> resolve active payment method ->
    exact minimum/fee facts (configured settings for USDT, approved
    EGP contract for Vodafone) + ``wallet_debit_units`` -> request_id
    -> cooldown check -> wallet reserve -> ledger hold -> repository
    insert -> COMMIT (the ``db.transaction()`` scope owns it).

If ANY step fails the whole transaction rolls back: no wallet hold, no
ledger hold, no request row survives — normal rollback, never manual
compensation.

Wallet-debit authority (part C)
-------------------------------
``wallet_debit_units`` is the single authoritative amount for every
wallet/ledger movement:

- Vodafone: EGP amount + EGP fee convert to USDT atomic units ONCE,
  ROUND_CEILING (never under-hold), via
  ``withdrawal_contract.egp_minor_to_wallet_debit`` — an explicit
  ``RateQuote`` is required.
- USDT: ``amount_units + withdrawal_fee_units`` as a plain integer
  sum via ``withdrawal_contract.usdt_wallet_debit`` — no rate enters
  the debit, structurally no USDT -> EGP -> USDT round trip.  The
  configured fee (MT-ADMIN-24) is already exact atomic units, so no
  conversion exists anywhere on this path.  A quote is still required
  because the schema pins the rate columns and the EGP display facts
  need it — display only, never the wallet debit.

Rate snapshot (part E, F)
-------------------------
- Vodafone: ``rate_quote.rate_persistence_fields(quote)`` stores the
  exact facts in ``wallet_rate_usdt_egp`` / ``rate_provider`` /
  ``rate_captured_at``; the payout-side ``rate_usdt_egp`` column stays
  NULL (per the model: the pinned rate belongs to the USDT payout).
- USDT: the same quote also backs the EGP-denominated minimum/fee, so
  ALL rate columns carry the caller-supplied quote — nothing is faked or
  manufactured, and the schema's NOT NULL rate columns never force an
  invented value.  (This is the explicit resolution of part F's
  inspect-the-schema condition: no schema adjustment is required, so
  no migration is attempted.)
- The rate is immutable after creation: reject/complete move only
  status + timestamps.

Payment method (part D)
-----------------------
``payment_method_id`` must identify an ACTIVE method — resolved by the
existing strict resolver on the SAME transaction connection; inactive
or missing -> ``PaymentMethodUnavailableError``.  No automatic
selection.  The ``pm_*`` snapshot is copied verbatim;
``payment_methods.destination`` (PLATFORM) and ``user_destination``
(USER) are stored in their own columns and are never substituted; no
destination is ever logged.

Minimum / fee settings (part G)
-------------------------------
Both settings are read INSIDE the create transaction on the same
connection via ``platform_settings.get_required_setting(conn=...)`` —
no fallback and no invented default: a missing key raises the
existing ``SettingNotFoundError`` before any write, exactly per the
platform-settings contract.

- USDT BEP-20: ``minimum_withdrawal_units`` is authoritative and
  compared with EXACT integer atomic units (the requested amount is
  converted once to units; no Decimal/display comparison decides the
  minimum), and ``withdrawal_fee_units`` is the exact fee added to
  the debit (``wallet_debit_units = amount_units +
  withdrawal_fee_units``, persisted as ``fee_native_minor``).
- Vodafone Cash: the approved EGP contract is UNCHANGED (10 EGP
  minimum, 1 EGP fee, one ceiling conversion).  Both settings are
  denominated in USDT atomic units while that contract is
  EGP-denominated, so applying them there would add a second fee in
  the wrong unit — the unit conflict is reported with MT-ADMIN-24
  rather than a silent contract change.

Cooldown / one-pending (part H)
-------------------------------
The 24-hour cooldown (``CooldownError``) is checked inside the
transaction, and the database partial unique index
``ux_withdrawals_one_pending`` stays the stricter invariant: when a
concurrent or stale create reaches it, the SQLite UNIQUE violation is
translated to ``PendingWithdrawalExistsError`` and the transaction
rolls back every write.  The index is never weakened.

Reject / complete (parts I, J)
------------------------------
``reject``: load -> verify pending -> require ``wallet_debit_units``
-> CAS pending -> rejected -> wallet release -> ledger release.
``complete``: load -> verify pending -> require ``wallet_debit_units``
-> CAS pending -> completed -> wallet settle -> ledger settlement.
Both stamp their timestamp ONLY on the successful CAS, move exactly
``wallet_debit_units`` (never recomputed), and roll back as one unit.
A legacy row with NULL ``wallet_debit_units`` raises the domain
``MissingWalletDebitError`` BEFORE any mutation — the amount is never
guessed and the request stays pending.

Errors (part L)
---------------
Every failure flows through ``withdrawal_contract.translated_errors``:
wallet errors become domain ``InsufficientBalanceError`` /
``InsufficientHeldBalanceError`` / ``InvalidAmountError``; payment
method errors become ``PaymentMethodUnavailableError``; duplicate
pending becomes ``PendingWithdrawalExistsError``; missing request /
invalid state / legacy missing debit raise their domain errors;
platform-settings failures (``SettingNotFoundError``) propagate
unchanged — configuration is never silently substituted; unexpected
exceptions propagate unchanged (never swallowed).

Connection ownership (part M)
-----------------------------
The service owns the outer transaction.  Repository, wallet adapter,
ledger adapter, the payment-method resolver and the settings reads
all receive that exact ``connection`` — no nested commit, no hidden
``db.get_connection()``
while a flow is active, rollback restores the complete
pre-operation state.

Run:
    python -m pytest test_withdrawal_service.py -v
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol

import db
import payment_method_store
import platform_settings
import rate_quote
import wallet
import withdrawal_contract
import withdrawal_rules
from rate_quote import RateQuote
from withdrawal_contract import translated_errors
from withdrawal_rules import (
    COOLDOWN,
    METHOD_VODAFONE_CASH,
    CooldownError,
    InvalidAmountError,
    InvalidMethodError,
    InvalidStateError,
    MissingRateError,
    RequestStatus,
    ValidationError,
    WithdrawalRequest,
)
from withdrawal_store import (
    LedgerPort,
    SqliteLedgerAdapter,
    SqliteWalletAdapter,
    SqliteWithdrawalRepository,
    WalletPort,
)

_MINOR_PRECISION = 60
_EGP_MINOR_PER_EGP = withdrawal_contract.EGP_MINOR_PER_EGP  # 100


# ── Domain errors (same hierarchy: subclasses of the MT-ADMIN-21
#    WithdrawalError tree, so the boundary passes them through) ──────


class MissingWalletDebitError(ValidationError):
    """A request row has no authoritative ``wallet_debit_units``.

    Raised by ``reject``/``complete`` on a legacy row whose
    ``wallet_debit_units`` is NULL: the wallet amount is not
    established, so no money may be moved and the request must stay
    untouched.  Never guessed, never treated as zero.
    """


# ── Injected boundary (payment-method read) ─────────────────────────


class PaymentMethodResolver(Protocol):
    """Strict active-method lookup on a caller-owned connection."""

    def __call__(
        self, method_id: object, *, connection: sqlite3.Connection
    ) -> payment_method_store.PaymentMethod: ...


class QuoteLoader(Protocol):
    """Authoritative current-rate lookup on a caller-owned connection
    (MT-ADMIN-25).

    Implementations READ ONLY on the given connection — they never
    commit, roll back or close it, and never open a second
    transaction — so the loaded quote shares the EXACT snapshot of
    the financial mutation that consumes it (no quote/transaction
    race).  ``rate_store.get_current_quote`` satisfies this
    boundary.
    """

    def __call__(
        self, *, connection: sqlite3.Connection
    ) -> RateQuote: ...


# ── Exact input helpers (validation only — no business policy) ──────


def _parse_amount(value: object) -> Decimal:
    """Decimal/int/str -> Decimal; float/bool/non-finite rejected.

    Mirrors ``withdrawal_rules``' amount parsing (same rejection
    classes) so the production service accepts exactly what the
    approved rules accept.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise InvalidAmountError(
            "amount: float/bool are forbidden in financial math; "
            "pass Decimal, int or str"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise InvalidAmountError(
                f"amount: not a valid decimal: {value!r}"
            ) from exc
    else:
        raise InvalidAmountError(
            f"amount: unsupported type {type(value).__name__}; "
            "pass Decimal, int or str"
        )
    if not dec.is_finite():
        raise InvalidAmountError(
            f"amount: must be a finite decimal, got {value!r}"
        )
    return dec


def _require_precision(dec: Decimal, max_decimal_places: int) -> None:
    """Reject more decimal places than the currency allows (no rounding)."""
    exponent = dec.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_decimal_places:
        raise InvalidAmountError(
            f"amount: at most {max_decimal_places} decimal place(s) "
            f"allowed, got {dec}"
        )


def _egp_major_to_minor(value: Decimal, *, field: str) -> int:
    """EGP major amount -> exact INTEGER minor units (never rounded).

    Notation conversion only (1 EGP = 100), feeding the MT-ADMIN-21
    wallet-debit calculator; a value not representable in minor units
    raises instead of being silently rounded.
    """
    with localcontext() as ctx:
        ctx.prec = _MINOR_PRECISION
        scaled = value * _EGP_MINOR_PER_EGP
    if scaled != scaled.to_integral_value():
        raise InvalidAmountError(
            f"{field}: {value!r} is not representable in EGP minor "
            "units — never rounded"
        )
    return int(scaled)


def _normalize_now(value: object) -> datetime:
    """Validate ``now``; aware datetimes render as naive UTC wall-clock
    (the SQLite TIMESTAMP convention) so cooldown arithmetic and stored
    timestamps share one representation."""
    if not isinstance(value, datetime):
        raise ValidationError("now must be a datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_wallet_debit(request: WithdrawalRequest) -> int:
    """The authoritative wallet amount for reject/complete.

    A legacy row (NULL ``wallet_debit_units``) yields the explicit
    domain error — no amount is ever guessed or defaulted to zero.
    """
    debit = request.wallet_debit_units
    if debit is None:
        raise MissingWalletDebitError(
            f"withdrawal {request.request_id!r} has no "
            "wallet_debit_units (legacy row): the authoritative wallet "
            "amount is missing — refusing to guess, the request stays "
            "untouched"
        )
    if isinstance(debit, bool) or not isinstance(debit, int):
        raise ValidationError(
            "wallet_debit_units must be an int of USDT atomic units, "
            f"got {type(debit).__name__}"
        )
    if debit <= 0:
        raise InvalidAmountError(
            f"wallet_debit_units must be positive, got {debit}"
        )
    return debit


# ── The service ─────────────────────────────────────────────────────


class WithdrawalService:
    """Production withdrawal orchestration over injected boundaries.

    Owns the outer transaction; every dependency receives the same
    connection.  No wallet/ledger/repository/rate logic is duplicated
    here — only sequencing, policy checks from the approved rules, and
    exact fact assembly for the existing schema.

    Args:
        db_path: database file for the service-owned transaction
            (defaults to ``db.DB_PATH``).
        repository: request repository (default: the SQLite one).
        wallet_port: wallet boundary (default: ``SqliteWalletAdapter``).
        ledger_port: ledger boundary (default: ``SqliteLedgerAdapter``).
        payment_method_resolver: strict active-method lookup
            (default: ``payment_method_store.get_active_payment_method``).
        quote_loader: optional authoritative current-rate boundary
            (MT-ADMIN-25) — used only when ``create`` receives NO
            explicit quote: the quote is loaded on the transaction's
            OWN connection, inside the one financial transaction.
            ``None`` (default) keeps the original explicit-quote-only
            contract exactly (``MissingRateError`` when none is
            supplied).
    """

    def __init__(
        self,
        *,
        db_path: str | None = None,
        repository: SqliteWithdrawalRepository | None = None,
        wallet_port: WalletPort | None = None,
        ledger_port: LedgerPort | None = None,
        payment_method_resolver: PaymentMethodResolver | None = None,
        quote_loader: QuoteLoader | None = None,
    ) -> None:
        self._db_path = db_path
        self._repository = (
            repository
            if repository is not None
            else SqliteWithdrawalRepository(db_path)
        )
        self._wallet = (
            wallet_port if wallet_port is not None else SqliteWalletAdapter()
        )
        self._ledger = (
            ledger_port if ledger_port is not None else SqliteLedgerAdapter()
        )
        self._resolve_method = (
            payment_method_resolver
            if payment_method_resolver is not None
            else payment_method_store.get_active_payment_method
        )
        # MT-ADMIN-25: no default loader — the authoritative source is
        # an INJECTED boundary (same pattern as wallet/ledger/resolver),
        # so a service built without one behaves exactly as before.
        self._quote_loader = quote_loader

    # ── create (part B) ──────────────────────────────────────────

    def create(
        self,
        user_id: int,
        method: str,
        amount: Decimal | int | str,
        *,
        payment_method_id: int,
        user_destination: str | None = None,
        now: datetime,
        quote: RateQuote | None = None,
        request_id: str | None = None,
    ) -> WithdrawalRequest:
        """Create a PENDING withdrawal atomically (parts B-F).

        One ``db.transaction()``: validate -> read the platform
        withdrawal settings on the transaction connection -> rate ->
        payment method -> exact minimum/fee facts + the authoritative
        ``wallet_debit_units`` -> cooldown -> wallet reserve -> ledger
        hold -> repository insert.  Any failure rolls the WHOLE flow
        back.

        Args:
            user_id: existing Telegram user id (positive int).
            method: ``vodafone_cash`` or ``usdt_bep20``.
            amount: requested payout in the method's own units
                (EGP for Vodafone, USDT for BEP-20).
            payment_method_id: must identify an ACTIVE method.
            user_destination: the USER's payout destination — never
                the platform's ``payment_methods.destination``.
            now: current time (drives ``created_at`` + 24 h cooldown);
                naive or timezone-aware, normalized to UTC wall-clock.
            quote: explicit immutable ``RateQuote`` — required by both
                methods (Vodafone for the wallet-debit conversion,
                USDT for the pinned rate columns and the EGP display
                facts); never fetched or invented here.  When None
                AND the service was built with a ``quote_loader``
                (MT-ADMIN-25), the authoritative current quote is
                loaded instead — on THIS transaction's connection,
                inside the same ``BEGIN IMMEDIATE`` scope, so the
                pinned rate and the financial facts always share one
                snapshot and no second transaction exists.  A quote
                is NEVER taken from the client.  With neither an
                explicit quote nor a loader, the original
                ``MissingRateError`` contract applies unchanged.
            request_id: optional explicit id (defaults to random hex).

        Returns:
            The PENDING request exactly as persisted.

        Raises:
            ValidationError: bad user_id / now / request_id /
                destination / quote type; unknown user.
            InvalidMethodError: unsupported method.
            InvalidAmountError: non-positive, float, wrong precision,
                below minimum, out of range.
            MissingRateError: no explicit ``RateQuote`` supplied and
                no ``quote_loader`` boundary is configured.
            PaymentMethodUnavailableError: inactive/missing method.
            CooldownError: another request inside 24 hours.
            SettingNotFoundError: a required platform withdrawal
                setting has never been configured (no fallback).
            PendingWithdrawalExistsError: the one-pending invariant
                won the race (everything rolled back).
            InsufficientBalanceError: wallet cannot cover the debit.
        """
        # 1. user id + amount (basic)
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
        ):
            raise ValidationError("user_id must be a positive int")
        dec_amount = _parse_amount(amount)
        if dec_amount <= 0:
            raise InvalidAmountError("amount must be positive")
        if user_destination is not None and not isinstance(
            user_destination, str
        ):
            raise ValidationError(
                "user_destination must be a str or None, "
                f"got {type(user_destination).__name__}"
            )
        if user_destination == "":
            raise ValidationError("user_destination must not be empty")
        created_at = _normalize_now(now)

        with translated_errors():
            with db.transaction(self._db_path) as conn:
                # 1b. the user must exist (no invented principals)
                if (
                    conn.execute(
                        "SELECT 1 FROM users WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValidationError(f"no such user {user_id}")

                # 1c. platform withdrawal settings — read on THIS
                #     transaction connection so the create facts and
                #     the configuration share one snapshot; a missing
                #     key raises the platform-settings contract error
                #     before anything is written (MT-ADMIN-24).
                minimum_withdrawal_units = (
                    platform_settings.get_required_setting(
                        platform_settings.MINIMUM_WITHDRAWAL_UNITS,
                        conn=conn,
                    )
                )
                withdrawal_fee_units = (
                    platform_settings.get_required_setting(
                        platform_settings.WITHDRAWAL_FEE_UNITS,
                        conn=conn,
                    )
                )

                # 2. method (closed set, rules 8)
                if method not in withdrawal_rules.SUPPORTED_METHODS:
                    raise InvalidMethodError(
                        f"unsupported method {method!r}; supported: "
                        f"{sorted(withdrawal_rules.SUPPORTED_METHODS)}"
                    )

                # amount precision is method-dependent (rules 1/8)
                _require_precision(
                    dec_amount,
                    (
                        withdrawal_rules._EGP_MAX_DP
                        if method == METHOD_VODAFONE_CASH
                        else withdrawal_rules._USDT_MAX_DP
                    ),
                )

                # 3. rate: BOTH methods cross the currency boundary
                #    exactly once (wallet debit / minimum+fee).
                #    An explicit quote (MT-ADMIN-23) always wins; with
                #    none, the optional quote_loader boundary
                #    (MT-ADMIN-25) reads the authoritative rate on
                #    THIS transaction's connection — same snapshot as
                #    every other read above, so quote freshness and
                #    the mutation cannot race and no second
                #    transaction is opened.  With neither, the
                #    contract is unchanged: rates are never fetched
                #    or invented here.
                if quote is None and self._quote_loader is not None:
                    quote = self._quote_loader(connection=conn)
                if quote is None:
                    raise MissingRateError(
                        f"{method} requires an explicit RateQuote — "
                        "rates are never fetched or invented here"
                    )
                if not isinstance(quote, RateQuote):
                    raise ValidationError(
                        "quote must be a RateQuote, got "
                        f"{type(quote).__name__}"
                    )

                # 4. active payment method on the SAME connection
                pm = self._resolve_method(
                    payment_method_id, connection=conn
                )

                # 5. minimum + fee: the configured USDT-unit settings
                #    are authoritative for USDT (exact integers); the
                #    Vodafone EGP contract is preserved unchanged
                #    (part G — its units are incompatible with the
                #    USDT-denominated settings).
                if method == METHOD_VODAFONE_CASH:
                    min_native = withdrawal_rules.min_native_for(
                        method, quote.rate_usdt_egp
                    )
                    if dec_amount < min_native:
                        raise InvalidAmountError(
                            f"amount {dec_amount} is below the minimum "
                            f"{min_native} for {method}"
                        )
                    fee_native = withdrawal_rules.fee_native_for(
                        method, quote.rate_usdt_egp
                    )
                else:  # METHOD_USDT_BEP20
                    # exact integer atomic units — the authoritative
                    # minimum check, never a Decimal comparison
                    amount_units = wallet.decimal_to_units(
                        dec_amount, field="amount"
                    )
                    if amount_units < minimum_withdrawal_units:
                        raise InvalidAmountError(
                            f"amount {dec_amount} is below the configured "
                            f"minimum {minimum_withdrawal_units} atomic "
                            "USDT units"
                        )
                    fee_native = wallet.units_to_decimal(
                        withdrawal_fee_units
                    )

                # 6. exact facts + the authoritative wallet debit
                rate_fields = rate_quote.rate_persistence_fields(quote)
                if method == METHOD_VODAFONE_CASH:
                    native_unit = "EGP"
                    amount_native = dec_amount
                    amount_egp = dec_amount
                    fee_egp = withdrawal_rules.WITHDRAW_FEE_EGP
                    # payout-side rate column: None for Vodafone
                    # (the pinned rate belongs to the USDT payout)
                    rate_usdt_egp: Decimal | None = None
                    wallet_debit = (
                        withdrawal_contract.egp_minor_to_wallet_debit(
                            amount_egp_minor=_egp_major_to_minor(
                                amount_egp, field="amount_egp"
                            ),
                            fee_egp_minor=_egp_major_to_minor(
                                fee_egp, field="fee_egp"
                            ),
                            quote=quote,
                        )
                    )
                else:  # METHOD_USDT_BEP20
                    native_unit = "USDT"
                    amount_native = dec_amount
                    # ONE-WAY display conversion (never fed back into
                    # the debit — no USDT -> EGP -> USDT round trip)
                    amount_egp = withdrawal_rules.egp_equivalent(
                        dec_amount, quote.rate_usdt_egp
                    )
                    fee_egp = withdrawal_rules.WITHDRAW_FEE_EGP
                    rate_usdt_egp = quote.rate_usdt_egp
                    # plain integer sum: configured fee units, no rate
                    # and no EGP conversion anywhere in the debit
                    wallet_debit = (
                        withdrawal_contract.usdt_wallet_debit(
                            amount_units=amount_units,
                            fee_units=withdrawal_fee_units,
                        )
                    )

                # 7. request id
                if request_id is None:
                    request_id = uuid.uuid4().hex
                elif not isinstance(request_id, str) or request_id == "":
                    raise ValidationError(
                        "request_id must be a non-empty str"
                    )

                # Rule 7: one request per user per 24 hours.  The
                # stricter one-pending DB invariant is enforced by the
                # insert itself and mapped by the boundary.
                latest = self._repository.latest_for(
                    user_id, connection=conn
                )
                if latest is not None and not withdrawal_rules.is_cooldown_over(
                    latest.created_at, created_at
                ):
                    remaining = COOLDOWN - (
                        created_at - latest.created_at
                    )
                    raise CooldownError(
                        "one withdrawal request every 24 hours; last "
                        "request at "
                        f"{latest.created_at.isoformat()}",
                        retry_after_seconds=max(
                            int(remaining.total_seconds()), 0
                        ),
                    )

                request = WithdrawalRequest(
                    request_id=request_id,
                    user_id=user_id,
                    method=method,
                    amount_egp=amount_egp,
                    fee_egp=fee_egp,
                    rate_usdt_egp=rate_usdt_egp,
                    amount_native=amount_native,
                    fee_native=fee_native,
                    status=RequestStatus.PENDING,
                    created_at=created_at,
                    wallet_debit_units=wallet_debit,
                    user_destination=user_destination,
                    native_unit=native_unit,
                    payment_method_id=pm.id,
                    pm_display_name=pm.display_name,
                    pm_category=pm.category,
                    pm_asset=pm.asset,
                    pm_network=pm.network,
                    pm_provider=pm.provider,
                    pm_destination=pm.destination,
                    wallet_rate_usdt_egp=rate_fields[
                        "wallet_rate_usdt_egp"
                    ],
                    rate_captured_at=rate_fields["rate_captured_at"],
                    rate_provider=rate_fields["rate_provider"],
                )

                # 8-10. reserve -> ledger hold -> insert, all on the
                # SAME connection; any later failure rolls them back.
                self._wallet.reserve(
                    user_id, wallet_debit, connection=conn
                )
                self._ledger.hold(
                    user_id,
                    wallet_debit,
                    request_id=request_id,
                    connection=conn,
                    rate_usdt_egp=rate_fields["wallet_rate_usdt_egp"],
                )
                self._repository.insert(request, connection=conn)
                return self._repository.get(
                    request_id, connection=conn
                )

    # ── reject (part I) ──────────────────────────────────────────

    def reject(
        self, request_id: str, *, now: datetime | None = None
    ) -> WithdrawalRequest:
        """Reject a PENDING request and release its hold exactly once.

        One ``db.transaction()``: load -> verify pending -> require
        ``wallet_debit_units`` -> CAS pending -> rejected -> wallet
        release -> ledger release.  Any failure rolls the whole flow
        back (status included), so a release can never happen twice
        and a legacy row without a wallet debit is never guessed.

        Args:
            request_id: target request.
            now: transition timestamp (defaults to the current UTC
                time); stored exactly as given on success.

        Returns:
            The REJECTED request as persisted.

        Raises:
            RequestNotFoundError: no such request.
            InvalidStateError: request is not pending.
            MissingWalletDebitError: legacy row without
                ``wallet_debit_units`` — nothing is mutated.
            InsufficientHeldBalanceError: held units cannot cover the
                release (transaction rolled back).
        """
        at = _utc_now() if now is None else _normalize_now(now)
        with translated_errors():
            with db.transaction(self._db_path) as conn:
                request = self._repository.get(
                    request_id, connection=conn
                )
                if request.status is not RequestStatus.PENDING:
                    raise InvalidStateError(
                        f"cannot reject a request in status "
                        f"{request.status.value!r} for {request_id!r}"
                    )
                debit = _require_wallet_debit(request)
                rejected = self._repository.transition(
                    request_id,
                    to_status="rejected",
                    at=at,
                    connection=conn,
                )
                self._wallet.release_units(
                    request.user_id, debit, connection=conn
                )
                self._ledger.release(
                    request.user_id,
                    debit,
                    request_id=request_id,
                    connection=conn,
                    rate_usdt_egp=request.wallet_rate_usdt_egp,
                )
                return rejected

    # ── complete (part J) ────────────────────────────────────────

    def complete(
        self, request_id: str, *, now: datetime | None = None
    ) -> WithdrawalRequest:
        """Settle a PENDING request — held funds leave exactly once.

        One ``db.transaction()``: load -> verify pending -> require
        ``wallet_debit_units`` -> CAS pending -> completed -> wallet
        settle -> ledger settlement.  Settlement removes held units
        only (available was already reduced at creation), so no second
        deduction is possible; CAS + ledger constraints make repeated
        completes impossible.

        Args:
            request_id: target request.
            now: transition timestamp (defaults to the current UTC
                time); stored exactly as given on success.

        Returns:
            The COMPLETED request as persisted.

        Raises:
            RequestNotFoundError: no such request.
            InvalidStateError: request is not pending.
            MissingWalletDebitError: legacy row without
                ``wallet_debit_units`` — the request stays pending.
            InsufficientHeldBalanceError: held units cannot cover the
                settlement (transaction rolled back).
        """
        at = _utc_now() if now is None else _normalize_now(now)
        with translated_errors():
            with db.transaction(self._db_path) as conn:
                request = self._repository.get(
                    request_id, connection=conn
                )
                if request.status is not RequestStatus.PENDING:
                    raise InvalidStateError(
                        f"cannot complete a request in status "
                        f"{request.status.value!r} for {request_id!r}"
                    )
                debit = _require_wallet_debit(request)
                completed = self._repository.transition(
                    request_id,
                    to_status="completed",
                    at=at,
                    connection=conn,
                )
                self._wallet.settle_units(
                    request.user_id, debit, connection=conn
                )
                self._ledger.settle(
                    request.user_id,
                    debit,
                    request_id=request_id,
                    connection=conn,
                    rate_usdt_egp=request.wallet_rate_usdt_egp,
                )
                return completed

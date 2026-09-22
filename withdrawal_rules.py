"""
Withdrawal Rules Layer (Micro-task)
====================================

Standalone implementation of the approved withdrawal policy — rules only.
Deliberately does NOT touch ``bot.py``, ``task_lifecycle.py``, any Task
Logic, any UI, or any deposit feature.

Approved policy (the legacy 50 EGP / 0 fee rules are NOT used):

 1. Minimum withdrawal = 10 EGP.
 2. Vodafone Cash minimum = 10 EGP.
 3. USDT BEP-20 minimum = 10 EGP worth at the rate pinned into the request.
 4. Withdrawal fee = 1 EGP.
 5. USDT fee = 1 EGP worth at the same pinned rate.
 6. The exchange rate is stored inside the request and never changes after
    creation.
 7. Cooldown = one withdrawal request per user per 24 hours.
 8. Methods: ``vodafone_cash`` and ``usdt_bep20``.
 9. Creating a request atomically holds amount + fee.
10. Rejecting a request refunds the held amount exactly once.
11. Completing a request performs no second deduction.

Money math uses ``decimal.Decimal`` only — floats are forbidden.
``test_withdrawal_rules.py`` asserts this module's source contains no
float literals or ``float`` references, and that every monetary value on a
request is a ``Decimal``.

Storage is injected through the ``Ledger`` and ``WithdrawalRepository``
protocols so a future micro-task can wire these rules to SQLite without
changing them; ``InMemoryLedger`` / ``InMemoryWithdrawalRepository`` are
reference implementations used by the tests.

Conventions:
- EGP amounts are exact to 2 dp; USDT amounts to 8 dp. Inputs with more
  precision are rejected instead of silently rounded.
- EGP -> USDT conversions round UP (``ROUND_CEILING``) so the platform
  never under-requires the minimum or under-collects the fee; the fee
  therefore always covers at least 1 EGP at the pinned rate.
- Production wiring must execute a request's hold/refund together with its
  status change in a single DB transaction (the in-memory stores and the
  service lock already serialize every operation).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
    localcontext,
)
from enum import Enum
from typing import Callable, Optional, Protocol

# ── Policy constants (single source of truth) ───────────────────────────

MIN_WITHDRAW_EGP = Decimal("10")
WITHDRAW_FEE_EGP = Decimal("1")
COOLDOWN = timedelta(hours=24)
COOLDOWN_SECONDS = int(COOLDOWN.total_seconds())  # 86400

METHOD_VODAFONE_CASH = "vodafone_cash"
METHOD_USDT_BEP20 = "usdt_bep20"
SUPPORTED_METHODS = frozenset({METHOD_VODAFONE_CASH, METHOD_USDT_BEP20})

EGP_QUANTUM = Decimal("0.01")
USDT_QUANTUM = Decimal("0.00000001")

_EGP_MAX_DP = 2
_USDT_MAX_DP = 8
_DIVISION_PRECISION = 60


# ── Errors ──────────────────────────────────────────────────────────────


class WithdrawalError(Exception):
    """Base class for every rejection raised by the withdrawal rules."""


class ValidationError(WithdrawalError):
    """Generic input validation failure."""


class InvalidMethodError(ValidationError):
    """Method is not one of the approved withdrawal methods."""


class InvalidAmountError(ValidationError):
    """Amount is not a positive, sufficiently precise, minimum-meeting value."""


class InvalidRateError(ValidationError):
    """Exchange rate is missing precision/type/positivity requirements."""


class MissingRateError(ValidationError):
    """A USDT request was made without any USDT/EGP rate."""


class CooldownError(WithdrawalError):
    """Another withdrawal request exists inside the 24 hour window."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class InsufficientBalanceError(WithdrawalError):
    """Balance cannot cover amount + fee; nothing was held."""


class InvalidStateError(WithdrawalError):
    """Settlement called on a request that is no longer PENDING."""


class RequestNotFoundError(WithdrawalError):
    """Unknown withdrawal request id."""


# ── Request model ───────────────────────────────────────────────────────


class RequestStatus(str, Enum):
    PENDING = "pending"
    REJECTED = "rejected"
    COMPLETED = "completed"


@dataclass(frozen=True)
class WithdrawalRequest:
    """An immutable withdrawal request; the pinned rate never changes."""

    request_id: str
    user_id: int
    method: str
    amount_egp: Decimal          # gross requested value in EGP (2 dp)
    fee_egp: Decimal             # always 1.00 EGP (rule 4)
    rate_usdt_egp: Decimal | None  # pinned USDT/EGP rate; None for Vodafone
    amount_native: Decimal       # payout units (EGP or USDT)
    fee_native: Decimal          # fee in payout units (rule 5)
    status: RequestStatus
    created_at: datetime

    @property
    def total_egp(self) -> Decimal:
        """Amount + fee held from the balance at creation (rule 9)."""
        return self.amount_egp + self.fee_egp

    @property
    def total_native(self) -> Decimal:
        """Amount + fee in payout units."""
        return self.amount_native + self.fee_native


# ── Decimal-only helpers ────────────────────────────────────────────────


def _parse_decimal(
    value: object,
    *,
    field: str,
    error_cls: type[WithdrawalError],
) -> Decimal:
    """Coerce Decimal/int/str to Decimal; floats and bools are forbidden."""
    if isinstance(value, float) or isinstance(value, bool):
        raise error_cls(
            f"{field}: float/bool is forbidden in financial math; "
            f"pass Decimal, int or str"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise error_cls(f"{field}: not a valid decimal: {value!r}") from exc
    else:
        raise error_cls(
            f"{field}: unsupported type {type(value).__name__}; "
            f"pass Decimal, int or str"
        )
    if not dec.is_finite():
        raise error_cls(f"{field}: must be a finite decimal, got {value!r}")
    return dec


def _require_precision(
    dec: Decimal,
    max_decimal_places: int,
    *,
    field: str,
    error_cls: type[WithdrawalError],
) -> None:
    """Reject values with more decimal places than the currency allows."""
    exponent = dec.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_decimal_places:
        raise error_cls(
            f"{field}: at most {max_decimal_places} decimal place(s) "
            f"allowed, got {dec}"
        )


def _resolve_rate(rate: object) -> Decimal:
    """Validate a USDT/EGP rate (must be provided, positive, Decimal-safe)."""
    if rate is None:
        raise MissingRateError(
            "USDT BEP-20 requests require the USDT/EGP rate "
            "(pass rate=... or configure a rate provider)"
        )
    dec = _parse_decimal(rate, field="rate", error_cls=InvalidRateError)
    if dec <= 0:
        raise InvalidRateError(f"rate must be > 0, got {dec}")
    return dec


def egp_to_usdt(amount_egp: Decimal, rate_usdt_egp: Decimal) -> Decimal:
    """EGP -> USDT at ``rate_usdt_egp`` (EGP per 1 USDT), rounded UP to 8 dp."""
    rate = _resolve_rate(rate_usdt_egp)
    with localcontext() as ctx:
        ctx.prec = _DIVISION_PRECISION
        quotient = amount_egp / rate
    return quotient.quantize(USDT_QUANTUM, rounding=ROUND_CEILING)


def egp_equivalent(amount_native: Decimal, rate_usdt_egp: Decimal) -> Decimal:
    """USDT -> EGP at ``rate_usdt_egp``, rounded half-up to 2 dp."""
    rate = _resolve_rate(rate_usdt_egp)
    with localcontext() as ctx:
        ctx.prec = _DIVISION_PRECISION
        product = amount_native * rate
    return product.quantize(EGP_QUANTUM, rounding=ROUND_HALF_UP)


def min_native_for(
    method: str,
    rate_usdt_egp: Decimal | int | str | None = None,
) -> Decimal:
    """Minimum amount in the method's own units (rules 1-3).

    Vodafone Cash: 10 EGP. USDT BEP-20: 10 EGP worth at ``rate_usdt_egp``,
    i.e. ``10 / rate`` rounded up to 8 dp.
    """
    if method == METHOD_VODAFONE_CASH:
        return MIN_WITHDRAW_EGP
    if method == METHOD_USDT_BEP20:
        return egp_to_usdt(MIN_WITHDRAW_EGP, _resolve_rate(rate_usdt_egp))
    raise InvalidMethodError(
        f"unsupported method {method!r}; supported: {sorted(SUPPORTED_METHODS)}"
    )


def fee_native_for(
    method: str,
    rate_usdt_egp: Decimal | int | str | None = None,
) -> Decimal:
    """Withdrawal fee in the method's own units (rules 4-5).

    Vodafone Cash: 1 EGP. USDT BEP-20: 1 EGP worth at ``rate_usdt_egp``
    using the very same rate as the minimum (rules 5-6).
    """
    if method == METHOD_VODAFONE_CASH:
        return WITHDRAW_FEE_EGP
    if method == METHOD_USDT_BEP20:
        return egp_to_usdt(WITHDRAW_FEE_EGP, _resolve_rate(rate_usdt_egp))
    raise InvalidMethodError(
        f"unsupported method {method!r}; supported: {sorted(SUPPORTED_METHODS)}"
    )


def is_cooldown_over(
    last_created_at: datetime | None,
    now: datetime,
) -> bool:
    """True when no request exists in the preceding 24 hours (rule 7).

    Both datetimes must share the same tz-awareness (both aware or both
    naive), otherwise Python raises ``TypeError`` on subtraction.
    """
    if last_created_at is None:
        return True
    return (now - last_created_at) >= COOLDOWN


# ── Injected storage protocols ──────────────────────────────────────────


class Ledger(Protocol):
    """Balance storage. ``hold`` must be atomic: all-or-nothing."""

    def balance_of(self, user_id: int) -> Decimal:
        """Current available balance in EGP."""
        ...

    def hold(self, user_id: int, amount_egp: Decimal) -> None:
        """Atomically deduct ``amount_egp``; raise InsufficientBalanceError
        (with no partial effect) when the balance is too low."""
        ...

    def release(self, user_id: int, amount_egp: Decimal) -> None:
        """Add ``amount_egp`` back to the balance."""
        ...


class WithdrawalRepository(Protocol):
    """Request storage for cooldown lookups and settlement state."""

    def save(self, request: WithdrawalRequest) -> None:
        """Insert or update the request."""
        ...

    def get(self, request_id: str) -> WithdrawalRequest:
        """Fetch by id or raise RequestNotFoundError."""
        ...

    def latest_for(self, user_id: int) -> WithdrawalRequest | None:
        """Most recent request for the user, or None."""
        ...


# ── In-memory reference implementations (tests / future wiring demo) ────


class InMemoryLedger:
    """Reference ``Ledger`` backed by a dict. Thread-safe."""

    def __init__(self, balances: dict[int, Decimal] | None = None) -> None:
        self._balances: dict[int, Decimal] = {}
        self._lock = threading.Lock()
        for user_id, amount in (balances or {}).items():
            self._balances[user_id] = _parse_decimal(
                amount, field="balance", error_cls=ValidationError
            )

    def balance_of(self, user_id: int) -> Decimal:
        with self._lock:
            return self._balances.get(user_id, Decimal("0"))

    def credit(self, user_id: int, amount: Decimal | int | str) -> None:
        """Test/wiring helper: add funds (not part of the withdrawal rules)."""
        dec = _parse_decimal(amount, field="amount", error_cls=ValidationError)
        with self._lock:
            self._balances[user_id] = (
                self._balances.get(user_id, Decimal("0")) + dec
            )

    def hold(self, user_id: int, amount_egp: Decimal) -> None:
        with self._lock:
            balance = self._balances.get(user_id, Decimal("0"))
            if balance < amount_egp:
                raise InsufficientBalanceError(
                    f"balance {balance} EGP cannot hold {amount_egp} EGP"
                )
            self._balances[user_id] = balance - amount_egp

    def release(self, user_id: int, amount_egp: Decimal) -> None:
        with self._lock:
            self._balances[user_id] = (
                self._balances.get(user_id, Decimal("0")) + amount_egp
            )


class InMemoryWithdrawalRepository:
    """Reference ``WithdrawalRepository`` backed by a dict. Thread-safe."""

    def __init__(self) -> None:
        self._items: dict[str, WithdrawalRequest] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def save(self, request: WithdrawalRequest) -> None:
        with self._lock:
            if request.request_id not in self._items:
                self._order.append(request.request_id)
            self._items[request.request_id] = request

    def get(self, request_id: str) -> WithdrawalRequest:
        with self._lock:
            try:
                return self._items[request_id]
            except KeyError:
                raise RequestNotFoundError(
                    f"no withdrawal request {request_id!r}"
                ) from None

    def latest_for(self, user_id: int) -> WithdrawalRequest | None:
        with self._lock:
            candidates = [
                self._items[rid]
                for rid in self._order
                if self._items[rid].user_id == user_id
            ]
        if not candidates:
            return None
        latest = candidates[0]
        for request in candidates[1:]:
            if request.created_at >= latest.created_at:
                latest = request
        return latest

    def all(self) -> list[WithdrawalRequest]:
        """Convenience for tests/inspection."""
        with self._lock:
            return [self._items[rid] for rid in self._order]


# ── Service: the rules engine ───────────────────────────────────────────


class WithdrawalService:
    """Enforces rules 1-11. Holds state only through injected storage.

    ``create`` is serialized under an internal lock so cooldown check,
    atomic hold and persistence happen as one unit (rule 7 + rule 9).
    Settlement never consults the rate provider: the rate pinned into the
    request at creation is the only rate ever used (rule 6).
    """

    def __init__(
        self,
        ledger: Ledger,
        repository: WithdrawalRepository,
        rate_provider: Optional[Callable[[], object]] = None,
    ) -> None:
        self._ledger = ledger
        self._repository = repository
        self._rate_provider = rate_provider
        self._lock = threading.RLock()

    def create(
        self,
        user_id: int,
        method: str,
        amount: Decimal | int | str,
        *,
        now: datetime,
        rate: Decimal | int | str | None = None,
        request_id: str | None = None,
    ) -> WithdrawalRequest:
        """Validate, hold amount + fee atomically, persist a PENDING request.

        Args:
            user_id: Telegram user id (positive int).
            method: ``vodafone_cash`` or ``usdt_bep20`` (rule 8).
            amount: requested amount in the method's own units
                (EGP for Vodafone, USDT for BEP-20).
            now: current time; drives the 24h cooldown (rule 7).
            rate: USDT/EGP rate to pin (rules 3, 5, 6). Falls back to the
                configured rate provider for USDT requests.
            request_id: optional explicit id (defaults to a random hex).

        Returns:
            The stored PENDING request with the rate already pinned.

        Raises:
            InvalidMethodError, InvalidAmountError, InvalidRateError,
            MissingRateError, CooldownError, InsufficientBalanceError.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValidationError("user_id must be a positive int")
        if method not in SUPPORTED_METHODS:
            raise InvalidMethodError(
                f"unsupported method {method!r}; "
                f"supported: {sorted(SUPPORTED_METHODS)}"
            )
        if not isinstance(now, datetime):
            raise ValidationError("now must be a datetime")

        dec_amount = _parse_decimal(
            amount, field="amount", error_cls=InvalidAmountError
        )
        if dec_amount <= 0:
            raise InvalidAmountError("amount must be positive")

        pinned_rate: Decimal | None = None
        if method == METHOD_USDT_BEP20:
            candidate = rate
            if candidate is None:
                candidate = self._fetch_rate()
            pinned_rate = _resolve_rate(candidate)
            _require_precision(
                dec_amount,
                _USDT_MAX_DP,
                field="amount",
                error_cls=InvalidAmountError,
            )
            min_native = egp_to_usdt(MIN_WITHDRAW_EGP, pinned_rate)
            if dec_amount < min_native:
                raise InvalidAmountError(
                    f"amount {dec_amount} USDT is below the minimum "
                    f"{min_native} USDT (= 10 EGP at rate {pinned_rate})"
                )
            amount_native = dec_amount
            amount_egp = egp_equivalent(dec_amount, pinned_rate)
            fee_native = egp_to_usdt(WITHDRAW_FEE_EGP, pinned_rate)
        else:  # METHOD_VODAFONE_CASH (rule 2)
            _require_precision(
                dec_amount,
                _EGP_MAX_DP,
                field="amount",
                error_cls=InvalidAmountError,
            )
            if dec_amount < MIN_WITHDRAW_EGP:
                raise InvalidAmountError(
                    f"amount {dec_amount} EGP is below the minimum "
                    f"{MIN_WITHDRAW_EGP} EGP"
                )
            amount_native = dec_amount
            amount_egp = dec_amount
            fee_native = WITHDRAW_FEE_EGP

        fee_egp = WITHDRAW_FEE_EGP  # rules 4 + 5: 1 EGP equivalent
        hold_egp = amount_egp + fee_egp  # rule 9

        with self._lock:
            # Rule 7: one request per user per 24 hours.
            latest = self._repository.latest_for(user_id)
            if latest is not None and not is_cooldown_over(latest.created_at, now):
                remaining = COOLDOWN - (now - latest.created_at)
                raise CooldownError(
                    "one withdrawal request every 24 hours; last request at "
                    f"{latest.created_at.isoformat()}",
                    retry_after_seconds=max(int(remaining.total_seconds()), 0),
                )

            # Rule 9: atomic hold of amount + fee; nothing persisted on failure.
            self._ledger.hold(user_id, hold_egp)

            request = WithdrawalRequest(
                request_id=request_id or uuid.uuid4().hex,
                user_id=user_id,
                method=method,
                amount_egp=amount_egp,
                fee_egp=fee_egp,
                rate_usdt_egp=pinned_rate,
                amount_native=amount_native,
                fee_native=fee_native,
                status=RequestStatus.PENDING,
                created_at=now,
            )
            try:
                self._repository.save(request)
            except Exception:
                self._ledger.release(user_id, hold_egp)  # roll back the hold
                raise
            return request

    def reject(self, request_id: str) -> WithdrawalRequest:
        """Reject a PENDING request and refund the held amount once (rule 10).

        The status transition is persisted before funds move so a failure
        can never enable a second refund; production wiring must still wrap
        both operations in one DB transaction.
        """
        with self._lock:
            request = self._repository.get(request_id)
            if request.status is not RequestStatus.PENDING:
                raise InvalidStateError(
                    f"cannot reject a request in status "
                    f"{request.status.value!r} (refund happens at most once)"
                )
            rejected = replace(request, status=RequestStatus.REJECTED)
            self._repository.save(rejected)
            self._ledger.release(request.user_id, request.total_egp)
            return rejected

    def complete(self, request_id: str) -> WithdrawalRequest:
        """Mark a PENDING request COMPLETED — no second deduction (rule 11)."""
        with self._lock:
            request = self._repository.get(request_id)
            if request.status is not RequestStatus.PENDING:
                raise InvalidStateError(
                    f"cannot complete a request in status "
                    f"{request.status.value!r}"
                )
            completed = replace(request, status=RequestStatus.COMPLETED)
            self._repository.save(completed)
            # Funds were held at creation; deliberately no ledger movement.
            return completed

    def _fetch_rate(self) -> object:
        if self._rate_provider is None:
            raise MissingRateError(
                "USDT BEP-20 requests require a USDT/EGP rate "
                "(pass rate=... or configure a rate provider)"
            )
        return self._rate_provider()

"""Tests for the Withdrawal Rules Layer (``withdrawal_rules.py``).

Coverage map (mandatory cases):

- 9.99 rejected / 10 accepted                       -> TestMinimumRules
- USDT minimum computed from the pinned rate        -> TestUsdtRules
- EGP fee = 1                                       -> TestFeeRules
- USDT fee computed from the same pinned rate       -> TestFeeRules
- Cooldown rejected before 24h / accepted after 24h -> TestCooldownRules
- Another user's request does not affect cooldown   -> TestCooldownRules
- Rejecting refunds exactly once                    -> TestSettlementRules
- Completing performs no second deduction            -> TestSettlementRules
- Price change after creation leaves request intact -> TestRatePinningRules

Also enforced:
- No float anywhere in the module source (AST scan).
- Every monetary field on a request is a Decimal.
- Legacy rules (50 EGP minimum / 0 fee) are not used.
- Methods are exactly Vodafone Cash and USDT BEP-20.

Run:
    python -m unittest test_withdrawal_rules.py -v
"""

import ast
import inspect
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from withdrawal_rules import (
    COOLDOWN_SECONDS,
    METHOD_USDT_BEP20,
    METHOD_VODAFONE_CASH,
    MIN_WITHDRAW_EGP,
    SUPPORTED_METHODS,
    WITHDRAW_FEE_EGP,
    CooldownError,
    InMemoryLedger,
    InMemoryWithdrawalRepository,
    InsufficientBalanceError,
    InvalidAmountError,
    InvalidMethodError,
    InvalidRateError,
    InvalidStateError,
    MissingRateError,
    RequestNotFoundError,
    RequestStatus,
    ValidationError,
    WithdrawalService,
    WithdrawalRequest,
    egp_equivalent,
    egp_to_usdt,
    fee_native_for,
    is_cooldown_over,
    min_native_for,
)
import withdrawal_rules


# ── Reference values (rate = 48.5 EGP per USDT) ────────────────────────
# 10 / 48.5 = 0.20618556701...  -> ceil 8dp -> 0.20618557
#  1 / 48.5 = 0.02061855670...  -> ceil 8dp -> 0.02061856
RATE = Decimal("48.5")
USDT_MIN_AT_485 = Decimal("0.20618557")
USDT_FEE_AT_485 = Decimal("0.02061856")


class _Base(unittest.TestCase):
    """Shared fixture: in-memory ledger/repository + fixed clock."""

    def setUp(self):
        self.ledger = InMemoryLedger()
        self.repo = InMemoryWithdrawalRepository()
        self.rate = RATE
        self.explode_rate = False
        self.svc = WithdrawalService(
            self.ledger, self.repo, rate_provider=self._provider
        )
        self.now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

    def _provider(self):
        if self.explode_rate:
            raise AssertionError("rate provider must not be consulted here")
        return self.rate

    def fund(self, user_id=1, amount="1000"):
        self.ledger.credit(user_id, amount)

    def _hours(self, hours):
        return self.now + timedelta(hours=hours)


# ── 1. Minimum rules (rules 1-2, mandatory: 9.99 / 10) ────────────────


class TestMinimumRules(_Base):
    def test_9_99_rejected_for_vodafone(self):
        self.fund()
        with self.assertRaises(InvalidAmountError):
            self.svc.create(1, METHOD_VODAFONE_CASH, "9.99", now=self.now)
        self.assertEqual(self.repo.all(), [])
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

    def test_10_accepted_for_vodafone(self):
        self.fund()
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        self.assertEqual(req.amount_egp, Decimal("10"))
        self.assertEqual(req.status, RequestStatus.PENDING)
        # hold = amount + fee = 11 EGP
        self.assertEqual(req.total_egp, Decimal("11.00"))
        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))

    def test_9_99_usdt_equivalent_rejected(self):
        # 0.20618556 USDT at 48.5 is just below 10 EGP.
        self.fund()
        below = USDT_MIN_AT_485 - Decimal("0.00000001")
        with self.assertRaises(InvalidAmountError):
            self.svc.create(1, METHOD_USDT_BEP20, below, now=self.now)
        self.assertEqual(self.repo.all(), [])
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

    def test_zero_and_negative_amounts_rejected(self):
        for bad in ("0", "-5"):
            with self.assertRaises(InvalidAmountError):
                self.svc.create(1, METHOD_VODAFONE_CASH, bad, now=self.now)
        self.assertEqual(self.repo.all(), [])

    def test_unsupported_method_rejected(self):
        with self.assertRaises(InvalidMethodError):
            self.svc.create(1, "paypal", "10", now=self.now)

    def test_invalid_user_id_rejected(self):
        for bad in (0, -1, "1", True):
            with self.assertRaises(ValidationError):
                self.svc.create(bad, METHOD_VODAFONE_CASH, "10", now=self.now)

    def test_excess_precision_rejected(self):
        self.fund()
        # 3 decimal places in EGP
        with self.assertRaises(InvalidAmountError):
            self.svc.create(1, METHOD_VODAFONE_CASH, "10.005", now=self.now)
        # 9 decimal places in USDT, and above the minimum -> precision rule
        with self.assertRaises(InvalidAmountError):
            self.svc.create(1, METHOD_USDT_BEP20, "1.123456789", now=self.now)
        self.assertEqual(self.repo.all(), [])
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

    def test_float_inputs_are_forbidden(self):
        self.fund()
        with self.assertRaises(InvalidAmountError):
            self.svc.create(1, METHOD_VODAFONE_CASH, 10.5, now=self.now)
        with self.assertRaises(InvalidRateError):
            self.svc.create(
                1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now, rate=48.5
            )
        with self.assertRaises(InvalidRateError):
            min_native_for(METHOD_USDT_BEP20, 48.5)
        self.assertEqual(self.repo.all(), [])


# ── 2. USDT minimum from the pinned rate (rules 3 + 6) ────────────────


class TestUsdtRules(_Base):
    def test_minimum_helpers(self):
        self.assertEqual(min_native_for(METHOD_VODAFONE_CASH), Decimal("10"))
        # ceil(10 / 48.5) to 8 dp
        self.assertEqual(
            min_native_for(METHOD_USDT_BEP20, RATE), USDT_MIN_AT_485
        )
        # ceil(10 / 60) to 8 dp
        self.assertEqual(
            min_native_for(METHOD_USDT_BEP20, Decimal("60")),
            Decimal("0.16666667"),
        )
        # ceil(10 / 48) to 8 dp
        self.assertEqual(
            min_native_for(METHOD_USDT_BEP20, Decimal("48")),
            Decimal("0.20833334"),
        )

    def test_minimum_helper_requires_rate(self):
        with self.assertRaises(MissingRateError):
            min_native_for(METHOD_USDT_BEP20)
        with self.assertRaises(InvalidRateError):
            min_native_for(METHOD_USDT_BEP20, "0")

    def test_exact_pinned_minimum_accepted(self):
        self.fund()
        req = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        self.assertEqual(req.rate_usdt_egp, RATE)
        self.assertEqual(req.amount_native, USDT_MIN_AT_485)
        # hold is denominated in EGP: 10.00 + 1.00
        self.assertEqual(req.amount_egp, Decimal("10.00"))
        self.assertEqual(req.total_egp, Decimal("11.00"))
        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))

    def test_usdt_request_without_rate_rejected(self):
        bare = WithdrawalService(self.ledger, self.repo)  # no rate provider
        with self.assertRaises(MissingRateError):
            bare.create(
                1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
            )
        self.assertEqual(self.repo.all(), [])

    def test_non_positive_rate_rejected(self):
        self.fund()
        for bad in ("0", "-1"):
            with self.assertRaises(InvalidRateError):
                self.svc.create(
                    1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now, rate=bad
                )


# ── 3. Fee rules (rules 4-5, mandatory) ───────────────────────────────


class TestFeeRules(_Base):
    def test_egp_fee_is_exactly_one(self):
        self.assertEqual(WITHDRAW_FEE_EGP, Decimal("1"))
        self.fund()
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        self.assertEqual(req.fee_egp, Decimal("1"))
        self.assertEqual(req.fee_native, Decimal("1"))
        self.assertEqual(req.total_egp, Decimal("11.00"))

    def test_usdt_fee_from_same_rate(self):
        self.assertEqual(
            fee_native_for(METHOD_USDT_BEP20, RATE), USDT_FEE_AT_485
        )
        self.assertEqual(
            fee_native_for(METHOD_USDT_BEP20, RATE),
            egp_to_usdt(WITHDRAW_FEE_EGP, RATE),
        )

    def test_usdt_fee_uses_request_pinned_rate_not_current(self):
        self.fund()
        pinned = Decimal("73.7")
        req = self.svc.create(
            1,
            METHOD_USDT_BEP20,
            min_native_for(METHOD_USDT_BEP20, pinned),
            now=self.now,
            rate=pinned,  # provider still says 48.5
        )
        self.assertEqual(req.rate_usdt_egp, pinned)
        # fee comes from the pinned rate...
        self.assertEqual(
            req.fee_native, egp_to_usdt(WITHDRAW_FEE_EGP, pinned)
        )
        # ...and from the same rate as the minimum
        self.assertEqual(
            req.amount_native,
            egp_to_usdt(MIN_WITHDRAW_EGP, pinned),
        )
        self.assertEqual(req.fee_egp, WITHDRAW_FEE_EGP)

    def test_usdt_fee_covers_one_egp(self):
        self.fund()
        req = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        covered = egp_equivalent(req.fee_native, req.rate_usdt_egp)
        self.assertGreaterEqual(covered, WITHDRAW_FEE_EGP)
        self.assertEqual(req.fee_egp, WITHDRAW_FEE_EGP)

    def test_fee_is_added_to_the_hold(self):
        self.fund()
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "25", now=self.now)
        self.assertEqual(req.total_egp, req.amount_egp + WITHDRAW_FEE_EGP)
        self.assertEqual(
            self.ledger.balance_of(1), Decimal("1000") - req.total_egp
        )


# ── 4. Cooldown rules (rule 7, mandatory) ─────────────────────────────


class TestCooldownRules(_Base):
    def test_request_before_24h_rejected(self):
        self.fund()
        self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        for offset in (1, 60, 3600, 86399):
            with self.subTest(seconds=offset):
                with self.assertRaises(CooldownError) as ctx:
                    self.svc.create(
                        1,
                        METHOD_VODAFONE_CASH,
                        "10",
                        now=self.now + timedelta(seconds=offset),
                    )
                self.assertEqual(
                    ctx.exception.retry_after_seconds, COOLDOWN_SECONDS - offset
                )
        # only the first request ever got stored
        self.assertEqual(len(self.repo.all()), 1)

    def test_request_at_exactly_24h_accepted(self):
        self.fund()
        self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        req = self.svc.create(
            1, METHOD_VODAFONE_CASH, "10", now=self._hours(24)
        )
        self.assertEqual(len(self.repo.all()), 2)
        self.assertEqual(req.status, RequestStatus.PENDING)

    def test_request_after_24h_accepted(self):
        self.fund()
        self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        req = self.svc.create(
            1, METHOD_VODAFONE_CASH, "10", now=self._hours(25)
        )
        self.assertIsNotNone(req)

    def test_other_user_does_not_affect_cooldown(self):
        self.fund(1)
        self.fund(2)
        self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        # user 2 is unaffected by user 1's request
        other = self.svc.create(
            2, METHOD_VODAFONE_CASH, "10", now=self.now
        )
        self.assertEqual(other.user_id, 2)
        # and each user is still blocked individually
        with self.assertRaises(CooldownError):
            self.svc.create(
                1, METHOD_VODAFONE_CASH, "10", now=self.now + timedelta(hours=1)
            )
        with self.assertRaises(CooldownError):
            self.svc.create(
                2, METHOD_VODAFONE_CASH, "10", now=self.now + timedelta(hours=1)
            )

    def test_rejected_request_still_counts_for_cooldown(self):
        self.fund()
        first = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        self.svc.reject(first.request_id)
        with self.assertRaises(CooldownError):
            self.svc.create(
                1, METHOD_VODAFONE_CASH, "10", now=self.now + timedelta(hours=1)
            )

    def test_is_cooldown_over_helper(self):
        self.assertTrue(is_cooldown_over(None, self.now))
        self.assertFalse(
            is_cooldown_over(self.now, self.now + timedelta(hours=23))
        )
        self.assertTrue(
            is_cooldown_over(self.now, self.now + timedelta(hours=24))
        )


# ── 5. Settlement: hold / refund-once / no double deduct (9-11) ───────


class TestSettlementRules(_Base):
    def test_create_holds_amount_plus_fee_atomically(self):
        self.fund(1, "1000")
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "40", now=self.now)
        self.assertEqual(
            self.ledger.balance_of(1), Decimal("1000") - req.total_egp
        )
        self.assertEqual(self.ledger.balance_of(1), Decimal("959"))

    def test_insufficient_balance_creates_nothing(self):
        self.ledger.credit(1, "5")  # less than 10 + 1
        with self.assertRaises(InsufficientBalanceError):
            self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        self.assertEqual(self.repo.all(), [])
        self.assertEqual(self.ledger.balance_of(1), Decimal("5"))

    def test_reject_refunds_exactly_once(self):
        self.fund(1, "1000")
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))

        rejected = self.svc.reject(req.request_id)
        self.assertEqual(rejected.status, RequestStatus.REJECTED)
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

        # second reject must fail and must not refund again
        with self.assertRaises(InvalidStateError):
            self.svc.reject(req.request_id)
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

        # a third attempt is still blocked
        with self.assertRaises(InvalidStateError):
            self.svc.reject(req.request_id)
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))

    def test_complete_does_not_deduct_again(self):
        self.fund(1, "1000")
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        balance_after_hold = self.ledger.balance_of(1)
        self.assertEqual(balance_after_hold, Decimal("989"))

        completed = self.svc.complete(req.request_id)
        self.assertEqual(completed.status, RequestStatus.COMPLETED)
        # no second deduction
        self.assertEqual(self.ledger.balance_of(1), balance_after_hold)

        # completion is final: neither reject nor complete may run again
        with self.assertRaises(InvalidStateError):
            self.svc.complete(req.request_id)
        with self.assertRaises(InvalidStateError):
            self.svc.reject(req.request_id)
        self.assertEqual(self.ledger.balance_of(1), balance_after_hold)

    def test_settlement_of_unknown_request(self):
        with self.assertRaises(RequestNotFoundError):
            self.svc.reject("missing")
        with self.assertRaises(RequestNotFoundError):
            self.svc.complete("missing")

    def test_concurrent_creates_yield_exactly_one_request(self):
        self.fund(1, "1000")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def attempt():
            barrier.wait()
            try:
                results.append(
                    self.svc.create(
                        1, METHOD_VODAFONE_CASH, "10", now=self.now
                    )
                )
            except Exception as exc:  # noqa: BLE001 - collected for assertions
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 1, "exactly one create must win")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CooldownError)
        self.assertEqual(len(self.repo.all()), 1)
        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))


# ── 6. Rate pinning: stored once, never changes (rule 6) ──────────────


class TestRatePinningRules(_Base):
    def test_rate_is_stored_inside_request(self):
        self.fund()
        req = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        stored = self.repo.get(req.request_id)
        self.assertEqual(stored.rate_usdt_egp, RATE)
        self.assertEqual(stored, req)

    def test_price_change_does_not_mutate_old_request(self):
        self.fund(1, "1000")
        created = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        snapshot = (
            created.amount_egp,
            created.fee_egp,
            created.rate_usdt_egp,
            created.amount_native,
            created.fee_native,
            created.total_egp,
        )

        # price moves after creation
        self.rate = Decimal("60")

        stored = self.repo.get(created.request_id)
        after = (
            stored.amount_egp,
            stored.fee_egp,
            stored.rate_usdt_egp,
            stored.amount_native,
            stored.fee_native,
            stored.total_egp,
        )
        self.assertEqual(after, snapshot)
        self.assertEqual(stored.rate_usdt_egp, RATE)
        self.assertEqual(stored.fee_native, USDT_FEE_AT_485)
        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))

        # settlement still refunds the originally held amount
        self.svc.reject(created.request_id)
        self.assertEqual(self.ledger.balance_of(1), Decimal("1000"))
        self.assertEqual(self.repo.get(created.request_id).rate_usdt_egp, RATE)

        # a brand-new request uses the new price
        new = self.svc.create(
            1,
            METHOD_USDT_BEP20,
            min_native_for(METHOD_USDT_BEP20, Decimal("60")),
            now=self._hours(25),
        )
        self.assertEqual(new.rate_usdt_egp, Decimal("60"))
        self.assertEqual(
            new.amount_native, Decimal("0.16666667")
        )
        self.assertEqual(
            new.fee_native, egp_to_usdt(WITHDRAW_FEE_EGP, Decimal("60"))
        )

    def test_settlement_never_refetches_rate(self):
        self.fund(1, "1000")
        self.fund(2, "1000")
        first = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        second = self.svc.create(
            2, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )

        self.explode_rate = True  # provider now raises if consulted
        self.svc.complete(first.request_id)
        self.svc.reject(second.request_id)

        self.assertEqual(self.ledger.balance_of(1), Decimal("989"))  # held
        self.assertEqual(self.ledger.balance_of(2), Decimal("1000"))  # refunded


# ── 7. Module discipline: methods, Decimal-only, no legacy rules ──────


class TestModuleDiscipline(_Base):
    def test_supported_methods_are_exactly_the_approved_two(self):
        self.assertEqual(
            SUPPORTED_METHODS,
            frozenset({METHOD_VODAFONE_CASH, METHOD_USDT_BEP20}),
        )
        self.assertEqual(METHOD_VODAFONE_CASH, "vodafone_cash")
        self.assertEqual(METHOD_USDT_BEP20, "usdt_bep20")

    def test_legacy_rules_are_not_used(self):
        # rule 12: no 50 EGP minimum, no 0 fee
        self.assertEqual(MIN_WITHDRAW_EGP, Decimal("10"))
        self.assertNotEqual(MIN_WITHDRAW_EGP, Decimal("50"))
        self.assertEqual(WITHDRAW_FEE_EGP, Decimal("1"))
        self.assertNotEqual(WITHDRAW_FEE_EGP, Decimal("0"))

        # functional proof: 49 EGP passes (old rule would reject < 50)
        # and still pays 1 EGP fee (old rule charged 0).
        self.fund(1, "1000")
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "49", now=self.now)
        self.assertEqual(req.amount_egp, Decimal("49"))
        self.assertEqual(req.fee_egp, Decimal("1"))
        self.assertEqual(self.ledger.balance_of(1), Decimal("950"))

    def test_module_source_contains_no_float(self):
        """No float literals, float() conversions, or .float usage.

        ``isinstance(value, float)`` guards are allowed — they are how the
        module rejects float inputs in the first place.
        """
        tree = ast.parse(inspect.getsource(withdrawal_rules))
        guarded: set[str] = set()
        for node in ast.walk(tree):
            # Collect float type-guards: isinstance(value, float)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                for arg in node.args[1:]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name):
                            guarded.add(sub.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                self.fail(f"float() conversion at line {node.lineno}")
            if (
                isinstance(node, ast.Name)
                and node.id == "float"
                and node.id not in guarded
            ):
                self.fail(f"float reference at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")

    def test_every_monetary_value_is_decimal(self):
        self.fund(1, "1000")
        req = self.svc.create(
            1, METHOD_USDT_BEP20, USDT_MIN_AT_485, now=self.now
        )
        values = [
            req.amount_egp,
            req.fee_egp,
            req.amount_native,
            req.fee_native,
            req.total_egp,
            req.total_native,
            req.rate_usdt_egp,
            self.ledger.balance_of(1),
            min_native_for(METHOD_USDT_BEP20, RATE),
            fee_native_for(METHOD_USDT_BEP20, RATE),
            egp_equivalent(req.amount_native, req.rate_usdt_egp),
            egp_to_usdt(MIN_WITHDRAW_EGP, RATE),
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertIsInstance(value, Decimal)
                self.assertNotIsInstance(value, float)

    def test_request_is_immutable(self):
        self.fund()
        req = self.svc.create(1, METHOD_VODAFONE_CASH, "10", now=self.now)
        with self.assertRaises(Exception):
            req.amount_egp = Decimal("50")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

"""
Focused tests — Withdrawal Currency Contract & Error Boundary (MT-ADMIN-21)
===========================================================================

Contract + pure helpers + error translation only.  Coverage:

  1. USDT amount validation
  2. EGP minor-unit validation
  3. exact USDT atomic values below 0.01 USDT
  4. exact EGP minor values
  5. Vodafone EGP → wallet USDT conversion with RateQuote
  6. CEILING behavior at boundaries
  7. USDT withdrawal direct wallet-unit behavior
  8. explicit proof the USDT path does NOT round-trip through EGP
  9. zero/negative rejection
 10. wrong-unit rejection (EGP↔USDT never interchangeable)
 11. no float acceptance
 12. error translation (wallet/rate/payment-method/sqlite → domain)
 13. unexpected exceptions are NOT swallowed
 14. helpers perform no DB/wallet/ledger mutation
 15. INT64 range boundaries (SQLite INTEGER constraints)

Temp/test data only; no DB writes, no wallet/ledger mutation, no rate
is fetched.

Run:
    python -m pytest test_withdrawal_contract.py -v
"""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import payment_method_store
import rate_quote
import wallet
import withdrawal_contract
import withdrawal_rules
from rate_quote import RateQuote
from withdrawal_contract import (
    EGP_MINOR_PER_EGP,
    USDT_UNITS_PER_USDT,
    EgpMinorUnits,
    UsdtAtomicUnits,
    egp_minor_to_wallet_debit,
    require_non_negative_egp_minor,
    require_non_negative_usdt_units,
    require_positive_egp_minor,
    require_positive_usdt_units,
    translate_to_domain_error,
    translated_errors,
    usdt_wallet_debit,
)

# alias imports — wallet and withdrawal_rules share exception NAMES
WalletInsufficient = wallet.InsufficientBalanceError
WalletHeld = wallet.InsufficientHeldBalanceError
WalletBadAmount = wallet.InvalidWalletAmountError
DomainInsufficient = withdrawal_rules.InsufficientBalanceError
DomainHeld = withdrawal_rules.InsufficientHeldBalanceError

INT64_MAX = 9_223_372_036_854_775_807
AWARE = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)


def quote(rate="48.5") -> RateQuote:
    return RateQuote(Decimal(rate), "manual", AWARE)


# ── 1, 2: unit validation ────────────────────────────────────────────


class TestUnitValidation(unittest.TestCase):

    def test_1_usdt_amount_validation(self):
        """1. USDT atomic-unit amounts validate and return plain int."""
        self.assertEqual(require_positive_usdt_units(100_000_000), 100_000_000)
        self.assertIsInstance(require_positive_usdt_units(7), int)
        # the matching wrapper is accepted
        self.assertEqual(
            require_positive_usdt_units(UsdtAtomicUnits(42)), 42
        )
        # non-negative variant (fees) allows zero
        self.assertEqual(require_non_negative_usdt_units(0), 0)

    def test_2_egp_minor_amount_validation(self):
        """2. EGP minor-unit amounts validate and return plain int."""
        self.assertEqual(require_positive_egp_minor(1000), 1000)
        self.assertIsInstance(require_positive_egp_minor(1), int)
        self.assertEqual(
            require_positive_egp_minor(EgpMinorUnits(1000)), 1000
        )
        self.assertEqual(require_non_negative_egp_minor(0), 0)


# ── 3, 4: exact sub-unit values ──────────────────────────────────────


class TestExactSubValues(unittest.TestCase):

    def test_3_exact_usdt_atomic_values_below_one_cent(self):
        """3. A single atomic unit (0.00000001 USDT) survives exactly —
        far below 0.01 USDT, with no float anywhere."""
        one = require_positive_usdt_units(1)
        self.assertEqual(one, 1)
        self.assertEqual(UsdtAtomicUnits(1).units, 1)
        # one unit + zero fee → one unit debit, exact
        self.assertEqual(usdt_wallet_debit(amount_units=1, fee_units=0), 1)
        # a value no float could represent exactly:
        weird = 12_345_678_901_234_567
        self.assertEqual(require_positive_usdt_units(weird), weird)

    def test_4_exact_egp_minor_values(self):
        """4. EGP minor units stay exact: 1 minor = 0.01 EGP."""
        self.assertEqual(require_positive_egp_minor(1), 1)
        self.assertEqual(EgpMinorUnits(1).minor, 1)      # 0.01 EGP
        self.assertEqual(EgpMinorUnits(1000).minor, 1000)  # 10.00 EGP
        self.assertEqual(EGP_MINOR_PER_EGP, 100)
        self.assertEqual(USDT_UNITS_PER_USDT, 100_000_000)


# ── 5, 6: Vodafone conversion ────────────────────────────────────────


class TestVodafoneConversion(unittest.TestCase):

    def test_5_egp_to_wallet_conversion_with_rate_quote(self):
        """5. Vodafone EGP amount + fee converts to the authoritative
        wallet debit through the explicit RateQuote, and reuses the
        established rate_quote conversion (no re-implementation)."""
        q = quote("48.5")
        # 10 EGP + 1 EGP fee = 11 EGP @48.5 → 22,680,413 units
        debit = egp_minor_to_wallet_debit(
            amount_egp_minor=1000, fee_egp_minor=100, quote=q
        )
        self.assertIsInstance(debit, int)
        self.assertEqual(debit, 22_680_413)
        # identical to the MT-ADMIN-20 helper on the same total:
        self.assertEqual(
            debit,
            rate_quote.egp_to_wallet_units(Decimal("11"), q),
        )

    def test_6_ceiling_at_boundaries(self):
        """6. ROUND_CEILING: exact multiples stay exact (never inflate);
        inexact values never under-hold (never round down)."""
        q = quote("48.5")
        # exact: 48.5 EGP @48.5 → exactly 1 USDT
        self.assertEqual(
            egp_minor_to_wallet_debit(
                amount_egp_minor=4850, fee_egp_minor=0, quote=q
            ),
            100_000_000,
        )
        # 10 EGP → 0.20618556907... → ceiling → 20,618,557 (never ...56)
        self.assertEqual(
            egp_minor_to_wallet_debit(
                amount_egp_minor=1000, fee_egp_minor=0, quote=q
            ),
            20_618_557,
        )
        # sub-cent: 0.01 EGP → 20618.55... → ceiling → 20,619 (>0)
        self.assertEqual(
            egp_minor_to_wallet_debit(
                amount_egp_minor=1, fee_egp_minor=0, quote=q
            ),
            20_619,
        )
        # repeating 1/3: 1 EGP @3 → ...333.33 → 33,333,334 (never 33)
        self.assertEqual(
            egp_minor_to_wallet_debit(
                amount_egp_minor=100, fee_egp_minor=0, quote=quote("3")
            ),
            33_333_334,
        )


# ── 7, 8: USDT path ──────────────────────────────────────────────────


class TestUsdtPath(unittest.TestCase):

    def test_7_usdt_direct_wallet_unit_behavior(self):
        """7. USDT amount + fee = wallet debit, plain integer sum —
        raw ints and wrappers behave identically."""
        self.assertEqual(
            usdt_wallet_debit(amount_units=25_000_000, fee_units=2_000_000),
            27_000_000,
        )
        self.assertEqual(
            usdt_wallet_debit(
                amount_units=UsdtAtomicUnits(25_000_000),
                fee_units=UsdtAtomicUnits(2_000_000),
            ),
            27_000_000,
        )
        self.assertEqual(
            usdt_wallet_debit(amount_units=1, fee_units=0), 1
        )

    def test_8_usdt_path_never_round_trips_through_egp(self):
        """8. Structural + runtime proof: the USDT debit takes no rate
        and calls NO EGP conversion helper (they are patched to
        explode and the debit still succeeds)."""
        sig = inspect.signature(usdt_wallet_debit)
        self.assertEqual(
            set(sig.parameters), {"amount_units", "fee_units"},
            "the USDT path must not accept a rate/quote parameter",
        )
        boom = AssertionError("USDT path invoked an EGP conversion")
        with mock.patch(
            "rate_quote.usdt_units_to_egp_display", side_effect=boom
        ), mock.patch(
            "withdrawal_rules.egp_equivalent", side_effect=boom
        ), mock.patch(
            "withdrawal_rules.egp_to_usdt", side_effect=boom
        ):
            self.assertEqual(
                usdt_wallet_debit(
                    amount_units=25_000_000, fee_units=2_000_000
                ),
                27_000_000,
            )


# ── 9, 10, 11: rejections ────────────────────────────────────────────


class TestRejections(unittest.TestCase):

    def test_9_zero_and_negative_rejected(self):
        """9. Zero/negative amounts (and negative fees) are rejected
        with the domain InvalidAmountError — in validators, value
        objects and both debit calculators."""
        for bad in (0, -1, -100):
            with self.subTest(bad=bad):
                with self.assertRaises(withdrawal_rules.InvalidAmountError):
                    require_positive_usdt_units(bad)
                with self.assertRaises(withdrawal_rules.InvalidAmountError):
                    require_positive_egp_minor(bad)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            require_non_negative_usdt_units(-1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            require_non_negative_egp_minor(-1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            UsdtAtomicUnits(-1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            EgpMinorUnits(-1)
        q = quote()
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            usdt_wallet_debit(amount_units=0, fee_units=0)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            usdt_wallet_debit(amount_units=100, fee_units=-1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=0, fee_egp_minor=0, quote=q
            )
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=100, fee_egp_minor=-1, quote=q
            )

    def test_10_wrong_unit_rejected(self):
        """10. EGP minor and USDT atomic units are NEVER
        interchangeable — either direction fails loudly."""
        with self.assertRaises(withdrawal_rules.ValidationError) as ctx:
            require_positive_usdt_units(EgpMinorUnits(100))
        self.assertIn("EGP minor", str(ctx.exception))
        with self.assertRaises(withdrawal_rules.ValidationError) as ctx:
            require_positive_egp_minor(UsdtAtomicUnits(100))
        self.assertIn("USDT atomic", str(ctx.exception))
        # the wrong wrapper is rejected at construction of the other
        with self.assertRaises(withdrawal_rules.ValidationError):
            UsdtAtomicUnits(EgpMinorUnits(100))
        with self.assertRaises(withdrawal_rules.ValidationError):
            EgpMinorUnits(UsdtAtomicUnits(100))
        # debit calculators inherit the same wall
        with self.assertRaises(withdrawal_rules.ValidationError):
            usdt_wallet_debit(
                amount_units=EgpMinorUnits(100), fee_units=0
            )
        with self.assertRaises(withdrawal_rules.ValidationError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=UsdtAtomicUnits(100),
                fee_egp_minor=0,
                quote=quote(),
            )

    def test_11_no_float_acceptance(self):
        """11. float (and bool) are rejected everywhere money is
        validated — no implicit conversion, ever."""
        for bad in (1.5, 0.1, float("nan"), True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(withdrawal_rules.ValidationError):
                    require_positive_usdt_units(bad)
                with self.assertRaises(withdrawal_rules.ValidationError):
                    require_positive_egp_minor(bad)
                with self.assertRaises(withdrawal_rules.ValidationError):
                    UsdtAtomicUnits(bad)
                with self.assertRaises(withdrawal_rules.ValidationError):
                    EgpMinorUnits(bad)
        with self.assertRaises(withdrawal_rules.ValidationError):
            usdt_wallet_debit(amount_units=1.5, fee_units=0)
        with self.assertRaises(withdrawal_rules.ValidationError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=1.5, fee_egp_minor=0, quote=quote()
            )


# ── 12: error translation ────────────────────────────────────────────


class TestErrorTranslation(unittest.TestCase):

    def test_12_translation_table(self):
        """12. Every documented mapping lands on the correct
        withdrawal-domain error class — never the source class."""
        cases = (
            (WalletInsufficient("avail"), DomainInsufficient),
            (WalletHeld("held"), DomainHeld),
            (WalletBadAmount("bad"), withdrawal_rules.InvalidAmountError),
            (rate_quote.RateValidationError("rate"),
             withdrawal_rules.InvalidRateError),
            (rate_quote.UnknownRateProviderError("api"),
             withdrawal_rules.InvalidRateError),
            (payment_method_store.PaymentMethodNotFoundError("x"),
             withdrawal_rules.PaymentMethodUnavailableError),
            (payment_method_store.PaymentMethodInactiveError("x"),
             withdrawal_rules.PaymentMethodUnavailableError),
            (payment_method_store.PaymentMethodValidationError("x"),
             withdrawal_rules.ValidationError),
            (
                sqlite3.IntegrityError(
                    "UNIQUE constraint failed: withdrawal_requests.user_id"
                ),
                withdrawal_rules.PendingWithdrawalExistsError,
            ),
        )
        for source, target in cases:
            with self.subTest(source=type(source).__name__):
                mapped = translate_to_domain_error(source)
                self.assertIsInstance(mapped, target)
                self.assertNotIsInstance(mapped, type(source))

    def test_12_domain_errors_and_other_integrity_errors_unchanged(self):
        """12b. Invalid STATE is already a domain concept (pass
        through); non-pending UNIQUE/FK violations are NOT
        misclassified as duplicate-pending."""
        state = withdrawal_rules.InvalidStateError("not pending")
        self.assertIs(translate_to_domain_error(state), state)
        cooldown = withdrawal_rules.CooldownError("24h", retry_after_seconds=1)
        self.assertIs(translate_to_domain_error(cooldown), cooldown)
        other_unique = sqlite3.IntegrityError(
            "UNIQUE constraint failed: withdrawal_requests.request_id"
        )
        self.assertIs(translate_to_domain_error(other_unique), other_unique)
        fk = sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        self.assertIs(translate_to_domain_error(fk), fk)

    def test_12_context_manager_chains_cause(self):
        """12c. The context manager raises the domain error with the
        original lower-level error preserved as __cause__."""
        with self.assertRaises(DomainInsufficient) as ctx:
            with translated_errors():
                raise WalletInsufficient("not enough available")
        self.assertIsInstance(
            ctx.exception.__cause__, WalletInsufficient
        )
        with self.assertRaises(withdrawal_rules.PendingWithdrawalExistsError):
            with translated_errors():
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: "
                    "withdrawal_requests.user_id"
                )

    def test_13_unexpected_exceptions_not_swallowed(self):
        """13. Unknown exceptions propagate as the ORIGINAL object —
        never swallowed, never re-labeled."""
        boom = ValueError("boom")
        self.assertIs(translate_to_domain_error(boom), boom)
        with self.assertRaises(ValueError) as ctx:
            with translated_errors():
                raise boom
        self.assertIs(ctx.exception, boom, "must re-raise the same object")
        # domain errors pass through untouched as well:
        state = withdrawal_rules.InvalidStateError("bad")
        with self.assertRaises(withdrawal_rules.InvalidStateError) as ctx:
            with translated_errors():
                raise state
        self.assertIs(ctx.exception, state)


# ── 14: purity ───────────────────────────────────────────────────────


class TestPurity(unittest.TestCase):

    def test_14_no_db_wallet_or_ledger_mutation(self):
        """14. All helpers are side-effect free: any DB access would
        explode because db.get_connection is patched to raise."""
        q = quote()
        with mock.patch(
            "db.get_connection",
            side_effect=AssertionError("helper touched the database"),
        ):
            require_positive_usdt_units(100)
            require_positive_egp_minor(100)
            require_non_negative_usdt_units(0)
            require_non_negative_egp_minor(0)
            UsdtAtomicUnits(100)
            EgpMinorUnits(100)
            egp_minor_to_wallet_debit(
                amount_egp_minor=1000, fee_egp_minor=100, quote=q
            )
            usdt_wallet_debit(amount_units=100, fee_units=0)
            translate_to_domain_error(WalletInsufficient("x"))
            with self.assertRaises(withdrawal_rules.InsufficientBalanceError):
                with translated_errors():
                    raise WalletInsufficient("x")


# ── 15: INT64 range ──────────────────────────────────────────────────


class TestRangeBoundaries(unittest.TestCase):

    def test_15_int64_boundaries(self):
        """15. Values fit SQLite INTEGER exactly: INT64_MAX accepted,
        anything larger rejected — in validators, value objects, sums
        and converted debits."""
        self.assertEqual(require_positive_usdt_units(INT64_MAX), INT64_MAX)
        self.assertEqual(require_positive_egp_minor(INT64_MAX), INT64_MAX)
        self.assertEqual(UsdtAtomicUnits(INT64_MAX).units, INT64_MAX)
        self.assertEqual(EgpMinorUnits(INT64_MAX).minor, INT64_MAX)
        # one past the bound → InvalidAmountError everywhere
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            require_positive_usdt_units(INT64_MAX + 1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            require_positive_egp_minor(INT64_MAX + 1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            UsdtAtomicUnits(INT64_MAX + 1)
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            EgpMinorUnits(INT64_MAX + 1)
        # sum overflow in the USDT debit
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            usdt_wallet_debit(
                amount_units=INT64_MAX, fee_units=1
            )
        # EGP sum overflow
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=INT64_MAX,
                fee_egp_minor=1,
                quote=quote(),
            )
        # converted debit overflow (unit range), even with valid inputs
        with self.assertRaises(withdrawal_rules.InvalidAmountError):
            egp_minor_to_wallet_debit(
                amount_egp_minor=INT64_MAX,
                fee_egp_minor=0,
                quote=quote("48.5"),
            )


if __name__ == "__main__":
    unittest.main()

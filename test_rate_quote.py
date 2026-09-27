"""
Focused tests — Withdrawal Rate Quote Contract (MT-ADMIN-20)
=============================================================

Rate-contract foundation only: exact immutable ``RateQuote``, canonical
parsing, provider boundary, timezone-aware timestamps, pure
serialization and the two MT-ADMIN-17 rounding helpers.

  A. valid manual RateQuote
  B. exact Decimal preservation
  C. canonical string serialization
  D. zero rejection
  E. negative rejection
  F. NaN rejection
  G. Infinity rejection
  H. malformed input rejection (incl. scientific-notation strings —
     documented behavior: PLAIN decimal text only)
  I. float rejection
  J. bool rejection
  K. empty input rejection
  L. provider validation
  M. only ``"manual"`` accepted
  N. timestamp must be timezone-aware
  O. immutable RateQuote
  P. deterministic serialization
  Q. no float/REAL usage in the implementation (AST scan)
  R. EGP → USDT units: ROUND_CEILING (never under-hold)
  S. USDT units → EGP display: ROUND_HALF_UP (2 dp)
  T. conversion boundary values / sub-cent atomic amounts
  U. no mutation of wallet/withdrawal state (no DB access at all)

Temp/test data only; no rate is ever fetched — nothing here talks to
the network or the database.

Run:
    python -m pytest test_rate_quote.py -v
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import rate_quote
import withdrawal_rules
from rate_quote import (
    APPROVED_RATE_PROVIDERS,
    PROVIDER_MANUAL,
    NaiveTimestampError,
    RateQuote,
    RateQuoteError,
    RateValidationError,
    UnknownRateProviderError,
    canonical_rate_text,
    canonical_timestamp_text,
    egp_to_wallet_units,
    parse_rate,
    rate_persistence_fields,
    usdt_units_to_egp_display,
    validate_rate_provider,
)

AWARE = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)


def quote(rate="48.5", provider=PROVIDER_MANUAL, captured_at=AWARE):
    return RateQuote(Decimal(rate) if isinstance(rate, str) else rate,
                     provider, captured_at)


# ── A, B: construction & exactness ───────────────────────────────────


class TestValidQuote(unittest.TestCase):

    def test_A_valid_manual_rate_quote(self):
        """A. A valid manual quote carries the exact three facts."""
        q = RateQuote(Decimal("48.5"), "manual", AWARE)
        self.assertEqual(q.rate_usdt_egp, Decimal("48.5"))
        self.assertEqual(q.provider, "manual")
        self.assertEqual(q.captured_at, AWARE)
        # concept: 1 USDT = rate EGP — pinned, readable
        self.assertEqual(q.rate_text, "48.5")
        # str and int inputs are accepted and exact
        self.assertEqual(quote("48.5").rate_usdt_egp, Decimal("48.5"))
        self.assertEqual(quote(50).rate_usdt_egp, Decimal(50))

    def test_B_exact_decimal_preservation(self):
        """B. No precision is lost, ever — far beyond any float's
        15-17 significant digits."""
        long_rate = "48.5000000000000000000000000001"
        q = quote(long_rate)
        self.assertEqual(q.rate_usdt_egp, Decimal(long_rate))
        self.assertEqual(str(q.rate_usdt_egp), long_rate)
        # a value a float could not even represent exactly:
        tiny = "0.1"
        self.assertEqual(parse_rate(tiny), Decimal("0.1"))
        self.assertEqual(str(parse_rate(tiny)), "0.1")
        # int stays exact, never funneled through float:
        self.assertEqual(parse_rate(50), Decimal(50))


# ── D–K: parser rejections ───────────────────────────────────────────


class TestParserRejections(unittest.TestCase):

    def test_D_zero_rejected(self):
        """D. Zero is not a rate — never silently accepted."""
        for value in (0, "0", "0.000", Decimal(0), Decimal("0.00")):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_E_negative_rejected(self):
        """E. Negative rates are rejected."""
        for value in (-1, "-1", Decimal("-48.5"), Decimal(-5)):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_F_nan_rejected(self):
        """F. NaN (quiet and signaling) is rejected."""
        for value in (Decimal("NaN"), Decimal("sNaN")):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_G_infinity_rejected(self):
        """G. ±Infinity is rejected."""
        for value in (Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_H_malformed_input_rejected(self):
        """H. Malformed text is rejected — including scientific
        notation, which the canonical-text convention (matching
        platform_settings) deliberately does NOT accept for strings."""
        for value in (
            "abc", "48..5", "48,5", " 48.5", "48.5 ", ".5", "5.",
            "+48.5", "0x48", "1_000", "48.5.5", None, [], object(),
            "1e5", "1E5", "5e-1", "1e+2",  # scientific notation: rejected
        ):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_I_float_rejected(self):
        """I. float input is rejected — no binary floating point ever."""
        for value in (48.5, 1e-9, float("inf"), float("nan"), 0.1):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_J_bool_rejected(self):
        """J. bool is rejected even though it subclasses int."""
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)

    def test_K_empty_input_rejected(self):
        """K. Empty and whitespace-only strings are rejected."""
        for value in ("", " ", "\t", "\n"):
            with self.subTest(value=value):
                with self.assertRaises(RateValidationError):
                    parse_rate(value)


# ── C, P: canonical & deterministic serialization ────────────────────


class TestSerialization(unittest.TestCase):

    def test_C_canonical_string_serialization(self):
        """C. Canonical text: plain decimal, same value → same text,
        never scientific notation, exact round-trip."""
        cases = {
            "48.500": "48.5",       # trailing zeros stripped (exact)
            "48.5": "48.5",
            "100": "100",
            "0.1": "0.1",
            "0.000000000001": "0.000000000001",   # stays plain, no 1E-12
            "50": "50",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                canonical = canonical_rate_text(text)
                self.assertEqual(canonical, expected)
                self.assertNotIn("E", canonical)
                self.assertNotIn("e", canonical)
                # exact value round-trip through the canonical text
                self.assertEqual(parse_rate(canonical), parse_rate(text))

    def test_P_serialization_is_deterministic(self):
        """P. Same instant + same value → byte-identical output,
        independent of the datetime's original UTC offset."""
        east = timezone(timedelta(hours=2))
        q1 = RateQuote(Decimal("48.50"), "manual",
                       datetime(2026, 9, 27, 12, 0, 0, tzinfo=east))
        q2 = RateQuote(Decimal("48.5"), "manual",
                       datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc))
        f1, f2 = rate_persistence_fields(q1), rate_persistence_fields(q2)
        self.assertEqual(f1, f2, "same instant must serialize identically")
        self.assertEqual(f1, rate_persistence_fields(q1), "repeat stable")
        # column-compatible keys:
        self.assertEqual(
            set(f1),
            {"wallet_rate_usdt_egp", "rate_provider", "rate_captured_at"},
        )
        self.assertEqual(f1["wallet_rate_usdt_egp"], "48.5")
        self.assertEqual(f1["rate_provider"], "manual")
        self.assertEqual(f1["rate_captured_at"],
                         "2026-09-27T10:00:00+00:00")
        # deterministic parse-back of the timestamp:
        parsed = datetime.fromisoformat(f1["rate_captured_at"])
        self.assertEqual(parsed, q2.captured_at)

    def test_timestamp_text_is_utc_offset_explicit(self):
        """Deterministic timestamp text always carries +00:00."""
        text = canonical_timestamp_text(AWARE)
        self.assertEqual(text, "2026-09-27T10:00:00+00:00")
        offset_text = canonical_timestamp_text(
            datetime(2026, 9, 27, 12, 0, 0,
                     tzinfo=timezone(timedelta(hours=2)))
        )
        self.assertEqual(offset_text, "2026-09-27T10:00:00+00:00")


# ── L, M: provider boundary ──────────────────────────────────────────


class TestProviderBoundary(unittest.TestCase):

    def test_L_provider_validation(self):
        """L. Provider ids are validated strings."""
        self.assertEqual(validate_rate_provider("manual"), "manual")
        for value in (None, 123, "", "   ", b"manual", ["manual"]):
            with self.subTest(value=value):
                with self.assertRaises(UnknownRateProviderError):
                    validate_rate_provider(value)

    def test_M_only_manual_accepted(self):
        """M. Only ``"manual"`` is an approved source — no fake
        providers, case-sensitively."""
        self.assertEqual(APPROVED_RATE_PROVIDERS, frozenset({"manual"}))
        for value in ("api", "binance", "Manual", "MANUAL", "live", "auto"):
            with self.subTest(value=value):
                with self.assertRaises(UnknownRateProviderError):
                    validate_rate_provider(value)
        with self.assertRaises(UnknownRateProviderError):
            RateQuote(Decimal("48.5"), "api", AWARE)


# ── N: timestamps ────────────────────────────────────────────────────


class TestTimestampSemantics(unittest.TestCase):

    def test_N_timestamp_must_be_timezone_aware(self):
        """N. Naive local time is ambiguous and rejected; aware is
        kept verbatim; non-datetime types rejected."""
        with self.assertRaises(NaiveTimestampError):
            RateQuote(Decimal("48.5"), "manual",
                      datetime(2026, 9, 27, 10, 0, 0))
        with self.assertRaises(NaiveTimestampError):
            canonical_timestamp_text(datetime(2026, 9, 27))
        with self.assertRaises(RateValidationError):
            RateQuote(Decimal("48.5"), "manual", "2026-09-27T10:00:00Z")
        # aware timestamps accepted, offset variants both fine:
        aware = datetime(2026, 9, 27, 12, 0, 0,
                         tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(RateQuote(Decimal("48.5"), "manual",
                                   aware).captured_at, aware)


# ── O: immutability ──────────────────────────────────────────────────


class TestImmutability(unittest.TestCase):

    def test_O_rate_quote_is_immutable(self):
        """O. None of the three facts can change after construction."""
        q = quote()
        for field, value in (
            ("rate_usdt_egp", Decimal("99")),
            ("provider", "manual"),
            ("captured_at", datetime(2027, 1, 1, tzinfo=timezone.utc)),
        ):
            with self.subTest(field=field):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(q, field, value)
        # still the original values:
        self.assertEqual(q.rate_usdt_egp, Decimal("48.5"))
        self.assertEqual(q.captured_at, AWARE)
        # frozen dataclass (value semantics preserved):
        self.assertEqual(q, quote())
        self.assertEqual(len(dataclasses.fields(RateQuote)), 3)


# ── Q: source discipline ─────────────────────────────────────────────


class TestNoFloatInSource(unittest.TestCase):

    def test_Q_no_float_or_real_usage_in_implementation(self):
        """Q. AST scan: no float literal, no float() call, no .float
        attribute in rate_quote.py (isinstance(..., float) rejection
        guards are the allowed pattern, exactly like wallet.py)."""
        tree = ast.parse(inspect.getsource(rate_quote))
        guarded: set[str] = set()
        for node in ast.walk(tree):
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
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "float" and node.func.id not in guarded:
                    self.fail(f"float() call at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")
            if isinstance(node, ast.Name) and node.id == "float":
                if node.id not in guarded:
                    self.fail(f"float reference at line {node.lineno}")


# ── R, S, T, U: conversion helpers ───────────────────────────────────


class TestConversions(unittest.TestCase):

    def test_R_egp_to_wallet_units_ceiling(self):
        """R. EGP → units never under-holds (ROUND_CEILING) and matches
        the established withdrawal_rules conversion exactly."""
        from wallet import decimal_to_units

        q = quote("48.5")
        # 10 EGP @48.5 → 0.20618556907... USDT → CEILING → 20,618,557
        self.assertEqual(egp_to_wallet_units(10, q), 20_618_557)
        # repeating 1/3 case: 1 EGP @3 → ...333.33 → 33,333,334 (never 33)
        self.assertEqual(egp_to_wallet_units(1, quote("3")), 33_333_334)
        # exact multiple stays exact — ceiling never inflates it
        self.assertEqual(egp_to_wallet_units("48.5", q), 100_000_000)
        # consistency with the existing rules-module conversion:
        for text in ("10", "1", "0.01", "7.77", "1000"):
            with self.subTest(amount=text):
                expected = decimal_to_units(
                    withdrawal_rules.egp_to_usdt(Decimal(text),
                                                 Decimal("48.5"))
                )
                self.assertEqual(egp_to_wallet_units(text, q), expected)

    def test_S_usdt_units_to_egp_display_half_up(self):
        """S. Units → EGP display is ROUND_HALF_UP at 2 dp, including
        an exact .005 tie (HALF_EVEN would round it down)."""
        # 1 USDT @48.5 → 48.50 EGP
        self.assertEqual(
            usdt_units_to_egp_display(100_000_000, quote("48.5")),
            Decimal("48.50"),
        )
        # tie: 0.001 USDT @5 = 0.005 EGP exactly → HALF_UP → 0.01
        self.assertEqual(
            usdt_units_to_egp_display(100_000, quote("5")),
            Decimal("0.01"),
        )
        # mirror of withdrawal_rules.egp_equivalent for the same value:
        self.assertEqual(
            usdt_units_to_egp_display(1_500_000, quote("48.5")),
            withdrawal_rules.egp_equivalent(
                Decimal("0.015"), Decimal("48.5")
            ),
        )

    def test_T_conversion_boundary_values(self):
        """T. Boundaries: zero, sub-cent EGP, one atomic unit, negative
        and float inputs."""
        q = quote("48.5")
        # zero amount → exactly 0 units (pure math, no wallet touched)
        self.assertEqual(egp_to_wallet_units(0, q), 0)
        self.assertEqual(egp_to_wallet_units("0", q), 0)
        # sub-cent EGP still requires a NON-ZERO hold: 0.01 EGP → 20,619
        self.assertEqual(egp_to_wallet_units("0.01", q), 20_619)
        # one atomic unit is below one EGP cent → displays as 0.00
        self.assertEqual(usdt_units_to_egp_display(1, q), Decimal("0.00"))
        # zero units → 0.00
        self.assertEqual(usdt_units_to_egp_display(0, q), Decimal("0.00"))
        # rejected inputs on both helpers:
        for bad in (-1, "-1", True):
            with self.subTest(bad=bad):
                with self.assertRaises(RateValidationError):
                    egp_to_wallet_units(bad, q)
        with self.assertRaises(RateValidationError):
            usdt_units_to_egp_display(True, q)
        with self.assertRaises(RateValidationError):
            usdt_units_to_egp_display(1.5, q)
        with self.assertRaises(RateValidationError):
            usdt_units_to_egp_display(-1, q)

    def test_U_no_mutation_of_wallet_or_withdrawal_state(self):
        """U. Helpers are pure: no database connection is EVER opened,
        and an explicit quote is mandatory (rates never fetched)."""
        with mock.patch(
            "db.get_connection",
            side_effect=AssertionError("helper touched the database"),
        ):
            q = quote("48.5")
            self.assertEqual(egp_to_wallet_units("10", q), 20_618_557)
            self.assertEqual(
                usdt_units_to_egp_display(100_000_000, q),
                Decimal("48.50"),
            )
            rate_persistence_fields(q)
            parse_rate("48.5")
        # a missing/explicit-less quote is rejected — never invented:
        with self.assertRaises(RateQuoteError):
            egp_to_wallet_units("10", None)
        with self.assertRaises(RateQuoteError):
            usdt_units_to_egp_display(1, "48.5")
        # not wired into the rules engine (MT-ADMIN-17: no wiring yet):
        rules_src = inspect.getsource(withdrawal_rules)
        self.assertNotIn("rate_quote", rules_src)


if __name__ == "__main__":
    unittest.main()

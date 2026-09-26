"""
Focused tests — Dynamic Payment Method / Wallet Management (MT-ADMIN-08)
=========================================================================

Admin-configurable payment-method foundation on the Admin Control
Plane:

    /paymethods → [ ➕ إضافة ] [ 📋 عرض ]
    /addpm <pipe form>        → validated persistent payment method
    /editpm <id> | <form>     → full replace, stable id
    list buttons: ✏️ edit / 🟢 activate / 🔴 deactivate / 🗑️ delete

Coverage (task list 1–16 + isolation + security):

 1. admin can create a crypto payment method
 2. admin can create an Egyptian-cash payment method
 3. arbitrary provider accepted (free-form, no enum)
 4. arbitrary network accepted (free-form, no migration needed)
 5. multiple methods may share the same asset
 6. multiple methods may share the same network
 7. active/inactive state persists (incl. raw re-read)
 8. edit persists (created_at / created_by untouched)
 9. delete works in zero-state (confirm → delete → gone)
10. non-admin cannot mutate or read (commands + callbacks)
11. missing destination rejected
12. required fields validated (structure, bounds, control chars)
13. list is deterministic (oldest-first)
14. ordering works (sort_order honored; paging bounded/clamped)
15. ids remain stable across edits
16. persistence after reopening the database connection
+  group/channel invocations stay silent (MT-ADMIN-02 isolation)
+  callback payloads are validated lookup pointers only
+  NO provider/network/address hard-coding in any shipped source
+  private-key material rejected; destinations never logged
+  bot.py registers the commands and the pm: callback family

Run:
    python3 -m pytest test_payment_methods.py -v
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup

import bot as bot_mod
import config
import db
import payment_method_admin as admin
import payment_method_store as store

# ── Identities ────────────────────────────────────────────────────────
ADMIN_A = 111111
STRANGER = 999999


def _run(coroutine):
    return asyncio.run(coroutine)


# ── Update builders ───────────────────────────────────────────────────


def _update(
    user_id: int,
    text: str | None = None,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
    message_id: int = 1,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_chat.id = chat_id if chat_id is not None else user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _callback(
    user_id: int,
    data: str | None,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_chat.id = chat_id if chat_id is not None else user_id
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _reply(update) -> str:
    return update.message.reply_text.call_args[0][0]


def _answered(query) -> str | None:
    args, kwargs = query.answer.call_args
    if kwargs.get("text") is not None:
        return kwargs["text"]
    if args:
        return args[0]
    return None


def _edited(query) -> str:
    return query.edit_message_text.call_args[0][0]


def _markup_of_edit(query) -> InlineKeyboardMarkup | None:
    return query.edit_message_text.call_args[1].get("reply_markup")


# ── Shared fixture ────────────────────────────────────────────────────


class PaymentMethodTestBase(unittest.TestCase):
    """Temp DB + patched ADMINS (the established MT-ADMIN fixture)."""

    CRYPTO_FORM = (
        "crypto | محفظة الاختبار | USDT | TESTNET | مزود الاختبار | "
        "TXtest1234567890 | -"
    )
    CASH_FORM = (
        "cash | كاش الاختبار | EGP | - | مزود الكاش | 01012345678 | -"
    )

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_A]
        self.addCleanup(self._restore_admins)

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db_path

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    # ── raw (fresh-connection) readers: restart simulation ────────────

    def _raw(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _rows(self) -> list:
        return self._raw(
            "SELECT id, category, display_name, asset, network, provider, "
            "destination, instructions, is_active, sort_order, "
            "created_by, updated_by, created_at, updated_at "
            "FROM payment_methods ORDER BY id"
        )

    # ── handler drivers ───────────────────────────────────────────────

    def _add(
        self,
        body: str,
        *,
        admin_id: int = ADMIN_A,
        chat_type: str = "private",
        chat_id: int | None = None,
    ) -> MagicMock:
        update = _update(
            admin_id,
            f"/addpm {body}",
            chat_type=chat_type,
            chat_id=chat_id,
        )
        _run(admin.add_pm_command(update, MagicMock()))
        return update

    def _edit(
        self, body: str, *, admin_id: int = ADMIN_A
    ) -> MagicMock:
        update = _update(admin_id, f"/editpm {body}")
        _run(admin.edit_pm_command(update, MagicMock()))
        return update

    def _press(
        self,
        data: str,
        *,
        admin_id: int = ADMIN_A,
        chat_type: str = "private",
        chat_id: int | None = None,
    ) -> MagicMock:
        update = _callback(
            admin_id, data, chat_type=chat_type, chat_id=chat_id
        )
        _run(admin.payment_method_callback(update, MagicMock()))
        return update

    def _create(self, form: str | None = None) -> int:
        """Store-level create helper; returns the new method id."""
        parts = (form or self.CRYPTO_FORM).split(" | ")
        created = store.create_payment_method(
            category=parts[0],
            display_name=parts[1],
            asset=parts[2],
            network=parts[3],
            provider=parts[4],
            destination=parts[5],
            instructions=parts[6] if len(parts) > 6 else None,
            created_by=ADMIN_A,
        )
        return created.id


# ══════════════════════════════════════════════════════════════════════
# 1–2, 5–6. CREATION (crypto / cash / shared asset / shared network)
# ══════════════════════════════════════════════════════════════════════


class TestCreation(PaymentMethodTestBase):
    def test_admin_creates_crypto_method(self) -> None:
        """1. Admin creates a crypto payment method (handler path)."""
        update = self._add(self.CRYPTO_FORM)
        self.assertIn("تمت إضافة الوسيلة #1", _reply(update))

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["category"], "crypto")
        self.assertEqual(row["display_name"], "محفظة الاختبار")
        self.assertEqual(row["asset"], "USDT")
        self.assertEqual(row["network"], "TESTNET")
        self.assertEqual(row["provider"], "مزود الاختبار")
        self.assertEqual(row["destination"], "TXtest1234567890")
        self.assertIsNone(row["instructions"])  # "-" never stored
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(row["created_by"], ADMIN_A)
        self.assertEqual(row["updated_by"], ADMIN_A)

    def test_admin_creates_cash_method(self) -> None:
        """2. Admin creates an Egyptian-cash payment method."""
        update = self._add(self.CASH_FORM)
        self.assertIn("تمت إضافة الوسيلة #1", _reply(update))
        row = self._rows()[0]
        self.assertEqual(row["category"], "cash")
        self.assertIsNone(row["network"])  # "-" → NULL
        self.assertEqual(row["asset"], "EGP")
        self.assertEqual(row["destination"], "01012345678")

    def test_arbitrary_provider_accepted(self) -> None:
        """3. Providers are free-form — never an enum."""
        for provider in ("Exchange-Ω-9", "مزود غير موجود من قبل"):
            with self.subTest(provider=provider):
                form = (
                    f"crypto | الاسم | USDT | NET | {provider} | "
                    "DEST123 | -"
                )
                update = self._add(form)
                self.assertIn("تمت إضافة", _reply(update))
        providers = [r["provider"] for r in self._rows()]
        self.assertIn("Exchange-Ω-9", providers)
        self.assertIn("مزود غير موجود من قبل", providers)
        # A brand-new provider needed ZERO code changes.

    def test_arbitrary_network_accepted(self) -> None:
        """4. Networks are free-form — no migration, no enum."""
        for network in ("QUANTUM-CHAIN-42", "شبكة_جديدة", "Z" * 64):
            with self.subTest(network=network[:16]):
                form = (
                    f"crypto | الاسم | ASSET | {network} | مزود | "
                    "DEST123 | -"
                )
                update = self._add(form)
                self.assertIn("تمت إضافة", _reply(update))
        networks = [r["network"] for r in self._rows()]
        self.assertIn("QUANTUM-CHAIN-42", networks)
        self.assertIn("شبكة_جديدة", networks)
        # The schema needed no migration for any novel network.

    def test_multiple_methods_same_asset(self) -> None:
        """5. Several methods may share one asset."""
        self._create("crypto | أول | USDT | NET-A | مزودأ | DESTA | -")
        self._create("crypto | ثاني | USDT | NET-B | مزودب | DESTB | -")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["asset"] for r in rows}, {"USDT"})

    def test_multiple_methods_same_network(self) -> None:
        """6. Several methods may share one network."""
        self._create("crypto | أول | USDT | SAME-NET | مزودأ | DESTA | -")
        self._create("crypto | ثاني | BTC | SAME-NET | مزودب | DESTB | -")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["network"] for r in rows}, {"SAME-NET"})

    def test_instructions_preserved_when_provided(self) -> None:
        self._create(
            "crypto | اسم | USDT | NET | مزود | DEST | ملاحظة متعددة\nسطرين"
        )
        row = self._rows()[0]
        self.assertEqual(row["instructions"], "ملاحظة متعددة\nسطرين")


# ══════════════════════════════════════════════════════════════════════
# 7–8, 15. STATE, EDITS, STABLE IDS
# ══════════════════════════════════════════════════════════════════════


class TestStateAndEdit(PaymentMethodTestBase):
    def test_active_inactive_state_persists(self) -> None:
        """7. The active toggle persists, including across re-reads."""
        mid = self._create()

        update = self._press(f"pm:off:{mid}")
        self.assertIn(f"#{mid}", _edited(update.callback_query))
        self.assertEqual(self._rows()[0]["is_active"], 0)

        # Raw fresh-connection read (beyond the process's own caches).
        rows = self._raw(
            "SELECT is_active FROM payment_methods WHERE id = ?", (mid,)
        )
        self.assertEqual(rows[0]["is_active"], 0)

        update = self._press(f"pm:on:{mid}")
        self.assertIn("تم تفعيل", _edited(update.callback_query))
        rows = self._raw(
            "SELECT is_active FROM payment_methods WHERE id = ?", (mid,)
        )
        self.assertEqual(rows[0]["is_active"], 1)

        # Idempotent repeat — same end state, no error, no new rows.
        update = self._press(f"pm:on:{mid}")
        self.assertIn("تم تفعيل", _edited(update.callback_query))
        self.assertEqual(len(self._rows()), 1)

    def test_edit_persists_all_fields(self) -> None:
        """8. A full-replace edit persists every field."""
        mid = self._create()
        update = self._edit(
            f"{mid} | cash | الاسم الجديد | EGP | - | مزود جديد | "
            "09999999999 | ملاحظة جديدة"
        )
        self.assertIn(f"تم تعديل الوسيلة #{mid}", _reply(update))

        row = self._rows()[0]
        self.assertEqual(row["category"], "cash")
        self.assertEqual(row["display_name"], "الاسم الجديد")
        self.assertEqual(row["asset"], "EGP")
        self.assertIsNone(row["network"])
        self.assertEqual(row["provider"], "مزود جديد")
        self.assertEqual(row["destination"], "09999999999")
        self.assertEqual(row["instructions"], "ملاحظة جديدة")
        self.assertEqual(row["updated_by"], ADMIN_A)

    def test_ids_and_creation_fields_stable_across_edits(self) -> None:
        """15. The integer id never changes across repeated edits."""
        mid = self._create()
        before = self._rows()[0]

        for i in range(3):
            self._edit(
                f"{mid} | crypto | اسم{i} | USDT | NET | مزود | "
                f"DEST{i} | -"
            )
        after = self._rows()[0]
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(before["created_at"], after["created_at"])
        self.assertEqual(before["created_by"], after["created_by"])
        self.assertEqual(after["display_name"], "اسم2")
        self.assertEqual(len(self._rows()), 1)

    def test_edit_missing_id_reports_not_found(self) -> None:
        update = self._edit(
            "4242 | cash | ن | EGP | - | م | 01000000000 | -"
        )
        self.assertIn(admin.MSG_NOT_FOUND, _reply(update))
        self.assertEqual(self._rows(), [])


# ══════════════════════════════════════════════════════════════════════
# 9. DELETE (zero-state)
# ══════════════════════════════════════════════════════════════════════


class TestDelete(PaymentMethodTestBase):
    def test_delete_works_in_zero_state(self) -> None:
        """9. Delete removes the row after an explicit confirmation."""
        mid = self._create()

        confirm = self._press(f"pm:del:{mid}")
        text = _edited(confirm.callback_query)
        self.assertIn(f"#{mid}", text)
        # The first press only asks for confirmation — nothing is gone.
        self.assertEqual(len(self._rows()), 1)
        markup = _markup_of_edit(confirm.callback_query)
        callbacks = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"pm:delyes:{mid}", callbacks)

        done = self._press(f"pm:delyes:{mid}")
        self.assertIn(f"تم حذف الوسيلة #{mid}", _edited(done.callback_query))
        self.assertEqual(self._rows(), [])
        self.assertEqual(store.list_payment_methods(), [])

    def test_stale_delete_confirmation_is_inert(self) -> None:
        mid = self._create()
        self._press(f"pm:delyes:{mid}")
        # A replayed confirm for the deleted id mutates nothing.
        again = self._press(f"pm:delyes:{mid}")
        self.assertEqual(
            _answered(again.callback_query), admin.MSG_NOT_FOUND
        )
        self.assertEqual(self._rows(), [])

    def test_delete_missing_id_reports_not_found(self) -> None:
        update = self._press("pm:delyes:777")
        self.assertEqual(
            _answered(update.callback_query), admin.MSG_NOT_FOUND
        )


# ══════════════════════════════════════════════════════════════════════
# 10. AUTHORIZATION
# ══════════════════════════════════════════════════════════════════════


class TestAuthorization(PaymentMethodTestBase):
    def test_non_admin_commands_denied(self) -> None:
        """10. Non-admin: no panel, no add, no edit — zero rows."""
        cases = [
            ("/paymethods", admin.paymethods_command),
            (f"/addpm {self.CRYPTO_FORM}", admin.add_pm_command),
            (
                "/editpm 1 | cash | n | EGP | - | p | d | -",
                admin.edit_pm_command,
            ),
        ]
        for text, handler in cases:
            with self.subTest(text=text[:24]):
                update = _update(STRANGER, text)
                _run(handler(update, MagicMock()))
                self.assertEqual(_reply(update), admin.MSG_ADMIN_ONLY)
        self.assertEqual(self._rows(), [])

    def test_non_admin_callbacks_denied_and_inert(self) -> None:
        self._create()
        for data in (
            "pm:list",
            "pm:help",
            "pm:edit:1",
            "pm:on:1",
            "pm:off:1",
            "pm:del:1",
            "pm:delyes:1",
            "pm:page:1",
        ):
            with self.subTest(data=data):
                update = self._press(data, admin_id=STRANGER)
                self.assertEqual(
                    _answered(update.callback_query), admin.MSG_ADMIN_ONLY
                )
                update.callback_query.edit_message_text.assert_not_called()
        row = self._rows()[0]
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(len(self._rows()), 1)

    def test_non_admin_never_sees_destinations(self) -> None:
        self._create()
        update = _update(STRANGER, "/paymethods")
        _run(admin.paymethods_command(update, MagicMock()))
        self.assertNotIn("TXtest1234567890", _reply(update))


# ══════════════════════════════════════════════════════════════════════
# 11–12. VALIDATION
# ══════════════════════════════════════════════════════════════════════


class TestValidation(PaymentMethodTestBase):
    def test_missing_destination_rejected(self) -> None:
        """11. Empty / whitespace-only destinations never persist."""
        for destination in ("", "   ", "\t"):
            with self.subTest(destination=repr(destination)):
                form = (
                    f"crypto | الاسم | USDT | NET | مزود | {destination} | -"
                )
                update = self._add(form)
                reply = _reply(update)
                self.assertIn("❌", reply)
                self.assertIn("العنوان", reply)
        self.assertEqual(self._rows(), [])

    def test_required_fields_validated(self) -> None:
        """12a. Structure and required fields are enforced."""
        # Too few fields → usage message, nothing stored.
        update = self._add("crypto | only | three")
        self.assertEqual(_reply(update), admin.MSG_USAGE_ADD)
        self.assertEqual(self._rows(), [])

        # Unknown category → store validation error.
        update = self._add(
            "blockchain | الاسم | USDT | NET | مزود | DEST | -"
        )
        self.assertIn("❌", _reply(update))
        self.assertIn("crypto", _reply(update))
        self.assertEqual(self._rows(), [])

        # Empty display name / provider / asset rejected.
        for body in (
            "crypto |  | USDT | NET | مزود | DEST | -",
            "crypto | الاسم | USDT | NET |  | DEST | -",
            "crypto | الاسم |  | NET | مزود | DEST | -",
        ):
            with self.subTest(body=body):
                update = self._add(body)
                self.assertIn("❌", _reply(update))
        self.assertEqual(self._rows(), [])

        # Bare /addpm shows the help text instead of failing.
        update = _update(ADMIN_A, "/addpm")
        _run(admin.add_pm_command(update, MagicMock()))
        self.assertEqual(_reply(update), admin.HELP_TEXT)

    def test_bounds_and_control_chars_rejected(self) -> None:
        """12b. Over-long and control-character input is rejected."""
        long_destination = "D" * (store.MAX_DESTINATION + 1)
        update = self._add(
            f"crypto | الاسم | USDT | NET | مزود | {long_destination} | -"
        )
        self.assertIn("يتجاوز الحد", _reply(update))

        long_name = "ن" * (store.MAX_DISPLAY_NAME + 1)
        update = self._add(
            f"crypto | {long_name} | USDT | NET | مزود | DEST | -"
        )
        self.assertIn("يتجاوز الحد", _reply(update))

        # Newlines are never valid inside a single-line destination.
        update = self._add(
            "crypto | الاسم | USDT | NET | مزود | 0xabc\ndef | -"
        )
        self.assertIn("رموز غير مسموحة", _reply(update))

        # Unsafe control characters rejected wherever they appear.
        update = self._add(
            "crypto | ال\x00اسم | USDT | NET | مزود | DEST | -"
        )
        self.assertIn("رموز غير مسموحة", _reply(update))
        self.assertEqual(self._rows(), [])

    def test_private_key_material_rejected(self) -> None:
        """Private keys are never accepted (generic marker only)."""
        for bad in (
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END-----",
            "my private key material",
        ):
            with self.subTest(bad=bad[:24]):
                update = self._add(
                    f"crypto | الاسم | USDT | NET | مزود | {bad} | -"
                )
                reply = _reply(update)
                self.assertIn("❌", reply)
                self.assertIn("مفتاح خاص", reply)
        self.assertEqual(self._rows(), [])

        # Same guard applies to free-text instructions.
        with self.assertRaises(store.PaymentMethodValidationError):
            store.validate_instructions("-----BEGIN PRIVATE KEY----- x")

    def test_edit_form_structure_validated(self) -> None:
        update = self._edit("notanid | cash | n | EGP | - | p | d | -")
        self.assertEqual(_reply(update), admin.MSG_USAGE_EDIT)
        update = self._edit("1")
        self.assertEqual(_reply(update), admin.MSG_USAGE_EDIT)


# ══════════════════════════════════════════════════════════════════════
# 13–14. LISTING, ORDERING, PAGING
# ══════════════════════════════════════════════════════════════════════


class TestListOrdering(PaymentMethodTestBase):
    def test_list_deterministic_oldest_first(self) -> None:
        """13. Same data always renders the same (oldest-first) order."""
        self._create("crypto | Alpha | USDT | N1 | م | D1 | -")
        self._create("cash | Beta | EGP | - | م | D2 | -")
        self._create("crypto | Gamma | BTC | N2 | م | D3 | -")

        ids = [m.id for m in store.list_payment_methods()]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            ids, [m.id for m in store.list_payment_methods()]
        )

        update = self._press("pm:list")
        text = _edited(update.callback_query)
        self.assertLess(text.index("Alpha"), text.index("Beta"))
        self.assertLess(text.index("Beta"), text.index("Gamma"))

        # Empty state is a concise Arabic message with no keyboard.
        store.delete_payment_method(ids[0])
        store.delete_payment_method(ids[1])
        store.delete_payment_method(ids[2])
        update = self._press("pm:list")
        self.assertEqual(_edited(update.callback_query), admin.MSG_LIST_EMPTY)
        self.assertIsNone(_markup_of_edit(update.callback_query))

    def test_explicit_sort_order_is_honored(self) -> None:
        """14a. sort_order drives display order (id breaks ties)."""
        store.create_payment_method(
            category="crypto", display_name="آخر", asset="USDT",
            network="N", provider="م", destination="D10",
            sort_order=10, created_by=ADMIN_A,
        )
        store.create_payment_method(
            category="crypto", display_name="أول", asset="USDT",
            network="N", provider="م", destination="D1",
            sort_order=1, created_by=ADMIN_A,
        )
        names = [m.display_name for m in store.list_payment_methods()]
        self.assertEqual(names, ["أول", "آخر"])

        # Defaults advance monotonically (insertion order preserved).
        third = store.create_payment_method(
            category="cash", display_name="وسط", asset="EGP",
            provider="م", destination="D5", created_by=ADMIN_A,
        )
        names = [m.display_name for m in store.list_payment_methods()]
        self.assertEqual(names, ["أول", "آخر", "وسط"])
        self.assertEqual(third.sort_order, 11)

    def test_paging_bounded_and_clamped(self) -> None:
        """14b. Page size is bounded; untrusted pages are clamped."""
        for i in range(7):
            self._create(
                f"crypto | طريقة{i} | USDT | NET{i} | مزود | DEST{i} | -"
            )

        update = self._press("pm:list")
        text = _edited(update.callback_query)
        self.assertIn("من 7", text)  # bounded header
        self.assertIn("طريقة0", text)
        self.assertNotIn("طريقة6", text)  # page 1 holds only 5
        self.assertEqual(text.count("🟢 نشطة"), admin.PAGE_SIZE)
        markup = _markup_of_edit(update.callback_query)
        nav = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("pm:page:")
        ]
        self.assertEqual(nav, ["pm:page:2"])

        update = self._press("pm:page:2")
        text = _edited(update.callback_query)
        self.assertIn("طريقة6", text)
        self.assertNotIn("طريقة0", text)

        # Out-of-range and garbage page ids clamp or reject safely.
        update = self._press("pm:page:999")
        self.assertIn("طريقة6", _edited(update.callback_query))
        update = self._press("pm:page:abc")
        self.assertEqual(_answered(update.callback_query), admin.MSG_INVALID)
        update = self._press("pm:page:0")
        self.assertEqual(_answered(update.callback_query), admin.MSG_INVALID)


# ══════════════════════════════════════════════════════════════════════
# 16. PERSISTENCE + PANEL + EDIT TEMPLATE
# ══════════════════════════════════════════════════════════════════════


class TestPersistenceAndPanels(PaymentMethodTestBase):
    def test_persistence_after_reopening_connection(self) -> None:
        """16. Everything survives a brand-new connection (restart)."""
        mid = self._create()
        self._press(f"pm:off:{mid}")
        self._edit(
            f"{mid} | cash | اسم معدَّل | EGP | - | مزود | 01111111111 | "
            "ملاحظة"
        )

        rows = self._raw(
            "SELECT * FROM payment_methods WHERE id = ?", (mid,)
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["category"], "cash")
        self.assertEqual(row["display_name"], "اسم معدَّل")
        self.assertEqual(row["destination"], "01111111111")
        self.assertEqual(row["instructions"], "ملاحظة")
        self.assertEqual(row["is_active"], 0)

        # Module-level reads resolve purely from SQLite, no warm state.
        fresh = store.get_payment_method(mid)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.display_name, "اسم معدَّل")

    def test_panel_shows_counts_and_buttons(self) -> None:
        self._create()
        self._press("pm:off:1")

        update = _update(ADMIN_A, "/paymethods")
        _run(admin.paymethods_command(update, MagicMock()))
        text = _reply(update)
        self.assertIn(admin.PANEL_HEADER, text)
        self.assertIn("النشطة: 0 — الإجمالي: 1", text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        callbacks = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks, ["pm:help", "pm:list"])

        # pm:help renders the pipe-form instructions.
        help_update = self._press("pm:help")
        self.assertIn("/addpm", _edited(help_update.callback_query))

    def test_edit_template_shows_full_values_and_prefilled_command(self) -> None:
        mid = self._create()
        update = self._press(f"pm:edit:{mid}")
        text = _edited(update.callback_query)
        self.assertIn(f"/editpm {mid} | crypto", text)
        self.assertIn("TXtest1234567890", text)  # full destination
        self.assertIn("محفظة الاختبار", text)

    def test_list_masks_destination(self) -> None:
        self._create()
        update = self._press("pm:list")
        text = _edited(update.callback_query)
        self.assertNotIn("TXtest1234567890", text)
        self.assertIn("TXtest…7890", text)


# ══════════════════════════════════════════════════════════════════════
# TELEGRAM ISOLATION (MT-ADMIN-02)
# ══════════════════════════════════════════════════════════════════════


class TestIsolation(PaymentMethodTestBase):
    def test_group_channel_commands_silent(self) -> None:
        for text, handler in (
            ("/paymethods", admin.paymethods_command),
            (f"/addpm {self.CRYPTO_FORM}", admin.add_pm_command),
            ("/editpm 1 | cash | n | EGP | - | p | d | -",
             admin.edit_pm_command),
        ):
            with self.subTest(text=text[:24]):
                update = _update(
                    STRANGER, text,
                    chat_type="supergroup", chat_id=-100123,
                )
                _run(handler(update, MagicMock()))
                update.message.reply_text.assert_not_called()
        self.assertEqual(self._rows(), [])

    def test_group_channel_callbacks_silent(self) -> None:
        self._create()
        for data in ("pm:list", "pm:help", "pm:off:1", "pm:delyes:1"):
            with self.subTest(data=data):
                update = _callback(
                    STRANGER, data,
                    chat_type="channel", chat_id=-100456,
                )
                _run(admin.payment_method_callback(update, MagicMock()))
                # Silent dismissal: answer with NO text, no edit.
                update.callback_query.answer.assert_called_once()
                self.assertIsNone(_answered(update.callback_query))
                update.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(self._rows()[0]["is_active"], 1)


# ══════════════════════════════════════════════════════════════════════
# SECURITY: no hard-coding, no secrets in logs, strict callbacks
# ══════════════════════════════════════════════════════════════════════


class TestSecurityGuards(PaymentMethodTestBase):
    """Source-level guards: the shipped modules stay generic.

    Extends the temp-DB fixture so audit-logging assertions run
    against an isolated database.
    """

    PAYMENT_SOURCES = ("payment_method_store.py", "payment_method_admin.py")

    FORBIDDEN_TOKENS = (
        "binance", "bitget", "bybit", "bep20", "trc20", "erc20",
        "bitcoin", "litecoin", "vodafone", "orange", "etisalat",
        "we pay",
    )

    OLD_REPO_LITERALS = (
        "01062275398",
        "295ecbbb578ab56a4b5b7328db9f8c1cd1cd2224",
    )

    def _source(self, name: str) -> str:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_no_hardcoded_provider_or_network_tokens(self) -> None:
        """No provider/network examples exist as literals in source."""
        for name in self.PAYMENT_SOURCES:
            text = self._source(name).lower()
            for token in self.FORBIDDEN_TOKENS:
                self.assertNotRegex(
                    text,
                    rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
                    f"{name} must not hard-code {token!r}",
                )

    def test_no_blockchain_address_literals(self) -> None:
        """No raw addresses/phone numbers in the payment modules."""
        patterns = (
            r"0x[0-9a-fA-F]{40}",
            r"01[0125][0-9]{8}",
            r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b",
        )
        for name in self.PAYMENT_SOURCES:
            text = self._source(name)
            for pattern in patterns:
                self.assertIsNone(
                    re.search(pattern, text),
                    f"{name} contains an address-like literal ({pattern})",
                )

    def test_old_repo_addresses_not_shipped(self) -> None:
        """The old repo's wallet literals appear nowhere in new code."""
        for name in self.PAYMENT_SOURCES + ("db.py",):
            text = self._source(name)
            for literal in self.OLD_REPO_LITERALS:
                self.assertNotIn(literal, text, f"{name} ships {literal}")

    def test_category_is_the_only_closed_set(self) -> None:
        """Categories are crypto/cash; asset/provider/network are free."""
        self.assertEqual(store.CATEGORIES, ("crypto", "cash"))
        with self.assertRaises(store.PaymentMethodValidationError):
            store.validate_category("bank_transfer")
        # ...while every free-form field accepts novel values.
        form = store.validate_form(
            "crypto", "name", "NOVEL-ASSET", "NOVEL-NET",
            "NOVEL-PROVIDER", "NOVEL-DEST", "note",
        )
        self.assertEqual(form.asset, "NOVEL-ASSET")
        self.assertEqual(form.network, "NOVEL-NET")
        self.assertEqual(form.provider, "NOVEL-PROVIDER")

    def test_destination_never_logged(self) -> None:
        """Audit lines carry ids/actors only — never the destination."""
        with self.assertLogs("payment_method_store", level="INFO") as cm:
            created = store.create_payment_method(
                category="crypto",
                display_name="تسجيل",
                asset="USDT",
                network="NET",
                provider="مزود",
                destination="SECRET-DEST-VALUE-98765",
                created_by=ADMIN_A,
            )
            store.list_payment_methods()
            store.update_payment_method(
                created.id,
                category="crypto",
                display_name="تسجيل2",
                asset="USDT",
                network="NET",
                provider="مزود",
                destination="SECRET-DEST-VALUE-98765",
                updated_by=ADMIN_A,
            )
            store.set_payment_method_active(
                created.id, False, updated_by=ADMIN_A
            )
            store.delete_payment_method(created.id, deleted_by=ADMIN_A)
        joined = "\n".join(cm.output)
        self.assertNotIn("SECRET-DEST-VALUE-98765", joined)
        # The auditable path IS present: operation + id + admin.
        self.assertIn("Payment method created", joined)
        self.assertIn(f"id={created.id}", joined)
        self.assertIn(f"admin={ADMIN_A}", joined)
        self.assertIn("Payment method updated", joined)
        self.assertIn("deactivated", joined)
        self.assertIn("Payment method deleted", joined)

    def test_destination_repr_redacted(self) -> None:
        method = store.PaymentMethod(
            id=1, category="crypto", display_name="x", asset="USDT",
            network="N", provider="p", destination="HIDDEN-DEST",
            instructions=None, is_active=True, sort_order=0,
            created_by=ADMIN_A, updated_by=ADMIN_A,
            created_at="t", updated_at="t",
        )
        self.assertNotIn("HIDDEN-DEST", repr(method))


# ══════════════════════════════════════════════════════════════════════
# CALLBACK PARSERS (untrusted payloads)
# ══════════════════════════════════════════════════════════════════════


class TestCallbackParsing(unittest.TestCase):
    def test_valid_payloads(self) -> None:
        self.assertEqual(admin.parse_callback("pm:list"), ("list", None))
        self.assertEqual(admin.parse_callback("pm:help"), ("help", None))
        self.assertEqual(admin.parse_callback("pm:page:3"), ("page", 3))
        self.assertEqual(admin.parse_callback("pm:edit:12"), ("edit", 12))
        self.assertEqual(admin.parse_callback("pm:on:1"), ("on", 1))
        self.assertEqual(admin.parse_callback("pm:off:2"), ("off", 2))
        self.assertEqual(admin.parse_callback("pm:del:4"), ("del", 4))
        self.assertEqual(
            admin.parse_callback("pm:delyes:4"), ("delyes", 4)
        )

    def test_invalid_payloads(self) -> None:
        for bad in (
            None,
            "",
            "pm:",
            "PM:list",
            "pm:hack",
            "pm:hack:1",
            "pm:list:1",      # no-id op with an id
            "pm:help:2",      # no-id op with an id
            "pm:page",        # id-required op without an id
            "pm:page:0",
            "pm:page:-3",
            "pm:page:abc",
            "pm:edit:",
            "pm:edit:1:2",
            "sup:list",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(admin.parse_callback(bad))

    def test_form_parsers(self) -> None:
        self.assertIsNone(admin.parse_add_form("a | b"))
        parsed = admin.parse_add_form("cash | name | EGP | - | p | dest")
        self.assertEqual(len(parsed), 7)
        self.assertIsNone(parsed[6])
        # Extra pipes fold into the trailing instructions field.
        parsed = admin.parse_add_form(
            "cash | name | EGP | - | p | dest | a | b"
        )
        self.assertEqual(parsed[6], "a | b")

        self.assertIsNone(admin.parse_edit_form("cash | x"))
        self.assertIsNone(admin.parse_edit_form("0 | cash | x"))
        mid, form = admin.parse_edit_form(
            "7 | cash | name | EGP | - | p | dest | -"
        )
        self.assertEqual(mid, 7)
        self.assertEqual(form[0], "cash")


# ══════════════════════════════════════════════════════════════════════
# BOT.PY REGISTRATION
# ══════════════════════════════════════════════════════════════════════


class TestBotRegistrations(unittest.TestCase):
    """bot.py must register the MT-ADMIN-08 handlers as designed."""

    def setUp(self) -> None:
        self._saved_channels = dict(config.CHANNELS)
        config.CHANNELS.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config.CHANNELS.clear()
        config.CHANNELS.update(self._saved_channels)

    def _capture_main_handlers(self) -> list:
        captured: list = []
        app = MagicMock()
        app.add_handler = lambda handler, group=None: captured.append(
            (handler, group)
        )
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "12345:TESTTOKEN"}
        ), patch.object(
            bot_mod, "ApplicationBuilder", return_value=builder
        ), patch.object(bot_mod, "run_single_entry"), patch.object(
            bot_mod, "db"
        ):
            config.CHANNELS.clear()
            bot_mod.main()
        return captured

    def test_commands_registered(self) -> None:
        from telegram.ext import CommandHandler

        captured = self._capture_main_handlers()
        expected = {
            "paymethods": admin.paymethods_command,
            "addpm": admin.add_pm_command,
            "editpm": admin.edit_pm_command,
        }
        for name, callback in expected.items():
            matches = [
                h
                for h, _g in captured
                if isinstance(h, CommandHandler) and name in h.commands
            ]
            self.assertEqual(len(matches), 1, f"one /{name} handler")
            self.assertIs(matches[0].callback, callback)

    def test_callback_family_registered(self) -> None:
        from telegram.ext import CallbackQueryHandler

        captured = self._capture_main_handlers()
        matches = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and h.callback is admin.payment_method_callback
        ]
        self.assertEqual(len(matches), 1)
        handler, group = matches[0]
        self.assertEqual(group, 5)
        self.assertEqual(handler.pattern.pattern, r"^pm:")
        # Disjoint from every other registered callback family.
        self.assertFalse(handler.pattern.match("sup:list"))
        self.assertFalse(handler.pattern.match("atw:x"))
        self.assertFalse(handler.pattern.match("mproof:1"))

    def test_commands_registered_once(self) -> None:
        from telegram.ext import CommandHandler

        captured = self._capture_main_handlers()
        for name, callback in (
            ("paymethods", admin.paymethods_command),
            ("addpm", admin.add_pm_command),
            ("editpm", admin.edit_pm_command),
        ):
            matches = [
                (h, g)
                for h, g in captured
                if isinstance(h, CommandHandler) and name in h.commands
            ]
            self.assertEqual(len(matches), 1, name)
            self.assertIs(matches[0][0].callback, callback)
            self.assertEqual(matches[0][1], 0)


if __name__ == "__main__":
    unittest.main()

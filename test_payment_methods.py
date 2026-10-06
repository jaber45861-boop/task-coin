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
ADMIN_B = 222222
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

        # The interactive wizard stages its form in a module-level
        # per-admin slot — isolate every test from any leftover state.
        admin._WIZARD_STATES.clear()
        self.addCleanup(admin._WIZARD_STATES.clear)

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

        # Bare /addpm now starts the interactive wizard (the pipe
        # form above keeps working unchanged).
        update = _update(ADMIN_A, "/addpm")
        _run(admin.add_pm_command(update, MagicMock()))
        reply = _reply(update)
        self.assertIn(admin.WZ_ADD_TITLE, reply)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        callbacks = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks, ["pm:wcat:1", "pm:wcat:2", "pm:wcancel"])
        self.assertIn(ADMIN_A, admin._WIZARD_STATES)

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
        # Bare /editpm <id> no longer shows usage: unknown ids report
        # NOT_FOUND (no state armed)…
        update = self._edit("1")
        self.assertEqual(_reply(update), admin.MSG_NOT_FOUND)
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)
        # …and a real id opens the interactive field menu.
        mid = self._create()
        update = self._edit(str(mid))
        reply = _reply(update)
        self.assertIn(f"#{mid}", reply)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        callbacks = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
        ]
        self.assertIn("pm:wsave", callbacks)
        self.assertIn("pm:wcancel", callbacks)
        self.assertIn("pm:wfield:5", callbacks)  # provider
        # Cancel drops the staged state and mutates nothing.
        cancel = _callback(ADMIN_A, "pm:wcancel")
        _run(admin.payment_method_callback(cancel, MagicMock()))
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)
        self.assertEqual(len(self._rows()), 1)


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
# ══════════════════════════════════════════════════════════════════
# INTERACTIVE WIZARD — bare /addpm and /editpm <id>
# ══════════════════════════════════════════════════════════════════


class WizardTestBase(PaymentMethodTestBase):
    """Shared drivers for the button/text wizard flows."""

    def _start(
        self,
        *,
        admin_id: int = ADMIN_A,
        text: str = "/addpm",
        chat_type: str = "private",
    ) -> MagicMock:
        update = _update(admin_id, text, chat_type=chat_type)
        _run(admin.add_pm_command(update, MagicMock()))
        return update

    def _press_w(
        self, data: str, *, admin_id: int = ADMIN_A, chat_type: str = "private"
    ) -> MagicMock:
        update = _callback(admin_id, data, chat_type=chat_type)
        _run(admin.payment_method_callback(update, MagicMock()))
        return update

    def _type(self, text: str, *, admin_id: int = ADMIN_A) -> MagicMock:
        update = _update(admin_id, text)
        _run(admin.wizard_text_input(update, MagicMock()))
        return update

    @staticmethod
    def _reply_markup_of(message_update: MagicMock) -> InlineKeyboardMarkup:
        return message_update.message.reply_text.call_args[1]["reply_markup"]

    @staticmethod
    def _buttons(markup: InlineKeyboardMarkup | None) -> list[str]:
        if markup is None:
            return []
        return [
            b.callback_data for row in markup.inline_keyboard for b in row
        ]

    @staticmethod
    def _labels(markup: InlineKeyboardMarkup | None) -> list[str]:
        if markup is None:
            return []
        return [b.text for row in markup.inline_keyboard for b in row]

    def _drive_to_review(
        self,
        *,
        asset: str = "WZASSET",
        network: str = "WZNET",
        name: str = "وسيلة الاختبار",
        provider: str = "مزود الاختبار",
        destination: str = "WZDEST123456",
        min_text: str = "0",
    ) -> MagicMock:
        """Bare /addpm → review page on a fresh database (no suggestions).

        ``min_text`` is typed on the minimum-deposit step that now
        follows the destination (``"0"`` = leave it unconfigured).
        """
        start = self._start()
        self.assertIn(admin.WZ_ADD_TITLE, _reply(start))
        stage2 = self._press_w("pm:wcat:1")
        asset_prompt = _edited(stage2.callback_query)
        if asset_prompt == admin.WZ_SELECT_ASSET:
            # This database already holds suggestions → use manual
            # entry so the flow stays value-driven.
            self._press_w("pm:wmanual")
        else:
            self.assertIn(admin.WZ_PROMPT_ASSET, asset_prompt)
        stage3 = self._type(asset)
        network_prompt = _reply(stage3)
        if network_prompt == admin.WZ_SELECT_NETWORK:
            self._press_w("pm:wmanual")
        else:
            self.assertEqual(network_prompt, admin.WZ_PROMPT_NETWORK)
        stage4 = self._type(network)
        self.assertEqual(_reply(stage4), admin.WZ_PROMPT_NAME)
        stage5 = self._type(name)
        self.assertEqual(_reply(stage5), admin.WZ_PROMPT_PROVIDER)
        stage6 = self._type(provider)
        dest_prompt = _reply(stage6)
        self.assertIn("Private Key", dest_prompt)
        self.assertIn("Seed Phrase", dest_prompt)
        stage7 = self._type(destination)
        min_prompt = _reply(stage7)
        self.assertIn(admin.WZ_PROMPT_MIN_DEPOSIT, min_prompt)
        stage8 = self._type(min_text)
        self.assertEqual(_reply(stage8), admin.WZ_PROMPT_INSTRUCTIONS)
        return self._press_w("pm:wskip")


class TestAddWizard(WizardTestBase):
    """The requested scenarios for the /addpm wizard."""

    def test_addpm_starts_wizard(self) -> None:
        """1. Bare /addpm opens the wizard; nothing is persisted."""
        # Group invocation stays silent first (MT-ADMIN-02 isolation).
        group_update = _update(
            ADMIN_A, "/addpm", chat_type="supergroup", chat_id=-1007
        )
        _run(admin.add_pm_command(group_update, MagicMock()))
        group_update.message.reply_text.assert_not_called()

        update = self._start()
        reply = _reply(update)
        self.assertIn(admin.WZ_ADD_TITLE, reply)
        self.assertIn("/addpm", reply)  # legacy form still discoverable
        self.assertEqual(
            self._buttons(self._reply_markup_of(update)),
            ["pm:wcat:1", "pm:wcat:2", "pm:wcancel"],
        )
        self.assertIn(ADMIN_A, admin._WIZARD_STATES)
        self.assertEqual(self._rows(), [])

    def test_non_admin_cannot_start_wizard(self) -> None:
        """2. Non-admin: no wizard, no buttons, zero rows."""
        update = _update(STRANGER, "/addpm")
        _run(admin.add_pm_command(update, MagicMock()))
        self.assertEqual(_reply(update), admin.MSG_ADMIN_ONLY)
        self.assertNotIn(STRANGER, admin._WIZARD_STATES)
        self.assertEqual(self._rows(), [])
        # …and a non-admin pressing a wizard button is refused too.
        press = self._press_w("pm:wconfirm", admin_id=STRANGER)
        self.assertEqual(
            _answered(press.callback_query), admin.MSG_ADMIN_ONLY
        )
        self.assertEqual(self._rows(), [])

    def test_full_crypto_flow_creates_once(self) -> None:
        """3 + 9 + 11 + 12 + 14. Crypto → address → skip notes →
        correct review → exactly one row on confirm."""
        review = self._drive_to_review()
        text = _edited(review.callback_query)
        self.assertIn("🔎 مراجعة وسيلة الدفع", text)
        self.assertIn("الفئة: crypto", text)
        self.assertIn("الاسم: وسيلة الاختبار", text)
        self.assertIn("العملة: WZASSET", text)
        self.assertIn("الشبكة: WZNET", text)
        self.assertIn("المزود: مزود الاختبار", text)
        self.assertIn("العنوان: WZDEST123456", text)
        self.assertIn("الملاحظات: -", text)
        self.assertEqual(
            self._buttons(_markup_of_edit(review.callback_query)),
            ["pm:wconfirm", "pm:wmenu", "pm:wcancel"],
        )
        # The review page wrote NOTHING before confirmation.
        self.assertEqual(self._rows(), [])

        done = self._press_w("pm:wconfirm")
        self.assertIn("تمت إضافة الوسيلة #1", _edited(done.callback_query))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["category"], "crypto")
        self.assertEqual(row["asset"], "WZASSET")
        self.assertEqual(row["network"], "WZNET")
        self.assertEqual(row["display_name"], "وسيلة الاختبار")
        self.assertEqual(row["provider"], "مزود الاختبار")
        self.assertEqual(row["destination"], "WZDEST123456")
        self.assertIsNone(row["instructions"])  # skipped
        self.assertEqual(row["created_by"], ADMIN_A)
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)

    def test_cash_flow_skips_network(self) -> None:
        """4. Cash is offered and completes without a network step."""
        self._start()
        stage = self._press_w("pm:wcat:2")
        self.assertIn(admin.WZ_PROMPT_ASSET, _edited(stage.callback_query))
        stage = self._type("EGP")
        self.assertEqual(_reply(stage), admin.WZ_PROMPT_NAME)  # no network
        self._type("كاش الاختبار")
        self._type("مزود الكاش")
        stage = self._type("WZCASHDEST")
        self.assertIn(admin.WZ_PROMPT_MIN_DEPOSIT, _reply(stage))
        stage = self._type("0")
        self.assertEqual(_reply(stage), admin.WZ_PROMPT_INSTRUCTIONS)
        review = self._press_w("pm:wskip")
        text = _edited(review.callback_query)
        self.assertIn("الفئة: cash", text)
        self.assertIn("الشبكة: -", text)
        self._press_w("pm:wconfirm")
        row = self._rows()[0]
        self.assertEqual(row["category"], "cash")
        self.assertIsNone(row["network"])

    def test_asset_and_network_suggestions_from_system(self) -> None:
        """5 + 7. Buttons show what the system ALREADY holds;
        crafted indexes are rejected without touching the state."""
        self._create()  # crypto / USDT / TESTNET already in the system
        self._start()
        pick = self._press_w("pm:wcat:1")
        asset_markup = _markup_of_edit(pick.callback_query)
        self.assertIn("pm:wasset:1", self._buttons(asset_markup))
        self.assertIn("USDT", self._labels(asset_markup))

        bad = self._press_w("pm:wasset:99")
        self.assertEqual(
            _answered(bad.callback_query), admin.MSG_WIZARD_INVALID_OPTION
        )
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "asset"
        )
        self.assertEqual(self._rows()[0]["asset"], "USDT")  # seed intact

        net = self._press_w("pm:wasset:1")
        net_markup = _markup_of_edit(net.callback_query)
        self.assertIn("pm:wnet:1", self._buttons(net_markup))
        self.assertIn("TESTNET", self._labels(net_markup))
        self.assertIn("pm:wnone", self._buttons(net_markup))

        bad = self._press_w("pm:wnet:42")
        self.assertEqual(
            _answered(bad.callback_query), admin.MSG_WIZARD_INVALID_OPTION
        )
        stage = self._press_w("pm:wnet:1")
        self.assertEqual(_edited(stage.callback_query), admin.WZ_PROMPT_NAME)

    def test_manual_asset_entry_validated_by_store(self) -> None:
        """6. Typing while buttons are pending gets guidance; the
        manual path refuses empty values via the EXISTING validator."""
        self._create()
        self._start()
        self._press_w("pm:wcat:1")
        # Free text while suggestion buttons are pending → guidance,
        # state unchanged.
        hint = self._type("WHATEVER")
        self.assertEqual(_reply(hint), admin.MSG_WIZARD_INVALID_OPTION)
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "asset"
        )
        # Manual entry…
        manual = self._press_w("pm:wmanual")
        self.assertEqual(
            _edited(manual.callback_query), admin.WZ_PROMPT_ASSET_MANUAL
        )
        for bad in ("", "   "):
            resp = self._type(bad)
            reply = _reply(resp)
            self.assertIn("❌", reply)
            self.assertIn("العملة", reply)
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "asset"
        )
        ok = self._type("WZNEWASSET")
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "network"
        )
        self.assertIn(admin.WZ_SELECT_NETWORK, _reply(ok))

    def test_empty_and_secret_destination_rejected(self) -> None:
        """10. Empty addresses never persist; the private-key marker
        rule applies on the wizard path too."""
        self._start()
        self._press_w("pm:wcat:1")
        self._type("WZASSET")
        self._type("WZNET")
        self._type("الاسم")
        self._type("المزود")
        for bad in ("", "   ", "\t"):
            resp = self._type(bad)
            reply = _reply(resp)
            self.assertIn("❌", reply)
            self.assertIn("العنوان", reply)
        resp = self._type("my private key material")
        self.assertIn("مفتاح خاص", _reply(resp))
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "destination"
        )
        self.assertEqual(self._rows(), [])
        # A valid public address advances to the minimum-deposit step.
        resp = self._type("WZDEST-OK")
        self.assertIn(admin.WZ_PROMPT_MIN_DEPOSIT, _reply(resp))
        # …and clearing the minimum reaches the optional notes.
        resp = self._type("0")
        self.assertEqual(_reply(resp), admin.WZ_PROMPT_INSTRUCTIONS)

    def test_cancel_creates_nothing(self) -> None:
        """13. Cancel drops the state; later presses are inert."""
        self._drive_to_review()
        cancel = self._press_w("pm:wcancel")
        self.assertEqual(
            _edited(cancel.callback_query), admin.MSG_WIZARD_CANCELED
        )
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)
        self.assertEqual(self._rows(), [])
        again = self._press_w("pm:wconfirm")
        self.assertEqual(
            _answered(again.callback_query), admin.MSG_WIZARD_STALE
        )
        self.assertEqual(self._rows(), [])

    def test_double_confirm_creates_once(self) -> None:
        """15. A double press on confirmation yields exactly one row."""
        self._drive_to_review()
        first = self._press_w("pm:wconfirm")
        self.assertIn("تمت إضافة", _edited(first.callback_query))
        second = self._press_w("pm:wconfirm")
        self.assertEqual(
            _answered(second.callback_query), admin.MSG_WIZARD_STALE
        )
        self.assertEqual(len(self._rows()), 1)

    def test_other_admin_cannot_drive_wizard(self) -> None:
        """16. The wizard belongs to the actor who started it — another
        admin (and any non-admin) has no handle on it."""
        config.ADMINS[:] = [ADMIN_A, ADMIN_B]
        self._drive_to_review()
        hijack = self._press_w("pm:wconfirm", admin_id=ADMIN_B)
        self.assertEqual(
            _answered(hijack.callback_query), admin.MSG_WIZARD_STALE
        )
        self.assertEqual(self._rows(), [])
        stranger = self._press_w("pm:wconfirm", admin_id=STRANGER)
        self.assertEqual(
            _answered(stranger.callback_query), admin.MSG_ADMIN_ONLY
        )
        self.assertEqual(self._rows(), [])
        # The original admin can still finish it.
        done = self._press_w("pm:wconfirm")
        self.assertIn("تمت إضافة", _edited(done.callback_query))
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self._rows()[0]["created_by"], ADMIN_A)

    def test_expired_state_cannot_confirm(self) -> None:
        """17. After the TTL the staged data can no longer complete
        an operation — and the stale notice is sent exactly once."""
        self._drive_to_review()
        admin._WIZARD_STATES[ADMIN_A][
            "updated_at"
        ] -= admin.WIZARD_TTL_SECONDS + 1
        # First contact after the TTL collects the slot with ONE stale
        # notice (free-text path)…
        resp = self._type("أي نص")
        self.assertEqual(_reply(resp), admin.MSG_WIZARD_STALE)
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)
        resp = self._type("نص ثانٍ")
        resp.message.reply_text.assert_not_called()
        # …and a later confirm press mutates nothing either.
        press = self._press_w("pm:wconfirm")
        self.assertEqual(
            _answered(press.callback_query), admin.MSG_WIZARD_STALE
        )
        self.assertEqual(self._rows(), [])

    def test_db_failure_leaves_consistent_state(self) -> None:
        """18. A failing insert reports the error, writes nothing and
        leaves no half-open wizard behind."""
        self._drive_to_review()
        with patch.object(
            admin.store,
            "create_payment_method",
            side_effect=RuntimeError("db down"),
        ):
            press = self._press_w("pm:wconfirm")
        self.assertEqual(_edited(press.callback_query), admin.MSG_ERROR)
        self.assertEqual(self._rows(), [])
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)

    def test_deposit_flow_sees_wizard_method(self) -> None:
        """19. A wizard-created method reaches the deposit surface via
        the exact selection filter of list_deposit_methods.  The
        minimum is staged in the wizard (EGP 50 → 5000 units) because
        enabling deposits without it is refused (decision 10)."""
        self._drive_to_review(asset="EGP", min_text="50")
        self._press_w("pm:wconfirm")
        mid = self._rows()[0]["id"]
        store.set_payment_method_deposits_enabled(
            mid, True, updated_by=ADMIN_A
        )
        visible = [
            pm.id
            for pm in store.list_payment_methods(active_only=True)
            if pm.deposits_enabled
        ]
        self.assertEqual(visible, [mid])

    def test_existing_methods_still_work(self) -> None:
        """20 + 22. Pre-existing rows and the legacy pipe form keep
        working unchanged alongside the wizard."""
        legacy_id = self._create()
        before = self._rows()[0]
        self._drive_to_review()
        self._press_w("pm:wconfirm")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["destination"], before["destination"])
        self.assertEqual(rows[0]["is_active"], 1)
        # List still renders both with destination masking intact.
        listing = self._press_w("pm:list")
        text = _edited(listing.callback_query)
        self.assertNotIn("TXtest1234567890", text)
        self.assertIn("وسيلة الاختبار", text)
        # The existing activation button still toggles the old row.
        self._press_w(f"pm:off:{legacy_id}")
        self.assertEqual(self._rows()[0]["is_active"], 0)
        # Legacy one-liner creates exactly as before…
        update = self._add(self.CASH_FORM)
        self.assertIn("تمت إضافة الوسيلة #3", _reply(update))
        # …and a malformed pipe line still yields the usage message.
        update = self._add("crypto | only | three")
        self.assertEqual(_reply(update), admin.MSG_USAGE_ADD)

    def test_no_destination_in_wizard_logs(self) -> None:
        """21. Audit lines carry wizard/operation ids — never the
        staged destination."""
        secret_dest = "WZSECRETDESTVALUE987"
        with self.assertLogs("payment_method_admin", level="INFO") as cm_a:
            with self.assertLogs("payment_method_store", level="INFO") as cm_s:
                self._drive_to_review(destination=secret_dest)
                self._press_w("pm:wconfirm")
        joined = "\n".join(cm_a.output + cm_s.output)
        self.assertNotIn(secret_dest, joined)
        self.assertIn("Payment method wizard started", joined)
        self.assertIn("Payment method created", joined)


class TestEditWizard(WizardTestBase):
    """Interactive /editpm <id> field menu."""

    def _open(self, method_id: int, *, admin_id: int = ADMIN_A) -> MagicMock:
        update = _update(admin_id, f"/editpm {method_id}")
        _run(admin.edit_pm_command(update, MagicMock()))
        return update

    def test_edit_menu_updates_one_field_and_saves(self) -> None:
        mid = self._create()
        opening = self._open(mid)
        text = _reply(opening)
        self.assertIn(f"✏️ تعديل وسيلة الدفع #{mid}", text)
        self.assertIn("العملة: USDT", text)
        self.assertIn("المزود: مزود الاختبار", text)
        buttons = self._buttons(self._reply_markup_of(opening))
        for expected in (
            "pm:wfield:1",  # category
            "pm:wfield:5",  # provider
            "pm:wfield:7",  # notes
            "pm:wsave",
            "pm:wcancel",
        ):
            self.assertIn(expected, buttons)

        prompt = self._press_w("pm:wfield:5")
        self.assertEqual(
            _edited(prompt.callback_query), admin.WZ_PROMPT_PROVIDER
        )
        bad = self._type("")
        self.assertIn("المزود", _reply(bad))
        self.assertEqual(
            admin._WIZARD_STATES[ADMIN_A]["step"], "provider"
        )
        ok = self._type("مزود جديد")
        menu_reply = _reply(ok)
        self.assertIn("المزود: مزود جديد", menu_reply)
        self.assertIn("العملة: USDT", menu_reply)  # untouched fields kept

        saved = self._press_w("pm:wsave")
        self.assertIn(
            f"تم تعديل الوسيلة #{mid}", _edited(saved.callback_query)
        )
        row = self._rows()[0]
        self.assertEqual(row["provider"], "مزود جديد")
        self.assertEqual(row["asset"], "USDT")
        self.assertEqual(row["display_name"], "محفظة الاختبار")
        self.assertEqual(row["updated_by"], ADMIN_A)
        self.assertEqual(row["created_by"], ADMIN_A)
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)
        # Single-use: a second save press is stale and writes nothing.
        again = self._press_w("pm:wsave")
        self.assertEqual(
            _answered(again.callback_query), admin.MSG_WIZARD_STALE
        )
        self.assertEqual(len(self._rows()), 1)

    def test_edit_menu_category_and_network_buttons(self) -> None:
        mid = self._create()
        self._open(mid)
        cat = self._press_w("pm:wfield:1")
        self.assertEqual(
            _edited(cat.callback_query), admin.WZ_PROMPT_CATEGORY
        )
        picked = self._press_w("pm:wcat:2")
        self.assertIn("الفئة: cash", _edited(picked.callback_query))
        net = self._press_w("pm:wfield:4")
        net_markup = _markup_of_edit(net.callback_query)
        self.assertIn("pm:wnone", self._buttons(net_markup))
        none_btn = self._press_w("pm:wnone")
        self.assertIn("الشبكة: -", _edited(none_btn.callback_query))
        self._press_w("pm:wsave")
        row = self._rows()[0]
        self.assertEqual(row["category"], "cash")
        self.assertIsNone(row["network"])

    def test_edit_wizard_missing_id_reports_not_found(self) -> None:
        update = self._open(999)
        self.assertEqual(_reply(update), admin.MSG_NOT_FOUND)
        self.assertNotIn(ADMIN_A, admin._WIZARD_STATES)

    def test_edit_wizard_denied_for_non_admin_and_silent_in_groups(
        self,
    ) -> None:
        update = _update(STRANGER, "/editpm 1")
        _run(admin.edit_pm_command(update, MagicMock()))
        self.assertEqual(_reply(update), admin.MSG_ADMIN_ONLY)
        self.assertNotIn(STRANGER, admin._WIZARD_STATES)
        update = _update(
            STRANGER, "/editpm 1", chat_type="supergroup", chat_id=-1009
        )
        _run(admin.edit_pm_command(update, MagicMock()))
        update.message.reply_text.assert_not_called()
        self.assertEqual(self._rows(), [])


# ════════════════════════════════════════════════════════════════════
# PER-METHOD MINIMUM DEPOSITS (per-asset atomic units; fail-closed)
# ════════════════════════════════════════════════════════════════════


class TestMinimumDepositUnits(WizardTestBase):
    """Decision 22: wizard create/edit of the minimum, the
    deposits-enable gate, and the asset-change clearing rule."""

    def _method(self, method_id: int):
        return store.get_payment_method(method_id)

    def _open(self, method_id: int, *, admin_id: int = ADMIN_A) -> MagicMock:
        update = _update(admin_id, f"/editpm {method_id}")
        _run(admin.edit_pm_command(update, MagicMock()))
        return update

    def _update_row(self, method_id: int, **overrides):
        """Full-field store update from the stored row — only the
        overrides this test cares about are spelled out."""
        row = dict(
            self._raw(
                "SELECT * FROM payment_methods WHERE id = ?",
                (method_id,),
            )[0]
        )
        fields = dict(
            category=row["category"],
            display_name=row["display_name"],
            asset=row["asset"],
            network=row["network"],
            provider=row["provider"],
            destination=row["destination"],
            instructions=row["instructions"],
            updated_by=ADMIN_A,
        )
        fields.update(overrides)
        return store.update_payment_method(method_id, **fields)

    def test_wizard_creates_min_in_asset_units(self) -> None:
        """The wizard stores the typed minimum at the staged asset's
        scale (EGP 50 → 5000 atomic units) and the review page
        renders it in that same asset's unit."""
        review = self._drive_to_review(asset="EGP", min_text="50")
        text = _edited(review.callback_query)
        self.assertIn("الحد الأدنى للإيداع: 50.00", text)
        self._press_w("pm:wconfirm")
        method = self._method(self._rows()[0]["id"])
        self.assertEqual(method.min_deposit_units, 5000)

    def test_wizard_edit_loads_and_saves_min(self) -> None:
        """Edit mode loads the stored minimum (unset → not-configured)
        and saves a new value at the method asset's own scale
        (USDT 1.5 → 150000000 atomic units)."""
        mid = self._create()  # USDT, minimum not configured
        opening = self._open(mid)
        self.assertIn("الحد الأدنى للإيداع: غير مضبوط", _reply(opening))
        self.assertIn(
            "pm:wfield:8", self._buttons(self._reply_markup_of(opening))
        )
        prompt = self._press_w("pm:wfield:8")
        self.assertIn(
            admin.WZ_PROMPT_MIN_DEPOSIT, _edited(prompt.callback_query)
        )
        resp = self._type("1.5")
        self.assertIn("الحد الأدنى للإيداع: 1.50000000", _reply(resp))
        saved = self._press_w("pm:wsave")
        self.assertIn(f"تم تعديل الوسيلة #{mid}", _edited(saved.callback_query))
        self.assertEqual(self._method(mid).min_deposit_units, 150000000)

    def test_wizard_asset_change_clears_staged_min(self) -> None:
        """Changing the asset inside the edit wizard drops the
        old-asset units immediately (menu shows not-configured) and
        the saved row keeps them cleared."""
        mid = self._create()  # USDT
        self._update_row(mid, min_deposit_units=1000)
        self._open(mid)
        self._press_w("pm:wfield:3")  # asset — suggestions pending
        self._press_w("pm:wmanual")
        resp = self._type("WZNEWASSET")
        menu = _reply(resp)
        self.assertIn("العملة: WZNEWASSET", menu)
        self.assertIn("الحد الأدنى للإيداع: غير مضبوط", menu)
        self._press_w("pm:wsave")
        method = self._method(mid)
        self.assertEqual(method.asset, "WZNEWASSET")
        self.assertIsNone(method.min_deposit_units)

    def test_enabling_deposits_requires_min(self) -> None:
        """Decision 10: with no minimum the enable flag is refused at
        the store AND on the pm:depon callback — the row stays
        deposits-disabled."""
        mid = self._create()  # minimum never configured
        with self.assertRaises(store.PaymentMethodValidationError):
            store.set_payment_method_deposits_enabled(
                mid, True, updated_by=ADMIN_A
            )
        self.assertFalse(self._method(mid).deposits_enabled)
        press = self._press(f"pm:depon:{mid}")
        answer = _answered(press.callback_query)
        self.assertIsNotNone(answer)
        self.assertIn("❌", answer)
        self.assertIn("الحد الأدنى", answer)
        self.assertFalse(self._method(mid).deposits_enabled)

    def test_enabling_deposits_requires_supported_asset(self) -> None:
        """Even with units stored, an asset with no registered scale
        can never be published for deposits (fail-closed)."""
        self._add(
            "crypto | وسيلة الاختبار | WZASSET | WZNET | مزود الاختبار | "
            "WZDEST123456 | -"
        )
        mid = self._rows()[0]["id"]
        # An int is already-canonical atomic units, so the minimum
        # itself is storable even though the asset has no scale.
        self._update_row(mid, min_deposit_units=1)
        with self.assertRaises(store.PaymentMethodValidationError):
            store.set_payment_method_deposits_enabled(
                mid, True, updated_by=ADMIN_A
            )
        self.assertFalse(self._method(mid).deposits_enabled)

    def test_asset_change_clears_min_and_blocks_reenable(self) -> None:
        """Decision 9 at the store: the units belong to the OLD
        asset — after a change they are cleared and deposits must be
        re-configured against the new asset before re-enabling."""
        mid = self._create()  # USDT
        self._update_row(mid, min_deposit_units=1000)
        on = store.set_payment_method_deposits_enabled(
            mid, True, updated_by=ADMIN_A
        )
        self.assertTrue(on.deposits_enabled)
        # Change the asset WITHOUT supplying a minimum → cleared.
        updated = self._update_row(mid, asset="EGP")
        self.assertEqual(updated.asset, "EGP")
        self.assertIsNone(updated.min_deposit_units)
        # Re-enabling after the change is refused…
        store.set_payment_method_deposits_enabled(
            mid, False, updated_by=ADMIN_A
        )
        with self.assertRaises(store.PaymentMethodValidationError):
            store.set_payment_method_deposits_enabled(
                mid, True, updated_by=ADMIN_A
            )
        # …until the minimum is re-set at the NEW asset's scale.
        self._update_row(mid, min_deposit_units="50")
        enabled = store.set_payment_method_deposits_enabled(
            mid, True, updated_by=ADMIN_A
        )
        self.assertTrue(enabled.deposits_enabled)
        self.assertEqual(enabled.min_deposit_units, 5000)  # EGP, 2 dp


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
        # Interactive wizard ops share the same strict grammar.
        self.assertEqual(admin.parse_callback("pm:wcat:1"), ("wcat", 1))
        self.assertEqual(admin.parse_callback("pm:wasset:6"), ("wasset", 6))
        self.assertEqual(admin.parse_callback("pm:wnet:2"), ("wnet", 2))
        self.assertEqual(admin.parse_callback("pm:wfield:7"), ("wfield", 7))
        for op in (
            "wmanual",
            "wskip",
            "wnone",
            "wmenu",
            "wreview",
            "wconfirm",
            "wsave",
            "wcancel",
        ):
            with self.subTest(op=op):
                self.assertEqual(admin.parse_callback(f"pm:{op}"), (op, None))

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
            "pm:wasset:0",      # suggestion index must be positive
            "pm:wcat:-1",
            "pm:wfield:abc",
            "pm:wconfirm:1",    # no-id wizard op with an id
            "pm:wcancel:2",
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

    def test_wizard_text_handler_registered_in_own_group(self) -> None:
        """The wizard free-text catch-all must live in its OWN group
        so neither it nor the task-wizard catch-all can shadow the
        other (first-match-wins within a group)."""
        from telegram.ext import MessageHandler

        captured = self._capture_main_handlers()
        mine = [
            (h, g)
            for h, g in captured
            if isinstance(h, MessageHandler)
            and h.callback is admin.wizard_text_input
        ]
        self.assertEqual(len(mine), 1)
        theirs = [
            (h, g)
            for h, g in captured
            if isinstance(h, MessageHandler)
            and h.callback is bot_mod.admin_task_wizard.wizard_text_input
        ]
        self.assertEqual(len(theirs), 1)
        self.assertNotEqual(mine[0][1], theirs[0][1])


if __name__ == "__main__":
    unittest.main()

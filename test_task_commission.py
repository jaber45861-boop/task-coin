"""
Advertiser Commission Integration Tests (MT-ADMIN-16)
=====================================================

Focused tests for the ONLY platform-settings integration implemented
by MT-ADMIN-16: the ``advertiser_commission`` read path at the ONE
canonical task-creation service (``task_creation``).

Covered (the micro-task's focused-test list):

 1. changing ``advertiser_commission`` changes NEW tasks' calculations
 2. an already-created task retains its original commission snapshot
 3. exact atomic-unit arithmetic (integer basis points, 10,000 = 100 %)
 4. sub-cent rewards (values below 0.01 USDT) stay exact
 5. deterministic commission rounding at boundary values (ceiling)
 6. settings missing → explicit ``SettingNotFoundError``, zero rows
 7. runtime setting changes take effect with NO restart (fresh read,
    no global cache)
 8. no float usage in the newly modified financial code (AST scan)

Intentionally NOT covered here, by design:

 * withdrawal minimum / withdrawal fee — ``withdrawal_rules`` is
   EGP-denominated (``MIN_WITHDRAW_EGP`` / ``WITHDRAW_FEE_EGP``) while
   the settings are USDT atomic units and no exchange rate exists in
   platform settings; integration is unsafe without a broader
   financial refactor (see the MT-ADMIN-16 report).
 * deposit minimum — no deposit flow exists anywhere in the codebase.

Run:
    python3 -m unittest test_task_commission -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile
import unittest

import db
import platform_settings as ps
import task_creation
from config import ADMINS
from task_creation import (
    TaskSpec,
    commission_units_for,
    create_task_from_spec,
)
from task_taxonomy import VERIFICATION_MANUAL

# The existing one admin model (config.is_admin / ADMINS) — the same
# identity platform_settings.set_setting authorizes.
ADMIN = ADMINS[0]

# 1 USDT in exact atomic units (wallet authority, mirrored by
# platform_settings.USDT_UNITS_PER_USDT).
USDT_SCALE = 100_000_000


def _spec(reward_units: int, *, title: str = "مهمة عمولة") -> TaskSpec:
    """A fully-valid manual TaskSpec with exact atomic reward units.

    ``reward`` (the whole-USDT display field) is derived so sub-cent
    rewards legitimately display as 0 while ``reward_units`` carries
    the exact accounting value.
    """
    whole, _fraction = divmod(reward_units, USDT_SCALE)
    return TaskSpec(
        title=title,
        description="وصف المهمة للاختبار",
        provider="instagram",
        action="follow",
        target={},
        verification=VERIFICATION_MANUAL,
        reward=whole,
        approver_id=ADMIN,
        reward_units=reward_units,
    )


# ════════════════════════════════════════════════════════════════════
# 1–5, 8: pure commission arithmetic (exact integers, no float)
# ════════════════════════════════════════════════════════════════════


class CommissionCalculationTests(unittest.TestCase):
    """``commission_units_for`` — exact, deterministic, integer-only."""

    def test_default_30_percent_exact(self):
        """3000 bp (the seeded default) is exactly 30 %."""
        self.assertEqual(
            commission_units_for(1 * USDT_SCALE, 3_000), 30_000_000
        )
        self.assertEqual(
            commission_units_for(25 * USDT_SCALE, 3_000), 7_500_000_00
        )
        self.assertEqual(
            commission_units_for(100 * USDT_SCALE, 3_000), 3_000_000_000
        )

    def test_exact_atomic_arithmetic(self):
        """No float: the integer formula matches the exact product."""
        # 123,456,789 * 3000 = 370,370,367,000 → /10,000 = 37,037,036.7
        self.assertEqual(commission_units_for(123_456_789, 3_000), 37_037_037)
        # 999,999,999 * 9999 = 9,998,999,990,001 → ceil = 999,900,000
        self.assertEqual(commission_units_for(999_999_999, 9_999), 999_900_000)

    def test_exact_multiple_never_bumped(self):
        """Ceiling must not add a unit to an exact multiple."""
        self.assertEqual(commission_units_for(40_000, 2_500), 10_000)
        self.assertEqual(commission_units_for(10_000, 10_000), 10_000)
        self.assertEqual(commission_units_for(100, 5_000), 50)

    def test_partial_unit_rounds_up_deterministically(self):
        """A partial unit always rounds UP to exactly one unit."""
        self.assertEqual(commission_units_for(1, 3_000), 1)    # 0.3 → 1
        self.assertEqual(commission_units_for(7, 3_000), 3)    # 2.1 → 3
        self.assertEqual(commission_units_for(9_999, 1), 1)    # 0.9999 → 1
        self.assertEqual(commission_units_for(10_001, 1), 2)   # 1.0001 → 2

    def test_boundary_rates(self):
        """0 bp → 0; 10,000 bp (100 %) → exactly the reward."""
        self.assertEqual(commission_units_for(1_234_567, 0), 0)
        self.assertEqual(commission_units_for(0, 3_000), 0)
        self.assertEqual(commission_units_for(0, 10_000), 0)
        self.assertEqual(
            commission_units_for(99_999_999, 10_000), 99_999_999
        )

    def test_sub_cent_rewards_stay_exact(self):
        """Rewards below 0.01 USDT are exact, never floored to 0 %."""
        self.assertEqual(commission_units_for(10_000, 3_000), 3_000)
        self.assertEqual(commission_units_for(1, 3_000), 1)
        self.assertEqual(commission_units_for(500_000, 3_000), 150_000)

    def test_commission_never_exceeds_reward(self):
        """bp ≤ 10,000 ⇒ commission_units ≤ reward_units (ceil safe)."""
        for units in (0, 1, 9_999, 10_000, USDT_SCALE, 123_456_789):
            for bp in (0, 1, 3_000, 9_999, 10_000):
                self.assertLessEqual(
                    commission_units_for(units, bp), units
                )

    def test_invalid_inputs_rejected_never_coerced(self):
        """bool / float / negative / non-int / out-of-range → ValueError."""
        for bad_reward in (True, 1.5, -1, "100", None):
            with self.assertRaises(ValueError):
                commission_units_for(bad_reward, 3_000)  # type: ignore[arg-type]
        for bad_bp in (True, 1.5, -1, "3000", None, 10_001):
            with self.assertRaises(ValueError):
                commission_units_for(1_000_000, bad_bp)  # type: ignore[arg-type]

    def test_no_float_no_round_in_commission_code(self):
        """AST scan of the NEW financial code: no float, no round()."""
        sources = [
            inspect.getsource(task_creation.commission_units_for),
            inspect.getsource(task_creation.create_task_from_spec),
        ]
        for source in sources:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, float)
                ):
                    self.fail(f"float literal in commission code: {source}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("float", "round")
                ):
                    self.fail(
                        f"{node.func.id}() call in commission code"
                    )
                if isinstance(node, ast.Name) and node.id == "float":
                    self.fail("float reference in commission code")


# ════════════════════════════════════════════════════════════════════
# 1, 2, 6, 7: settings → creation integration (snapshot + runtime)
# ════════════════════════════════════════════════════════════════════


class CommissionSnapshotTests(unittest.TestCase):
    """The canonical creation service reads the LIVE setting once."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)  # seeds advertiser_commission = 3000 bp

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── helpers ──────────────────────────────────────────────────

    def set_commission(self, bp: int) -> int:
        return ps.set_setting(
            ps.ADVERTISER_COMMISSION, bp, admin_user_id=ADMIN,
            db_path=self.db_path,
        )

    def raw_value(self, key: str) -> object:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else row[0]

    # ── tests ────────────────────────────────────────────────────

    def test_creation_snapshots_default_commission(self):
        """A new task stores the exact integer snapshot (seeded 3000)."""
        tid = create_task_from_spec(_spec(USDT_SCALE, title="افتراضية"))
        task = db.get_task(tid)
        self.assertEqual(task["commission_units"], 30_000_000)
        # Stored as a true SQLite integer, never REAL.
        with sqlite3.connect(self.db_path) as conn:
            kind = conn.execute(
                "SELECT typeof(commission_units) FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()[0]
        self.assertEqual(kind, "integer")

    def test_changing_setting_changes_new_tasks_no_restart(self):
        """Runtime change (same process) affects the NEXT creation only."""
        first = create_task_from_spec(_spec(USDT_SCALE, title="أولى"))
        self.assertEqual(
            db.get_task(first)["commission_units"], 30_000_000
        )

        self.assertEqual(self.set_commission(4_500), 4_500)

        second = create_task_from_spec(_spec(USDT_SCALE, title="ثانية"))
        self.assertEqual(
            db.get_task(second)["commission_units"], 45_000_000
        )
        # No restart, no re-import — the read is always fresh.
        self.assertEqual(self.raw_value(ps.ADVERTISER_COMMISSION), 4_500)

    def test_existing_task_keeps_original_snapshot(self):
        """Changing the setting NEVER alters an already-created task."""
        old_task = create_task_from_spec(_spec(USDT_SCALE, title="قديمة"))

        self.set_commission(5_000)

        new_task = create_task_from_spec(_spec(USDT_SCALE, title="حديثة"))

        # Old task: snapshot intact on re-read (get_task AND list_tasks).
        self.assertEqual(
            db.get_task(old_task)["commission_units"], 30_000_000
        )
        listed = {t["id"]: t for t in db.list_tasks()}
        self.assertEqual(listed[old_task]["commission_units"], 30_000_000)
        # New task: computed with the NEW rate.
        self.assertEqual(
            db.get_task(new_task)["commission_units"], 50_000_000
        )

    def test_missing_setting_is_explicit_and_writes_zero_rows(self):
        """No silent fallback: SettingNotFoundError, zero rows created."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM platform_settings WHERE key = ?",
                (ps.ADVERTISER_COMMISSION,),
            )
        before = len(db.list_tasks())
        with self.assertRaises(ps.SettingNotFoundError):
            create_task_from_spec(_spec(USDT_SCALE, title=" بلا إعداد"))
        self.assertEqual(len(db.list_tasks()), before)

    def test_creation_inside_caller_transaction(self):
        """Wizard path: the settings read joins the caller's transaction."""
        with db.transaction() as conn:
            tid = create_task_from_spec(
                _spec(10_000, title="داخل معاملة"), conn=conn
            )
        task = db.get_task(tid)
        self.assertEqual(task["reward_units"], 10_000)
        self.assertEqual(task["commission_units"], 3_000)

    def test_legacy_spec_without_units_snapshots_commission(self):
        """TaskSpec(reward=50) derives units exactly as db.create_task."""
        spec = TaskSpec(
            title="قديمة",
            description="وصف المهمة للاختبار",
            provider="instagram",
            action="follow",
            target={},
            verification=VERIFICATION_MANUAL,
            reward=50,
            approver_id=ADMIN,
        )
        task = db.get_task(create_task_from_spec(spec))
        self.assertEqual(task["reward_units"], 50 * USDT_SCALE)
        # 30 % of 50 USDT = 15 USDT, exact atomic units.
        self.assertEqual(task["commission_units"], 15 * USDT_SCALE)

    def test_sub_cent_task_reward_gets_exact_snapshot(self):
        """0.0001 USDT reward → exactly 0.00003 USDT commission."""
        tid = create_task_from_spec(_spec(10_000, title="دقيقة"))
        task = db.get_task(tid)
        self.assertEqual(task["reward"], 0)          # whole-USDT display
        self.assertEqual(task["reward_units"], 10_000)
        self.assertEqual(task["commission_units"], 3_000)

    def test_db_create_task_validates_commission_units(self):
        """db layer rejects bad snapshots; stores explicit good ones."""
        base = dict(
            title="تحقق", description="d", task_type="deterministic",
            reward=1,
        )
        for bad in (True, -1, 1.5, 2 ** 63, "10000"):
            with self.assertRaises(ValueError):
                db.create_task(**base, commission_units=bad)
        tid = db.create_task(**base, commission_units=30_000_000)
        self.assertEqual(
            db.get_task(tid)["commission_units"], 30_000_000
        )

    def test_direct_db_create_task_keeps_null_snapshot(self):
        """Legacy/direct creators keep NULL — nothing is invented."""
        tid = db.create_task(
            title="بدون عمولة", description="d",
            task_type="deterministic", reward=1,
        )
        self.assertIsNone(db.get_task(tid)["commission_units"])


if __name__ == "__main__":
    unittest.main()

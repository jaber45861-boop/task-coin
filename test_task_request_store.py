"""
User Task-Request Store («إضافة مهمة ➕»)
=========================================

Storage + state-machine contract for user-proposed tasks:

  Payload validation
  - every field re-validated with the existing canonical validators
  - unknown/missing fields, bad provider/action, bad reward rejected

  User transitions
  - create → pending (audit: submitted)
  - resubmit only for the owner, only from changes_requested

  Admin transitions
  - edit (pending only) keeps the previous payload in the audit
  - return → changes_requested (note stored)
  - reject → rejected, terminal, reason stored

  Approval
  - claim CAS: exactly one winner; replay → None
  - spec built from the STORED payload (manual contract, approver =
    approving admin)
  - approved request → active `tasks` row visible to the existing
    catalog/lifecycle; rejected request → no task, ever

Run:
    python3 -m pytest test_task_request_store.py -v
"""

import json

import pytest

import db
import task_request_store as store
from task_creation import TaskCreationError, create_task_from_spec
from task_request_store import TaskRequestError
from task_taxonomy import MANUAL_TASK_ACTIONS

OWNER = 4242
ADMIN = 777

VALID = {
    "title": "مهمة تجريبية",
    "description": "وصف المهمة للمنفذين",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "task_request_store_test.db")
    db.DB_PATH = db_path
    db.init_db(db_path)
    db.register_user(OWNER, "owner", "Owner")
    yield db_path


def _payload(**overrides) -> dict:
    payload = dict(VALID)
    payload.update(overrides)
    return payload


def _create(**overrides):
    return store.create_request(OWNER, _payload(**overrides))


# ════════════════════════════════════════════════════════════════════
# Payload validation
# ════════════════════════════════════════════════════════════════════


class TestValidation:
    def test_valid_payload_normalized(self, env):
        request = _create()
        assert request.status == store.STATUS_PENDING
        assert request.user_id == OWNER
        assert request.payload == {
            "title": "مهمة تجريبية",
            "description": "وصف المهمة للمنفذين",
            "provider": "instagram",
            "action": "follow",
            "target_ref": "https://instagram.com/example",
            "reward_units": 50_000_000,
        }
        assert [e["event"] for e in request.history] == ["submitted"]

    @pytest.mark.parametrize("field,value", [
        ("title", ""),
        ("title", "x" * 201),
        ("title", "line1\nline2"),
        ("description", ""),
        ("description", "d" * 1001),
        ("provider", "nope"),
        ("provider", 5),
        ("action", "referral"),
        ("target_ref", "t" * 501),
        ("reward", "abc"),
        ("reward", "-2"),
        ("reward", "0.000000001"),
        ("reward", True),
        ("reward", None),
    ])
    def test_invalid_field_rejected(self, env, field, value):
        with pytest.raises(TaskRequestError):
            _create(**{field: value})

    def test_action_must_be_allowed_for_provider(self, env):
        # telegram allows join_channel; instagram does not.
        store.validate_payload(_payload(
            provider="telegram", action="join_channel"
        ))
        with pytest.raises(TaskRequestError):
            store.validate_payload(_payload(
                provider="instagram", action="join_channel"
            ))

    def test_missing_required_field_rejected(self, env):
        payload = _payload()
        del payload["title"]
        with pytest.raises(TaskRequestError):
            store.create_request(OWNER, payload)

    def test_unknown_field_rejected(self, env):
        with pytest.raises(TaskRequestError):
            store.create_request(OWNER, _payload(status="approved"))

    def test_non_dict_rejected(self, env):
        with pytest.raises(TaskRequestError):
            store.create_request(OWNER, ["not", "a", "dict"])

    def test_target_ref_optional(self, env):
        request = _create(target_ref="")
        assert request.payload["target_ref"] == ""

    def test_arabic_indic_reward_digits_accepted(self, env):
        request = _create(reward="٠.٥")
        assert request.reward_units == 50_000_000


# ════════════════════════════════════════════════════════════════════
# Reads & ownership
# ════════════════════════════════════════════════════════════════════


class TestReads:
    def test_list_for_user_only_own_newest_first(self, env):
        first = _create(title="الأولى")
        second = _create(title="الثانية")
        items = store.list_for_user(OWNER)
        assert [r.request_id for r in items] == [
            second.request_id, first.request_id
        ]
        assert store.list_for_user(9999) == []

    def test_list_pending_oldest_first_and_count(self, env):
        first = _create()
        second = _create()
        pending = store.list_pending()
        assert [r.request_id for r in pending] == [
            first.request_id, second.request_id
        ]
        assert store.count_pending() == 2
        store.admin_reject_request(second.request_id, ADMIN, "لا")
        assert store.count_pending() == 1

    def test_get_request_bad_ids_are_none(self, env):
        _create()
        assert store.get_request(None) is None
        assert store.get_request(True) is None
        assert store.get_request("1") is None
        assert store.get_request(999999) is None


# ════════════════════════════════════════════════════════════════════
# User transition: resubmit
# ════════════════════════════════════════════════════════════════════


class TestResubmit:
    def test_resubmit_requires_changes_requested(self, env):
        request = _create()
        with pytest.raises(TaskRequestError):
            store.resubmit_request(
                request.request_id, OWNER, _payload(title="جديد")
            )
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING

    def test_resubmit_requires_owner(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN, "عدّل")
        with pytest.raises(TaskRequestError):
            store.resubmit_request(
                request.request_id, 1234, _payload(title="جديد")
            )
        assert store.get_request(request.request_id).status == \
            store.STATUS_CHANGES_REQUESTED

    def test_resubmit_missing_request_raises(self, env):
        with pytest.raises(TaskRequestError):
            store.resubmit_request(999999, OWNER, _payload())

    def test_resubmit_success_updates_payload_and_clears_reason(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN, "عدّل الوصف")
        updated = store.resubmit_request(
            request.request_id, OWNER, _payload(title="عنوان جديد")
        )
        assert updated.status == store.STATUS_PENDING
        assert updated.payload["title"] == "عنوان جديد"
        assert updated.decision_reason is None
        assert updated.decided_by is None
        assert [e["event"] for e in updated.history][-2:] == [
            "resubmitted", "submitted",
        ]

    def test_invalid_resubmit_changes_nothing(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN, "ملاحظة")
        with pytest.raises(TaskRequestError):
            store.resubmit_request(
                request.request_id, OWNER, _payload(title="")
            )
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_CHANGES_REQUESTED
        assert stored.payload["title"] == VALID["title"]


# ════════════════════════════════════════════════════════════════════
# Admin transitions: edit / return / reject
# ════════════════════════════════════════════════════════════════════


class TestAdminTransitions:
    def test_edit_only_when_pending(self, env):
        request = _create()
        store.admin_reject_request(request.request_id, ADMIN, "لا يصلح")
        with pytest.raises(TaskRequestError):
            store.admin_edit_field(
                request.request_id, ADMIN, "title", "تعديل"
            )

    def test_edit_updates_payload_and_audits_previous(self, env):
        request = _create()
        updated = store.admin_edit_field(
            request.request_id, ADMIN, "title", "عنوان الإدارة"
        )
        assert updated.payload["title"] == "عنوان الإدارة"
        event = updated.history[-1]
        assert event["event"] == "admin_edit"
        assert event["actor"] == ADMIN
        assert event["field"] == "title"
        assert event["previous_payload"]["title"] == VALID["title"]

    def test_edit_reward_revalidates_exactly(self, env):
        request = _create()
        updated = store.admin_edit_field(
            request.request_id, ADMIN, "reward", "1.25"
        )
        assert updated.reward_units == 125_000_000
        with pytest.raises(TaskRequestError):
            store.admin_edit_field(
                request.request_id, ADMIN, "reward", "abc"
            )
        # Failed edit changed nothing.
        assert store.get_request(request.request_id).reward_units == \
            125_000_000

    def test_edit_unknown_field_rejected(self, env):
        request = _create()
        with pytest.raises(TaskRequestError):
            store.admin_edit_field(
                request.request_id, ADMIN, "provider", "tiktok"
            )

    def test_return_moves_to_changes_requested(self, env):
        request = _create()
        returned = store.admin_return_request(
            request.request_id, ADMIN, "عدّل الوصف"
        )
        assert returned.status == store.STATUS_CHANGES_REQUESTED
        assert returned.decision_reason == "عدّل الوصف"
        assert returned.decided_by == ADMIN
        assert returned.history[-1]["event"] == "returned"

    def test_reject_is_terminal_with_reason(self, env):
        request = _create()
        rejected = store.admin_reject_request(
            request.request_id, ADMIN, "المحتوى غير مناسب"
        )
        assert rejected.status == store.STATUS_REJECTED
        assert rejected.decision_reason == "المحتوى غير مناسب"
        assert rejected.history[-1]["event"] == "rejected"
        # Terminal: no edit, no return, no second reject.
        for op in (
            lambda: store.admin_edit_field(
                request.request_id, ADMIN, "title", "x"
            ),
            lambda: store.admin_return_request(
                request.request_id, ADMIN, "م"
            ),
            lambda: store.admin_reject_request(
                request.request_id, ADMIN, "م"
            ),
        ):
            with pytest.raises(TaskRequestError):
                op()

    def test_reject_missing_request_raises(self, env):
        with pytest.raises(TaskRequestError):
            store.admin_reject_request(999999, ADMIN, "سبب")

    def test_reason_bounded(self, env):
        request = _create()
        rejected = store.admin_reject_request(
            request.request_id, ADMIN, "ر" * 2000
        )
        assert len(rejected.decision_reason) <= store.MAX_REASON_LENGTH


# ════════════════════════════════════════════════════════════════════
# Approval → the existing task lifecycle
# ════════════════════════════════════════════════════════════════════


def _approve(request_id: int, admin_id: int = ADMIN) -> int:
    """The SAME orchestration task_request_admin performs, inline."""
    with db.transaction() as conn:
        claimed = store.claim_for_approval(conn, request_id, admin_id)
        assert claimed is not None
        spec = store.spec_from_request(claimed, approver_id=admin_id)
        task_id = create_task_from_spec(
            spec, conn=conn, funding_advertiser_id=None
        )
        store.mark_approved(conn, request_id, admin_id, task_id)
    return task_id


class TestApproval:
    def test_claim_is_single_winner(self, env):
        request = _create()
        with db.transaction() as conn:
            first = store.claim_for_approval(
                conn, request.request_id, ADMIN
            )
            second = store.claim_for_approval(
                conn, request.request_id, ADMIN
            )
        assert first is not None
        assert second is None  # duplicate approval blocked at the CAS

    def test_claim_missing_or_decided_is_none(self, env):
        with db.transaction() as conn:
            assert store.claim_for_approval(conn, 999999, ADMIN) is None
        request = _create()
        store.admin_reject_request(request.request_id, ADMIN, "لا")
        with db.transaction() as conn:
            assert store.claim_for_approval(
                conn, request.request_id, ADMIN
            ) is None

    def test_approve_publishes_active_manual_task(self, env):
        request = _create()
        task_id = _approve(request.request_id)

        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_APPROVED
        assert stored.published_task_id == task_id
        assert stored.decided_by == ADMIN
        assert stored.history[-1]["event"] == "approved"

        task = db.get_task(task_id)
        assert task["active"] == 1
        assert task["type"] == "manual"
        assert task["title"] == VALID["title"]
        assert task["reward_units"] == 50_000_000
        task_data = json.loads(task["task_data"])
        assert task_data["provider"] == "instagram"
        assert task_data["action"] == "follow"
        assert task_data["approver"]["telegram_user_id"] == ADMIN
        assert task_data["target"]["ref"] == VALID["target_ref"]

    def test_approved_task_visible_to_existing_catalog(self, env):
        from task_catalog import TaskCatalog

        request = _create()
        assert TaskCatalog().list_available_tasks() == []
        task_id = _approve(request.request_id)
        summaries = TaskCatalog().list_available_tasks()
        assert [s.id for s in summaries] == [task_id]

    def test_approved_task_lifecycle_still_works(self, env):
        from task_lifecycle import TaskLifecycle

        request = _create()
        task_id = _approve(request.request_id)
        result = TaskLifecycle().start_task(OWNER, task_id)
        assert result.success
        assert result.status == "started"

    def test_duplicate_approve_creates_exactly_one_task(self, env):
        request = _create()
        task_id = _approve(request.request_id)
        # Replay attempt: the CAS refuses, no second task exists.
        with db.transaction() as conn:
            assert store.claim_for_approval(
                conn, request.request_id, ADMIN
            ) is None
            assert store.read_published_task_id(
                conn, request.request_id
            ) == task_id
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
        assert count == 1

    def test_rejected_request_never_creates_a_task(self, env):
        request = _create()
        store.admin_reject_request(request.request_id, ADMIN, "لا يصلح")
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
        assert count == 0
        # And it can never be claimed afterwards.
        with db.transaction() as conn:
            assert store.claim_for_approval(
                conn, request.request_id, ADMIN
            ) is None

    def test_failed_creation_rolls_back_claim(self, env):
        """A spec that cannot become a task leaves the request pending
        (zero tasks, zero claims): the failure must escape the
        transaction so the claim CAS rolls back with it."""
        request = _create()
        from dataclasses import replace

        with pytest.raises(TaskCreationError):
            with db.transaction() as conn:
                claimed = store.claim_for_approval(
                    conn, request.request_id, ADMIN
                )
                assert claimed is not None
                spec = store.spec_from_request(
                    claimed, approver_id=ADMIN
                )
                # Force a creation failure via an invalid approver.
                spec = replace(spec, approver_id=0)
                create_task_from_spec(spec, conn=conn)
        # Transaction rolled back → pending again, no tasks.
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
        assert count == 0

    def test_spec_uses_manual_contract(self, env):
        request = _create()
        spec = store.spec_from_request(request, approver_id=ADMIN)
        assert spec.verification == "manual"
        assert spec.approver_id == ADMIN
        assert spec.action in MANUAL_TASK_ACTIONS
        assert spec.reward == 0
        assert spec.reward_units == 50_000_000
        assert spec.target == {"ref": VALID["target_ref"]}

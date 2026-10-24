# Owner task management tests (Phase 18)
import json
import sqlite3

import pytest

import db
import wallet
from wallet import USDT_SCALE
from test_task_routes import _auth, _create_owned_task, USER_A, USER_B
from task_creation import TaskSpec, create_task_from_spec


OWNER_SEED = 1000 * USDT_SCALE


class TestOwnerTaskManagement:
    def test_owner_can_edit_title_description_reward(self, client):
        tid = _create_owned_task()
        wallet.credit_units(USER_A, 5 * USDT_SCALE)
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'title': 'عنوان جديد', 'description': 'وصف جديد', 'reward': 2},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        task = data['task']
        assert task['title'] == 'عنوان جديد'
        assert task['description'] == 'وصف جديد'
        assert task['reward'] == 2

    def test_non_owner_cannot_edit(self, client):
        tid = _create_owned_task()
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_B),
            json={'title': 'hacked'},
        )
        assert resp.status_code == 403
        assert resp.get_json()['error'] == 'not_owner'

    def test_non_owner_cannot_pause(self, client):
        tid = _create_owned_task()
        resp = client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_B))
        assert resp.status_code == 403

    def test_non_owner_cannot_resume(self, client):
        tid = _create_owned_task()
        client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        resp = client.post(f'/api/tasks/{tid}/resume', headers=_auth(USER_B))
        assert resp.status_code == 403

    def test_non_owner_cannot_archive(self, client):
        tid = _create_owned_task()
        resp = client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_B),
            json={'confirm': True},
        )
        assert resp.status_code == 403

    def test_pause_resume_lifecycle(self, client):
        tid = _create_owned_task()
        r = client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        assert r.status_code == 200
        assert r.get_json()['paused'] is True
        r2 = client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        assert r2.status_code == 409
        assert r2.get_json()['error'] == 'already_paused'
        r3 = client.post(f'/api/tasks/{tid}/resume', headers=_auth(USER_A))
        assert r3.status_code == 200
        assert r3.get_json()['paused'] is False
        r4 = client.post(f'/api/tasks/{tid}/resume', headers=_auth(USER_A))
        assert r4.status_code == 409
        assert r4.get_json()['error'] == 'not_paused'

    def test_archive_lifecycle(self, client):
        tid = _create_owned_task()
        r = client.post(f'/api/tasks/{tid}/archive', headers=_auth(USER_A), json={})
        assert r.status_code == 400
        assert r.get_json()['error'] == 'confirmation_required'
        r2 = client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        assert r2.status_code == 200
        assert r2.get_json()['archived'] is True
        r3 = client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        assert r3.status_code == 409
        assert r3.get_json()['error'] == 'already_archived'

    def test_archived_task_rejects_edit(self, client):
        tid = _create_owned_task()
        client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'title': 'nope'},
        )
        assert resp.status_code in (404, 409, 400)

    def test_archived_task_rejects_pause(self, client):
        tid = _create_owned_task()
        client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        resp = client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        assert resp.status_code in (404, 409)

    def test_archived_excluded_from_catalog(self, client):
        tid = _create_owned_task()
        client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        resp = client.get('/api/tasks', headers=_auth(USER_A))
        ids = [t['id'] for t in resp.get_json()['tasks']]
        assert tid not in ids

    def test_rejected_edit_does_not_alter_record(self, client):
        tid = _create_owned_task()
        before = db.get_task(tid)
        client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_B),
            json={'title': 'hacked', 'reward': 999},
        )
        after = db.get_task(tid)
        assert after['title'] == before['title']
        assert after['reward'] == before['reward']

    def test_unauthenticated_rejected(self, client):
        tid = _create_owned_task()
        r = client.patch(f'/api/tasks/{tid}/owner', json={'title': 'x'})
        assert r.status_code == 401
        r = client.post(f'/api/tasks/{tid}/pause')
        assert r.status_code == 401
        r = client.post(f'/api/tasks/{tid}/resume')
        assert r.status_code == 401
        r = client.post(f'/api/tasks/{tid}/archive', json={'confirm': True})
        assert r.status_code == 401

    def test_reward_increase_requires_balance(self, client):
        tid = _create_owned_task()
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'reward': 100000},
        )
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'balance_insufficient'

    def test_reward_increase_no_partial_write(self, client):
        tid = _create_owned_task()
        before = db.get_task(tid)
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'title': 'new title', 'reward': 100000},
        )
        assert resp.status_code == 400
        after = db.get_task(tid)
        assert after['title'] == before['title']
        assert after['reward'] == before['reward']

    def test_decreasing_reward_no_refund(self, client):
        tid = _create_owned_task()
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'reward': 0},
        )
        assert resp.status_code == 200
        after = db.get_task(tid)
        assert after['reward'] == 0

    def test_paused_task_flag(self, client):
        tid = _create_owned_task()
        client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        t = db.get_task(tid)
        assert bool(t['paused']) is True

    def test_edit_empty_title_rejected(self, client):
        tid = _create_owned_task()
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'title': '   '},
        )
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'invalid_payload'

    def test_edit_invalid_reward_rejected(self, client):
        tid = _create_owned_task()
        resp = client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_A),
            json={'reward': 'abc'},
        )
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'invalid_payload'

    def test_archived_task_rejects_resume(self, client):
        tid = _create_owned_task()
        client.post(f'/api/tasks/{tid}/pause', headers=_auth(USER_A))
        client.post(
            f'/api/tasks/{tid}/archive',
            headers=_auth(USER_A),
            json={'confirm': True},
        )
        resp = client.post(f'/api/tasks/{tid}/resume', headers=_auth(USER_A))
        assert resp.status_code in (404, 409)

    def test_edit_does_not_change_balances_or_ledger_on_reject(self, client):
        tid = _create_owned_task()
        # snapshot ledger count
        with db.get_connection() as conn:
            before_count = conn.execute(
                'SELECT COUNT(*) AS c FROM ledger'
            ).fetchone()['c']
        client.patch(
            f'/api/tasks/{tid}/owner',
            headers=_auth(USER_B),
            json={'title': 'hacked', 'reward': 999},
        )
        with db.get_connection() as conn:
            after_count = conn.execute(
                'SELECT COUNT(*) AS c FROM ledger'
            ).fetchone()['c']
        assert after_count == before_count

from test_task_routes import _Members, USER_A, USER_B, CHANNELS, Channel, CHANNEL_SLUG, CHANNEL_ID, CHANNEL_USERNAME, _TEST_BOT_TOKEN, _make_init_data
import os


@pytest.fixture
def members():
    return _Members()


@pytest.fixture
def env(monkeypatch, tmp_path, members):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', _TEST_BOT_TOKEN)
    db_path = str(tmp_path / 'owner_task_test.db')
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    db.init_db(db_path)
    db.register_user(USER_A, 'alice', 'Alice')
    db.register_user(USER_B, 'bob', 'Bob')
    CHANNELS.clear()
    CHANNELS[CHANNEL_SLUG] = Channel(
        slug=CHANNEL_SLUG,
        channel_id=CHANNEL_ID,
        username=CHANNEL_USERNAME,
        title='TaskCoin',
        required=True,
    )
    from channel_task_verifier import ChannelTaskVerifier, register_channel_task_verifier
    register_channel_task_verifier(
        ChannelTaskVerifier(membership_checker=members)
    )
    yield db_path
    CHANNELS.clear()
    register_channel_task_verifier()


@pytest.fixture
def client(env):
    import serve_miniapp
    serve_miniapp.app.config['TESTING'] = True
    return serve_miniapp.app.test_client()

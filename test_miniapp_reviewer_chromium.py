"""Real-browser regression coverage for the Account reviewer entry.

Run with:
    python3 -m pytest test_miniapp_reviewer_chromium.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

import pytest


ROOT = "miniapp"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as source:
        return source.read()


def _head_version(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _webdriver(port: int, path: str, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@pytest.fixture(scope="module")
def chromium():
    if os.environ.get("RUN_CHROMIUM_TESTS") != "1":
        pytest.skip("set RUN_CHROMIUM_TESTS=1 to run the real-browser suite")

    driver = shutil.which("chromedriver")
    if not driver or not shutil.which("chromium"):
        pytest.skip("Chromium and ChromeDriver are required for this regression test")

    with socket.socket() as reserved_port:
        reserved_port.bind(("127.0.0.1", 0))
        port = reserved_port.getsockname()[1]
    process = subprocess.Popen(
        [driver, f"--port={port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                _webdriver(port, "/status")
                break
            except URLError:
                time.sleep(0.1)
        else:
            pytest.fail("ChromeDriver did not start")

        session = _webdriver(
            port,
            "/session",
            "POST",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "chrome",
                        "goog:chromeOptions": {
                            "args": [
                                "--headless=new",
                                "--no-sandbox",
                                "--disable-gpu",
                                "--disable-dev-shm-usage",
                                "--no-first-run",
                            ]
                        },
                    }
                }
            },
        )["value"]["sessionId"]
        yield port, session
    finally:
        try:
            _webdriver(port, f"/session/{locals().get('session', '')}", "DELETE")
        except (URLError, KeyError):
            pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _profile_template() -> str:
    html = _read(f"{ROOT}/index.html")
    begin = html.index('<template id="page-profile">')
    end = html.index("</template>", begin) + len("</template>")
    return html[begin:end]


def _document(user=None) -> bytes:
    user_json = json.dumps(user) if user is not None else "null"
    return f"""<!doctype html>
<html><head><link rel="stylesheet" href="/css/app.css"></head><body>
<main id="app-content"></main>
{_profile_template()}
<script>
const TelegramApp = {{ init: () => true, getUser: () => ({user_json}) }};
const Header = {{ init: () => {{}} }};
const Navigation = {{ init: () => {{}} }};
</script>
<script src="/js/wallet-data.js"></script>
<script src="/js/review.js"></script>
<script src="/js/profile.js"></script>
<script src="/js/app.js"></script>
<script>
document.addEventListener('DOMContentLoaded', () => {{
    App.renderPage('profile');
    setTimeout(() => {{
        const entry = document.querySelector('[data-testid="review-entry"]');
        document.body.dataset.entry = entry
            ? `${{entry.hidden}}:${{getComputedStyle(entry).display}}:${{entry.style.getPropertyValue('display')}}:${{entry.style.getPropertyPriority('display')}}`
            : 'removed';
    }}, 300);
    setTimeout(() => {{
        const acc = (t) => document.querySelector(
            `[data-testid="${{t}}"]`);
        document.body.dataset.account = JSON.stringify({{
            balance: acc('profile-balance')
                ? acc('profile-balance').textContent : null,
            earnings: acc('profile-earnings')
                ? acc('profile-earnings').textContent : null,
            completed: acc('profile-completed')
                ? acc('profile-completed').textContent : null,
            progress: acc('profile-progress')
                ? acc('profile-progress').textContent : null,
        }});
    }}, 700);
}});
</script>
</body></html>""".encode()


def _serve(scenario: str, account: str = "ok"):
    current = {
        "/js/app.js": _read(f"{ROOT}/js/app.js"),
        "/js/review.js": _read(f"{ROOT}/js/review.js"),
        "/js/profile.js": _read(f"{ROOT}/js/profile.js"),
        "/js/wallet-data.js": _read(f"{ROOT}/js/wallet-data.js"),
        "/css/app.css": _read(f"{ROOT}/css/app.css"),
    }
    old = {
        "/js/review.js": _head_version(f"{ROOT}/js/review.js"),
        "/css/app.css": _head_version(f"{ROOT}/css/app.css"),
    }
    assets = current if scenario != "mix-a" else {**current, **old}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                query = parse_qs(urlparse(self.path).query)
                user = (json.loads(query["user"][0])
                        if "user" in query else None)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_document(user))
                return

            if self.path == "/api/me":
                if account == "ok":
                    body = {
                        "ok": True,
                        "user": {
                            "id": 987654321,
                            "username": "sara_ahmad",
                            "firstName": "سارة",
                        },
                        "wallet": {
                            "availableUnits": 12_500_000_000,
                        },
                        "stats": {
                            "completedTasks": 3,
                            "inProgressTasks": 1,
                            "earnedUnits": 12_500_000_000,
                        },
                    }
                    self.send_response(200)
                elif account == "error":
                    body = {
                        "ok": False,
                        "error": "server_error",
                        "message": "حدث خطأ غير متوقع، حاول مرة أخرى",
                    }
                    self.send_response(500)
                elif account == "malformed":
                    # ok:true but every figure is missing —
                    # the page must keep its placeholders.
                    body = {"ok": True, "wallet": {}, "stats": {}}
                    self.send_response(200)
                else:  # "missing" — no such endpoint at all
                    self.send_error(404)
                    return
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
                return

            if self.path == "/api/tasks":
                body = {"ok": True, "tasks": [{"id": 1, "type": "manual"}]}
            elif self.path == "/api/tasks/1/claims":
                body = {"ok": scenario == "reviewer", "claims": []}
                if scenario != "reviewer":
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode())
                    return
            elif self.path in assets:
                self.send_response(200)
                self.send_header("Content-Type", "text/css" if self.path.endswith(".css") else "application/javascript")
                self.end_headers()
                self.wfile.write(assets[self.path].encode())
                return
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("mix-a", "true:none:none:important"),
        ("reviewer", "false:flex::"),
        ("non-reviewer", "true:none:none:important"),
    ],
)
def test_account_entry_visibility_in_real_chromium(chromium, scenario: str, expected: str):
    """MIX A must fail closed even with the exact old CSS and review.js."""
    server = _serve(scenario)
    try:
        port, session = chromium
        _webdriver(
            port,
            f"/session/{session}/url",
            "POST",
            {"url": f"http://127.0.0.1:{server.server_port}/"},
        )
        time.sleep(0.5)
        status = _webdriver(
            port,
            f"/session/{session}/execute/sync",
            "POST",
            {
                "script": """
                    const entry = document.querySelector('[data-testid="review-entry"]');
                    return entry
                        ? `${entry.hidden}:${getComputedStyle(entry).display}:${entry.style.getPropertyValue('display')}:${entry.style.getPropertyPriority('display')}`
                        : 'removed';
                """,
                "args": [],
            },
        )["value"]
    finally:
        server.shutdown()
        server.server_close()

    assert status == expected


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (
            {"id": 987654321, "first_name": "سارة",
             "last_name": "الأحمد", "username": "sara_ahmad",
             "photo_url": "https://example.com/sara.jpg"},
            {"name": "سارة الأحمد", "username": "@sara_ahmad",
             "avatar": "https://example.com/sara.jpg",
             "placeholder": False, "id": "987654321"},
        ),
        (
            {"id": 123456, "first_name": "عمر"},
            {"name": "عمر", "username": None, "avatar": None,
             "placeholder": True, "id": "123456"},
        ),
        (
            {"id": 42, "first_name": "ليلى", "username": "layla",
             "photo_url": "https://example.com/layla.png"},
            {"name": "ليلى", "username": "@layla",
             "avatar": "https://example.com/layla.png",
             "placeholder": False, "id": "42"},
        ),
        (
            {"id": 7, "first_name": "خالد", "last_name": "علي"},
            {"name": "خالد علي", "username": None, "avatar": None,
             "placeholder": True, "id": "7"},
        ),
    ],
)
def test_profile_identity_in_real_chromium(chromium, user, expected):
    """The Account page shows the caller's own Telegram identity:
    name (first + last), username only when provided, the photo
    only when served over https, and the Telegram id."""
    server = _serve("non-reviewer")
    try:
        port, session = chromium
        _webdriver(
            port,
            f"/session/{session}/url",
            "POST",
            {"url": (
                f"http://127.0.0.1:{server.server_port}/"
                f"?user={quote(json.dumps(user))}"
            )},
        )
        time.sleep(0.5)
        status = _webdriver(
            port,
            f"/session/{session}/execute/sync",
            "POST",
            {
                "script": """
                    const el = (t) => document.querySelector(
                        `[data-testid="${t}"]`);
                    const avatar = el('profile-avatar');
                    const username = el('profile-username');
                    const img = avatar && avatar.querySelector('img');
                    return JSON.stringify({
                        name: el('profile-name')
                            ? el('profile-name').textContent : null,
                        username: username && !username.hidden
                            ? username.textContent : null,
                        avatar: img ? img.getAttribute('src') : null,
                        placeholder: !!(avatar
                            && avatar.querySelector('.avatar-placeholder')),
                        id: el('profile-id')
                            ? el('profile-id').textContent : null,
                    });
                """,
                "args": [],
            },
        )["value"]
    finally:
        server.shutdown()
        server.server_close()

    assert json.loads(status) == expected


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        (
            "ok",
            {
                "balance": "125.00000000 USDT",
                "earnings": "125.00000000 USDT",
                "completed": "3",
                "progress": "1",
            },
        ),
        (
            "error",
            {"balance": "—", "earnings": "—",
             "completed": "—", "progress": "—"},
        ),
        (
            "malformed",
            {"balance": "—", "earnings": "—",
             "completed": "—", "progress": "—"},
        ),
        (
            "missing",
            {"balance": "—", "earnings": "—",
             "completed": "—", "progress": "—"},
        ),
    ],
)
def test_profile_account_data_in_real_chromium(chromium, account, expected):
    """The Account page renders the real figures GET /api/me
    confirms — exact 8-decimal USDT formatting for money,
    plain counts for tasks — and keeps every neutral
    placeholder when the read errored, came back malformed
    or the endpoint is missing entirely."""
    server = _serve("non-reviewer", account=account)
    try:
        port, session = chromium
        _webdriver(
            port,
            f"/session/{session}/url",
            "POST",
            {"url": f"http://127.0.0.1:{server.server_port}/"},
        )
        time.sleep(1.0)
        status = _webdriver(
            port,
            f"/session/{session}/execute/sync",
            "POST",
            {
                "script": "return document.body.dataset.account;",
                "args": [],
            },
        )["value"]
    finally:
        server.shutdown()
        server.server_close()

    assert json.loads(status) == expected

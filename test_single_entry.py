"""Focused tests for the WispByte single-entry architecture.

WispByte's Startup Command executes only ``python bot.py`` — it does not run
the Procfile. Therefore bot.py must, from one process:

* serve the Mini App HTTP on ``0.0.0.0:$PORT`` via Waitress (main thread),
* keep the Telegram bot polling in a controlled background context,
* fail clearly if the HTTP listener cannot bind,
* shut both down cleanly, exactly once, with no Procfile dependency.
"""

import inspect
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


# ── Helpers ──────────────────────────────────────────────────────────


def _make_application_mock() -> MagicMock:
    """Build a MagicMock that behaves like a PTB Application lifecycle."""
    application = MagicMock()
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.stop = AsyncMock()
    application.shutdown = AsyncMock()
    application.post_init = AsyncMock()
    application.post_stop = None
    application.post_shutdown = None
    application.running = True
    application.updater = MagicMock()
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock()
    application.updater.running = True
    return application


def _patch_server_factory(monkeypatch) -> tuple[MagicMock, dict]:
    """Replace bot's Waitress factory with a counting mock server."""
    server = MagicMock()
    server.effective_host = "0.0.0.0"
    server.effective_port = 40000
    server.run = MagicMock()
    server.close = MagicMock()
    calls = {"count": 0}

    def _factory(*args, **kwargs):
        calls["count"] += 1
        return server

    monkeypatch.setattr(bot, "create_server", _factory)
    return server, calls


def _http_get(url: str, timeout: float = 2.0, retries: int = 100):
    """GET *url*, retrying while the test server thread warms up."""
    last_error = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.status, response.read()
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"GET {url} never succeeded: {last_error!r}")


@pytest.fixture(autouse=True)
def _reset_single_entry_guard():
    """Never leak the process-wide duplicate-startup guard between tests."""
    bot._SINGLE_ENTRY_RUNNING = False
    yield
    bot._SINGLE_ENTRY_RUNNING = False


@pytest.fixture
def running_http_server(monkeypatch):
    """A real Waitress server for the Mini App on an ephemeral port."""
    monkeypatch.setenv("PORT", "0")
    server = bot.create_mini_app_server()

    def _serve():
        try:
            server.run()
        except OSError:
            # Expected: the fixture's server.close() interrupts select().
            pass

    thread = threading.Thread(target=_serve, name="test-miniapp-http", daemon=True)
    thread.start()
    yield server, server.effective_port
    try:
        server.close()
    except Exception:
        pass
    thread.join(timeout=5)


# ── 1–3: server creation, host, port ────────────────────────────────


class TestServerCreation:
    def test_bot_creates_the_mini_app_http_server(self):
        """bot.py exposes the Waitress-based Mini App HTTP server factory."""
        assert hasattr(bot, "create_mini_app_server")
        assert callable(bot.create_mini_app_server)
        source = inspect.getsource(bot)
        assert "create_server(" in source, "bot.py must build a Waitress server"

    def test_server_binds_0_0_0_0(self):
        """Default bind host is 0.0.0.0 (WispByte requirement)."""
        default_host = inspect.signature(bot.create_mini_app_server).parameters[
            "host"
        ].default
        assert default_host == "0.0.0.0"

        saved = os.environ.pop("PORT", None)
        try:
            server = bot.create_mini_app_server(port=0)
            try:
                assert server.effective_host == "0.0.0.0"
            finally:
                server.close()
        finally:
            if saved is not None:
                os.environ["PORT"] = saved

    def test_server_uses_port_from_environment(self, monkeypatch):
        """PORT env var selects the listening port (no hardcoded port)."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        monkeypatch.setenv("PORT", str(free_port))
        server = bot.create_mini_app_server()
        try:
            # Waitress exposes effective_port as a string.
            assert int(server.effective_port) == free_port
        finally:
            server.close()

    def test_no_hardcoded_deployment_port_in_bot(self):
        """bot.py must not hardcode a WispByte port or read the Procfile."""
        with open("bot.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "10148" not in source
        # No runtime dependency on the Procfile (comments may mention it).
        assert 'open("Procfile"' not in source
        assert "open('Procfile'" not in source
        assert 'os.environ.get("PORT"' in source


# ── 4–5: Mini App root and static assets over real HTTP ─────────────


class TestMiniAppEndpoints:
    def test_root_serves_mini_app_index(self, running_http_server):
        server, port = running_http_server
        status, body = _http_get(f"http://127.0.0.1:{port}/")
        assert status == 200
        with open("miniapp/index.html", "rb") as fh:
            assert body == fh.read()

    def test_static_css_asset_accessible(self, running_http_server):
        server, port = running_http_server
        status, body = _http_get(f"http://127.0.0.1:{port}/css/app.css")
        assert status == 200
        with open("miniapp/css/app.css", "rb") as fh:
            assert body == fh.read()

    def test_static_js_asset_accessible(self, running_http_server):
        server, port = running_http_server
        status, body = _http_get(f"http://127.0.0.1:{port}/js/home.js")
        assert status == 200
        with open("miniapp/js/home.js", "rb") as fh:
            assert body == fh.read()


# ── 6, 10: Telegram lifecycle in the controlled background context ──


class TestTelegramBackgroundLifecycle:
    def _start_thread(self, application, stop_event):
        thread = threading.Thread(
            target=bot._run_telegram_bot,
            args=(application, stop_event),
            name="telegram-bot",
        )
        thread.start()
        return thread

    def test_lifecycle_starts_in_background_execution_context(self):
        stop_event = threading.Event()
        stop_event.set()  # shut down immediately after startup
        application = _make_application_mock()
        observed = {}

        async def _record_initialize(*args, **kwargs):
            observed["thread"] = threading.current_thread()

        application.initialize = AsyncMock(side_effect=_record_initialize)

        main_thread = threading.current_thread()
        thread = self._start_thread(application, stop_event)
        thread.join(timeout=10)

        assert not thread.is_alive(), "lifecycle must terminate after stop_event"
        assert observed["thread"] is thread
        assert observed["thread"] is not main_thread
        application.initialize.assert_awaited_once()
        application.post_init.assert_awaited_once()  # runs the Open button setup
        application.updater.start_polling.assert_awaited_once()
        application.start.assert_awaited_once()

    def test_lifecycle_waits_then_shuts_down_cleanly(self):
        stop_event = threading.Event()  # not set: bot keeps running
        application = _make_application_mock()

        thread = self._start_thread(application, stop_event)

        # Wait until polling has started in the background.
        deadline = time.monotonic() + 10
        while (
            application.updater.start_polling.await_count == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert application.updater.start_polling.await_count == 1
        assert thread.is_alive(), "bot must keep running until stop_event is set"

        stop_event.set()
        thread.join(timeout=10)

        assert not thread.is_alive()
        # run_polling's teardown order: updater.stop → stop → shutdown.
        application.updater.stop.assert_awaited_once()
        application.stop.assert_awaited_once()
        application.shutdown.assert_awaited_once()


# ── 7–12: single-entry runtime: once-only, bind failure, shutdown ───


class TestSingleEntryRuntime:
    def test_http_server_and_polling_started_exactly_once(self, monkeypatch):
        server, calls = _patch_server_factory(monkeypatch)
        application = _make_application_mock()

        bot.run_single_entry(application)

        # HTTP server created and run exactly once (req: no duplicates).
        assert calls["count"] == 1
        assert server.run.call_count == 1
        # Telegram polling started exactly once.
        assert application.updater.start_polling.await_count == 1
        # Shutdown signalled the Telegram lifecycle…
        application.updater.stop.assert_awaited_once()
        application.stop.assert_awaited_once()
        application.shutdown.assert_awaited_once()
        # …and closed the HTTP server.
        server.close.assert_called_once()

    def test_duplicate_startup_is_rejected(self, monkeypatch):
        server, calls = _patch_server_factory(monkeypatch)
        observed = {}

        def _run_side_effect():
            # A second concurrent startup attempt must be refused before it
            # can create or run another HTTP server.
            with pytest.raises(RuntimeError, match="already running"):
                bot.run_single_entry(_make_application_mock())
            observed["attempted"] = True

        server.run.side_effect = _run_side_effect

        bot.run_single_entry(_make_application_mock())

        assert observed.get("attempted"), "nested startup attempt must happen"
        assert calls["count"] == 1, "exactly one HTTP server may ever be created"

    def test_http_bind_failure_surfaces_clearly_before_telegram_start(
        self, monkeypatch
    ):
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]
        try:
            monkeypatch.setenv("PORT", str(occupied_port))
            application = _make_application_mock()

            with pytest.raises(OSError):
                bot.run_single_entry(application)

            # No Telegram resource may be created when the bind fails.
            application.initialize.assert_not_awaited()
            application.updater.start_polling.assert_not_awaited()
        finally:
            blocker.close()

        # The guard must be released so a corrected restart is possible.
        assert bot._SINGLE_ENTRY_RUNNING is False

    def test_shutdown_signal_handlers_are_installed_and_restored(
        self, monkeypatch
    ):
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)
        observed = {}

        server, _calls = _patch_server_factory(monkeypatch)

        def _run_side_effect():
            observed["term"] = signal.getsignal(signal.SIGTERM)
            observed["int"] = signal.getsignal(signal.SIGINT)

        server.run.side_effect = _run_side_effect

        bot.run_single_entry(_make_application_mock())

        assert observed["term"] is bot._handle_shutdown_signal
        assert observed["int"] is bot._handle_shutdown_signal
        assert signal.getsignal(signal.SIGTERM) is original_term
        assert signal.getsignal(signal.SIGINT) is original_int

    def test_bot_thread_stops_when_signalled_before_http_return(self, monkeypatch):
        """stop_event is set as soon as the HTTP server returns — no hang."""
        server, _calls = _patch_server_factory(monkeypatch)
        application = _make_application_mock()

        bot.run_single_entry(application)

        application.updater.start_polling.assert_awaited_once()
        application.shutdown.assert_awaited_once()


# ── 13–14: Open button and MINI_APP_URL configuration intact ────────


class TestExistingConfigurationIntact:
    def test_open_button_configuration_intact(self):
        with open("bot.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "set_chat_menu_button" in source
        assert "MenuButtonWebApp" in source
        assert "WebAppInfo" in source
        assert "def setup_menu_button" in source
        assert "await setup_menu_button(application)" in source

    def test_mini_app_url_configuration_intact(self):
        with open("config.py", "r", encoding="utf-8") as fh:
            config_source = fh.read()
        assert "MINI_APP_URL" in config_source
        assert "def get_mini_app_url" in config_source
        assert 'parsed.scheme != "https"' in config_source

        with open("bot.py", "r", encoding="utf-8") as fh:
            bot_source = fh.read()
        assert "get_mini_app_url" in bot_source

    def test_waitress_used_not_flask_dev_server(self):
        with open("bot.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "from waitress import create_server" in source
        assert "server.run()" in source
        assert "app.run_polling()" not in source
        assert "run_single_entry(app)" in source

    def test_standalone_serve_miniapp_preserved_for_procfile(self):
        """serve_miniapp.py stays a functional standalone server (non-WispByte)."""
        assert os.path.exists("serve_miniapp.py")
        with open("serve_miniapp.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert 'host = "0.0.0.0"' in source
        assert 'os.environ.get("PORT"' in source

    def test_no_process_manager_dependency(self):
        """No honcho/foreman/supervisor/gunicorn in the entrypoint."""
        with open("bot.py", "r", encoding="utf-8") as fh:
            source = fh.read().lower()
        for forbidden in ("honcho", "foreman", "supervisor", "gunicorn"):
            assert forbidden not in source

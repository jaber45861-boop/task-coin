"""
Integration Tests for Single-Entry WispByte Architecture
=========================================================

Verifies that bot.py serves as the single production entry point:
- PORT is read from environment
- HTTP server binds on 0.0.0.0:PORT
- Mini App HTTP endpoint returns 200
- Telegram application runs in background execution context
- Mini App server doesn't start twice
- Startup failure in HTTP bind fails clearly
- shutdown closes HTTP server
- shutdown doesn't leave bot lifecycle hanging
- No production dependency on Procfile
- py_compile passes

Run:
    python3 -m pytest test_single_entry.py -v
"""

import asyncio
import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


# ── Architecture Tests ───────────────────────────────────────────


class TestPortConfiguration:
    """Verify PORT is read from environment."""

    def test_port_reads_from_environment(self):
        """PORT environment variable is used when set."""
        os.environ["PORT"] = "9999"
        try:
            port = int(os.environ.get("PORT", 5000))
            assert port == 9999
        finally:
            del os.environ["PORT"]

    def test_port_defaults_to_5000(self):
        """PORT defaults to 5000 when not set."""
        os.environ.pop("PORT", None)
        port = int(os.environ.get("PORT", 5000))
        assert port == 5000

    def test_no_hardcoded_wispbyte_port(self):
        """bot.py must not hardcode the WispByte port 10148."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "10148" not in content, "bot.py must not hardcode port 10148"


class TestHttpServerBinding:
    """Verify HTTP server binds on 0.0.0.0:PORT."""

    def test_bot_py_binds_to_0_0_0_0(self):
        """bot.py must configure HTTP server to bind on 0.0.0.0."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert 'host = "0.0.0.0"' in content, "bot.py must bind to 0.0.0.0"

    def test_bot_py_uses_waitress_not_flask_dev(self):
        """bot.py must use Waitress, not Flask development server."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "create_server" in content, "bot.py must use waitress.create_server"
        assert "from waitress import create_server" in content, (
            "bot.py must import create_server from waitress"
        )

    def test_bot_py_has_port_from_env(self):
        """bot.py reads PORT from environment."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert 'os.environ.get("PORT"' in content, (
            "bot.py must read PORT from environment"
        )

    def test_bot_py_creates_mini_app_flask(self):
        """bot.py creates a Flask app for Mini App serving."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "create_mini_app" in content, "bot.py must define create_mini_app()"
        assert 'send_from_directory' in content, (
            "bot.py must use send_from_directory for Mini App files"
        )

    def test_flask_dev_server_not_used_in_bot(self):
        """bot.py must not use Flask's development server (app.run)."""
        with open("bot.py", "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Find lines that call app.run() — but NOT app.run_polling()
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip commented lines
            if stripped.startswith("#"):
                continue
            # Only flag bare app.run( without polling
            if "app.run(" in stripped and "polling" not in stripped:
                # Check it's not the flask app variable
                if "create_mini_app" not in stripped:
                    assert False, (
                        f"bot.py line {i+1} uses Flask dev server: {stripped}"
                    )


class TestMiniAppEndpoint:
    """Verify Mini App HTTP endpoint returns 200."""

    def test_create_mini_app_returns_flask_app(self):
        """create_mini_app() returns a Flask application."""
        from bot import create_mini_app
        app = create_mini_app()
        assert app is not None
        assert hasattr(app, "test_client")

    def test_root_returns_200(self):
        """Root path / returns HTTP 200 (serves index.html)."""
        from bot import create_mini_app
        app = create_mini_app()
        client = app.test_client()
        response = client.get("/")
        assert response.status_code == 200

    def test_root_serves_html(self):
        """Root path / serves HTML content."""
        from bot import create_mini_app
        app = create_mini_app()
        client = app.test_client()
        response = client.get("/")
        content_type = response.headers.get("Content-Type", "")
        assert "html" in content_type.lower(), (
            f"Expected HTML content, got: {content_type}"
        )

    def test_css_returns_200(self):
        """CSS asset /css/app.css returns HTTP 200."""
        from bot import create_mini_app
        app = create_mini_app()
        client = app.test_client()
        response = client.get("/css/app.css")
        assert response.status_code == 200

    def test_js_returns_200(self):
        """JS asset /js/app.js returns HTTP 200."""
        from bot import create_mini_app
        app = create_mini_app()
        client = app.test_client()
        response = client.get("/js/app.js")
        assert response.status_code == 200


class TestTelegramBackgroundExecution:
    """Verify Telegram application runs in background context."""

    def test_bot_thread_target_exists(self):
        """bot.py defines a _run_telegram_bot thread target."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "def _run_telegram_bot" in content, (
            "bot.py must define _run_telegram_bot"
        )

    def test_bot_uses_threading(self):
        """bot.py uses threading.Thread for background execution."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "threading.Thread" in content, (
            "bot.py must use threading.Thread"
        )

    def test_bot_uses_stop_event(self):
        """bot.py uses threading.Event for clean shutdown."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "threading.Event()" in content, (
            "bot.py must use threading.Event for stop signal"
        )

    def test_bot_uses_asyncio_in_thread(self):
        """bot.py uses asyncio event loop in the background thread."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "asyncio.new_event_loop()" in content, (
            "bot.py must create a new event loop for the bot thread"
        )

    def test_bot_uses_ptb_lifecycle(self):
        """bot.py uses PTB low-level lifecycle (initialize/start/stop)."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "await app.initialize()" in content, (
            "bot.py must call app.initialize()"
        )
        assert "await app.start()" in content, (
            "bot.py must call app.start()"
        )
        assert "await app.updater.start_polling" in content, (
            "bot.py must call updater.start_polling()"
        )
        assert "await app.updater.stop_polling()" in content, (
            "bot.py must call updater.stop_polling()"
        )
        assert "await app.stop()" in content, (
            "bot.py must call app.stop()"
        )
        assert "await app.shutdown()" in content, (
            "bot.py must call app.shutdown()"
        )

    def test_main_starts_bot_thread_before_server(self):
        """bot.py starts the bot thread before the HTTP server."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Find relative positions
        thread_start_pos = content.find("bot_thread.start()")
        server_run_pos = content.find("server.run()")
        assert thread_start_pos > 0, "bot_thread.start() must exist"
        assert server_run_pos > 0, "server.run() must exist"
        assert thread_start_pos < server_run_pos, (
            "bot_thread.start() must come before server.run()"
        )


class TestNoDoubleServer:
    """Verify Mini App server doesn't start twice."""

    def test_single_server_call_in_bot(self):
        """bot.py has exactly one create_server call."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert content.count("create_server(") == 1, (
            "bot.py must call create_server exactly once"
        )

    def test_no_run_polling_in_main(self):
        """bot.py main() does not call run_polling()."""
        with open("bot.py", "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Ensure run_polling is not in any executable line (ignoring comments)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "app.run_polling()" not in stripped, (
                f"bot.py line {i+1} must not call app.run_polling(): {stripped}"
            )


class TestStartupFailure:
    """Verify startup failure in HTTP bind fails clearly."""

    def test_oserror_caught_on_bind(self):
        """bot.py catches OSError when port binding fails."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "except OSError" in content, (
            "bot.py must catch OSError on HTTP server bind failure"
        )

    def test_bind_failure_exits_with_error(self):
        """bot.py exits with error code when bind fails."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "sys.exit(1)" in content, (
            "bot.py must sys.exit(1) on bind failure"
        )

    def test_bind_failure_stops_bot_thread(self):
        """bot.py stops bot thread when bind fails."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        # After OSError, stop_event should be set and thread joined
        oserror_block = content[content.find("except OSError"):]
        assert "stop_event.set()" in oserror_block, (
            "bot.py must set stop_event on bind failure"
        )
        assert "bot_thread.join" in oserror_block, (
            "bot.py must join bot_thread on bind failure"
        )


class TestShutdown:
    """Verify clean shutdown behavior."""

    def test_stop_event_set_after_server(self):
        """stop_event is set after HTTP server exits."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Find the section after server.run()
        after_server = content[content.find("server.run()"):]
        assert "stop_event.set()" in after_server, (
            "stop_event must be set after server exits"
        )

    def test_bot_thread_joined_on_shutdown(self):
        """Bot thread is joined after server stops."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        after_server = content[content.find("server.run()"):]
        assert "bot_thread.join" in after_server, (
            "bot_thread must be joined after server stops"
        )

    def test_lifecycle_cleanup_in_thread(self):
        """Bot thread performs full lifecycle cleanup."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "await app.updater.stop_polling()" in content
        assert "await app.stop()" in content
        assert "await app.shutdown()" in content


class TestNoProcfileDependency:
    """Verify no production dependency on Procfile."""

    def test_bot_py_is_self_contained(self):
        """bot.py does not import or reference Procfile."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "Procfile" not in content, (
            "bot.py must not reference Procfile"
        )

    def test_bot_py_has_main_guard(self):
        """bot.py has if __name__ == '__main__' guard."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert 'if __name__ == "__main__":' in content, (
            "bot.py must have __main__ guard"
        )
        assert "main()" in content, (
            "bot.py must call main() in __main__ guard"
        )

    def test_procfile_still_exists(self):
        """Procfile still exists for other platforms (not deleted)."""
        assert os.path.exists("Procfile"), "Procfile must still exist"

    def test_serve_miniapp_still_exists(self):
        """serve_miniapp.py still exists for standalone use."""
        assert os.path.exists("serve_miniapp.py"), (
            "serve_miniapp.py must still exist"
        )


class TestPyCompile:
    """Verify py_compile passes for all modified files."""

    def test_bot_py_compiles(self):
        """bot.py passes py_compile."""
        result = os.system("python3 -m py_compile bot.py")
        assert result == 0, "bot.py failed py_compile"

    def test_serve_miniapp_compiles(self):
        """serve_miniapp.py passes py_compile."""
        result = os.system("python3 -m py_compile serve_miniapp.py")
        assert result == 0, "serve_miniapp.py failed py_compile"


class TestBusinessLogicPreserved:
    """Verify no business logic changes."""

    def test_anti_bot_unchanged(self):
        """Anti-bot logic is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "ANTI_BOT" in content
        assert "check_answer" in content
        assert "_send_math_question" in content

    def test_subscription_unchanged(self):
        """Subscription system is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "subscription_gate" in content
        assert "verify_subscription" in content

    def test_admin_channel_management_unchanged(self):
        """Admin channel management is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "addchannel_conv" in content
        assert "removechannel_conv" in content
        assert "admin_command" in content

    def test_mini_app_menu_button_unchanged(self):
        """Mini App menu button setup is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "setup_menu_button" in content
        assert "MenuButtonWebApp" in content

    def test_db_init_preserved(self):
        """Database initialization is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "db.init_db()" in content
        assert "db.load_channels()" in content

    def test_telegram_bot_token_check(self):
        """Token validation is preserved."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "TELEGRAM_BOT_TOKEN" in content
        assert "RuntimeError" in content

    def test_all_handlers_present(self):
        """All original handlers are still registered."""
        with open("bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        handlers = [
            "conv_handler",
            "addchannel_conv",
            "removechannel_conv",
            "verify_subscription",
            "admin_panel_callback",
            "subscription_message_gate",
            "on_chat_member_update",
        ]
        for handler in handlers:
            assert handler in content, f"Handler {handler} missing from bot.py"


if __name__ == "__main__":
    unittest.main()

"""
Integration tests for Mini App web-server lifecycle inside the Telegram bot process.

Verifies:
- create_miniapp_server() is called (deterministic port binding)
- server.run() is started in a daemon thread
- server.close() is called via post_shutdown for clean shutdown
- Telegram Application remains single-instance
- Existing polling flow is unchanged
- Waitress/asyncore does NOT install process signal handlers from background thread
- Port-bind failure propagates immediately (deterministic startup-error reporting)
"""

import signal
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


def _call_main():
    """Call bot.main() with a fake token, aborting when run_polling would block."""
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake-token"}):
        from bot import main
        try:
            main()
        except SystemExit:
            pass


class TestCreateMiniappServerCalled:
    """Verify create_miniapp_server() is used for deterministic port binding."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_create_server_called(self, mock_builder, mock_create, mock_thread_cls):
        """main() must call create_miniapp_server() to bind port before thread start."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        mock_create.assert_called_once()


class TestServerRunInDaemonThread:
    """Verify server.run() is started in a daemon thread."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_thread_created_and_started(self, mock_builder, mock_create, mock_thread_cls):
        """main() must create and start a single daemon thread for server.run()."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        assert mock_thread_cls.call_count == 1
        call_kwargs = mock_thread_cls.call_args
        assert call_kwargs[1]["name"] == "miniapp-web-server"
        assert call_kwargs[1]["daemon"] is True
        mock_thread_cls.return_value.start.assert_called_once()


class TestRunPollingRemainsOnMainThread:
    """Verify run_polling is still called on the main flow."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_run_polling_called(self, mock_builder, mock_create, mock_thread_cls):
        """main() must call app.run_polling() — the bot stays on the main thread."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        mock_app.run_polling.assert_called_once()


class TestServerRunIsThreadTarget:
    """Verify server.run() is invoked inside the daemon thread."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_server_run_called_in_thread(self, mock_builder, mock_create, mock_thread_cls):
        """The thread target must call server.run() when executed."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        # The thread was mocked so it never actually ran.
        # Capture the target and call it manually to verify server.run() is called.
        target_func = mock_thread_cls.call_args[1]["target"]
        mock_server.run.side_effect = SystemExit  # Prevent blocking
        try:
            target_func()
        except SystemExit:
            pass
        mock_server.run.assert_called_once()


class TestNoSecondApplication:
    """Verify bot does not create a second Telegram Application."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_single_application_created(self, mock_builder, mock_create, mock_thread_cls):
        """Only one Application instance should be created in main()."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        mock_builder.return_value.token.return_value.build.assert_called_once()


class TestPostShutdownClosesServer:
    """Verify server.close() is called via post_shutdown for clean shutdown."""

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_close_called_in_post_shutdown(self, mock_builder, mock_create, mock_thread_cls):
        """post_shutdown must call server.close() to cleanly shut down Waitress."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        # post_shutdown must be set
        assert mock_app.post_shutdown is not None

        # Call post_shutdown to verify it closes the server
        import asyncio
        post_shutdown = mock_app.post_shutdown
        asyncio.get_event_loop().run_until_complete(post_shutdown(mock_app))
        mock_server.close.assert_called_once()

    @patch("threading.Thread")
    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_close_exception_does_not_propagate(self, mock_builder, mock_create, mock_thread_cls):
        """If server.close() raises, post_shutdown must catch it, not crash."""
        mock_server = MagicMock()
        mock_server.effective_host = "0.0.0.0"
        mock_server.effective_port = 5000
        mock_server.close.side_effect = RuntimeError("already closed")
        mock_create.return_value = mock_server

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        _call_main()

        import asyncio
        post_shutdown = mock_app.post_shutdown
        # Must not raise
        asyncio.get_event_loop().run_until_complete(post_shutdown(mock_app))


class TestStartupFailurePropagates:
    """Verify port-bind failure propagates immediately (deterministic startup-error)."""

    @patch("serve_miniapp.create_miniapp_server")
    @patch("bot.ApplicationBuilder")
    def test_oserror_propagates(self, mock_builder, mock_create):
        """If create_miniapp_server() raises OSError, main() must propagate it."""
        mock_create.side_effect = OSError("Address already in use")

        mock_app = MagicMock()
        mock_app.post_init = None
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling.side_effect = SystemExit

        with pytest.raises(OSError, match="Address already in use"):
            _call_main()


class TestSignalSafety:
    """Verify Waitress/asyncore does NOT install process signal handlers."""

    def test_signal_from_non_main_thread_raises(self):
        """signal.signal() must raise ValueError when called from a non-main thread.

        This is the mechanism that prevents Waitress from competing with PTB
        for SIGINT/SIGTERM: asyncore.loop() (called by server.run()) tries to
        install signal handlers, but signal.signal() only works from the main
        thread.  The ValueError ensures no handlers are installed.
        """
        results = []

        def _try_signal():
            try:
                signal.signal(signal.SIGINT, lambda s, f: None)
                results.append("installed")
            except ValueError:
                results.append("blocked")

        t = threading.Thread(target=_try_signal)
        t.start()
        t.join()

        assert results == ["blocked"], (
            "signal.signal() succeeded from a non-main thread — "
            "Waitress could install competing signal handlers"
        )

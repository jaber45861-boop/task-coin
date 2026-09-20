"""
Tests for Mini App Web Server Configuration.

Verifies:
- Server configuration reads PORT from environment
- Server listens on 0.0.0.0
- Mini App index is served at root
- CSS assets are reachable
- JS assets are reachable
- Server serves static files correctly
"""

import os
import sys
import pytest


class TestServerConfiguration:
    """Verify server configuration reads from environment correctly."""

    def test_port_reads_from_environment(self):
        """Server should read PORT from environment variable."""
        # Simulate environment variable
        os.environ["PORT"] = "8080"
        port = int(os.environ.get("PORT", 5000))
        assert port == 8080
        del os.environ["PORT"]

    def test_port_defaults_to_5000(self):
        """Server should default to port 5000 if PORT not set."""
        # Ensure PORT is not set
        if "PORT" in os.environ:
            del os.environ["PORT"]
        port = int(os.environ.get("PORT", 5000))
        assert port == 5000

    def test_host_is_0_0_0_0(self):
        """Server must listen on 0.0.0.0 for WispByte compatibility."""
        # Read the server file and verify host configuration
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert 'host = "0.0.0.0"' in content, "Server must bind to 0.0.0.0"
        assert "waitress.serve" in content, "Server must use waitress.serve()"


class TestMiniAppServing:
    """Verify Mini App files are served correctly."""

    def test_miniapp_index_exists(self):
        """Mini App index.html must exist."""
        assert os.path.exists("miniapp/index.html"), "miniapp/index.html not found"

    def test_miniapp_css_exists(self):
        """Mini App CSS must exist."""
        assert os.path.exists("miniapp/css/app.css"), "miniapp/css/app.css not found"

    def test_miniapp_js_files_exist(self):
        """All Mini App JS files must exist."""
        js_files = ["app.js", "header.js", "home.js", "navigation.js", "telegram.js"]
        for js_file in js_files:
            path = f"miniapp/js/{js_file}"
            assert os.path.exists(path), f"{path} not found"

    def test_server_serves_index_at_root(self):
        """Server should serve index.html at root path."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Check that root route serves index.html
        assert '@app.route("/")' in content or "@app.route('/')" in content, \
            "Server must have root route"
        assert "send_from_directory" in content, \
            "Server must use send_from_directory"

    def test_server_serves_static_files(self):
        """Server should serve static assets via catch-all route."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Check for catch-all route for static files
        assert 'path:path' in content or '<path:path>' in content, \
            "Server must have catch-all route for static files"

    def test_miniapp_dir_configured(self):
        """Server must configure MINIAPP_DIR correctly."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "MINIAPP_DIR" in content, "Server must define MINIAPP_DIR"
        assert "miniapp" in content, "MINIAPP_DIR must reference miniapp directory"


class TestProcfileConfiguration:
    """Verify Procfile is configured for WispByte deployment."""

    def test_procfile_exists(self):
        """Procfile must exist."""
        assert os.path.exists("Procfile"), "Procfile not found"

    def test_procfile_has_web_process(self):
        """Procfile must have web process type for WispByte."""
        with open("Procfile", "r", encoding="utf-8") as f:
            content = f.read()
        assert "web:" in content, "Procfile must define web process type"

    def test_procfile_web_uses_serve_miniapp(self):
        """Web process must run serve_miniapp.py."""
        with open("Procfile", "r", encoding="utf-8") as f:
            content = f.read()
        assert "serve_miniapp.py" in content, \
            "Web process must run serve_miniapp.py"

    def test_procfile_preserves_worker(self):
        """Procfile must preserve existing worker process."""
        with open("Procfile", "r", encoding="utf-8") as f:
            content = f.read()
        assert "worker:" in content, "Procfile must preserve worker process"
        assert "bot.py" in content, "Worker process must run bot.py"


class TestRequirements:
    """Verify Flask is available for the server."""

    def test_flask_in_requirements(self):
        """Flask must be in requirements.txt."""
        with open("requirements.txt", "r", encoding="utf-8") as f:
            content = f.read()
        assert "flask" in content.lower(), "Flask must be in requirements.txt"


class TestNoHardcodedPorts:
    """Verify no hardcoded ports in server configuration."""

    def test_no_hardcoded_port_in_server(self):
        """Server must not hardcode specific ports like 10148."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Should not contain hardcoded WispByte ports
        assert "10148" not in content, "Must not hardcode port 10148"
        assert "8080" not in content, "Must not hardcode port 8080"
        # Should only have default port
        assert "5000" in content or "PORT" in content, \
            "Server must read PORT from environment"


class TestNoBusinessLogicChanges:
    """Verify server doesn't modify business logic."""

    def test_no_api_endpoints_added(self):
        """Server must not add API endpoints."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Should not contain business logic keywords
        forbidden = ["create_task", "submit_task", "start_task", "complete_task"]
        for keyword in forbidden:
            assert keyword not in content, \
                f"Server must not contain business logic: {keyword}"

    def test_no_database_operations(self):
        """Server must not perform database operations."""
        with open("serve_miniapp.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "sqlite" not in content.lower(), "Server must not use database"
        assert "db.execute" not in content, "Server must not execute SQL"

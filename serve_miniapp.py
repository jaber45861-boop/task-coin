"""
Mini App Web Server
===================
Serves the static Mini App files for WispByte deployment.

Usage:
    python serve_miniapp.py

The server:
- Reads PORT from environment (default: 5000)
- Listens on 0.0.0.0 (required by WispByte)
- Serves miniapp/ directory as static files
- Serves miniapp/index.html as the root page
- Uses Waitress production WSGI server (not Flask dev server)
"""

import os

import waitress
from flask import Flask, send_from_directory

app = Flask(__name__)

MINIAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp")


@app.route("/")
def index():
    """Serve the Mini App entry point."""
    return send_from_directory(MINIAPP_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    """Serve static assets (CSS, JS, images, etc.)."""
    return send_from_directory(MINIAPP_DIR, path)


def create_miniapp_server():
    """Create and bind a Waitress server without starting the event loop.

    Returns the ``waitress.server.TcpWSGIServer`` instance.  The socket is
    bound and validated at this point — if the PORT is unavailable an
    ``OSError`` is raised immediately, giving the caller deterministic
    startup-error reporting.

    Call ``server.run()`` in a background thread to start accepting
    requests, and ``server.close()`` for a clean shutdown.
    """
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    server = waitress.create_server(app, host=host, port=port, threads=6)
    return server


def run_web_server():
    """Start the production Waitress WSGI server (blocking).

    Reads PORT from the environment (defaults to 5000 for local dev).
    Binds to 0.0.0.0 as required by WispByte.
    Uses a fixed thread pool for concurrent request handling.

    Used for standalone execution (``python serve_miniapp.py``).
    When integrating into the Telegram bot process, prefer
    :func:`create_miniapp_server` + ``server.run()`` in a daemon thread
    so that ``server.close()`` is available for clean shutdown.
    """
    server = create_miniapp_server()
    host = server.effective_host
    port = server.effective_port
    print(f"Starting Mini App server on {host}:{port}")
    server.run()


if __name__ == "__main__":
    run_web_server()

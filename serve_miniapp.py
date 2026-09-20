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


def run_web_server():
    """Start the production Waitress WSGI server.

    Reads PORT from the environment (defaults to 5000 for local dev).
    Binds to 0.0.0.0 as required by WispByte.
    Uses a fixed thread pool for concurrent request handling.
    """
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Mini App server on {host}:{port}")
    waitress.serve(app, host=host, port=port, threads=6)


if __name__ == "__main__":
    run_web_server()

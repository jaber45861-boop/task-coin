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
"""

import os
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0"
    print(f"Starting Mini App server on {host}:{port}")
    app.run(host=host, port=port, debug=False)

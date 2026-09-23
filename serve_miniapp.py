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

from social_routes import social_bp
from task_routes import tasks_bp

app = Flask(__name__)

# API routes live in their own module so this file remains the static
# file server it was designed to be (SA-YT-01 account linking).
app.register_blueprint(social_bp)
# Production Task pipeline wiring (MT-TASK-03): list/start/submit.
app.register_blueprint(tasks_bp)

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

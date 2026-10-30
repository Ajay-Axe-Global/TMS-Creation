"""
TMS Platform — Flask app factory. Stays generic: adding a client never
touches this file, only clients/__init__.py's TASK_REGISTRY.
"""
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, send_from_directory  # noqa: E402

from database.seed import seed  # noqa: E402
from helpers.tms_worker import start_workers  # noqa: E402
from routes import register_all  # noqa: E402

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"


def _setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_tms_handler", False) for h in root.handlers):
        return  # the debug reloader imports this module twice
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h._tms_handler = True
        root.addHandler(h)


def _running_in_reloader_watcher() -> bool:
    # Flask's debug reloader forks a second process; only the process that actually serves
    # requests may run singleton background work (the TMS workers), or it would start twice.
    return os.getenv("FLASK_DEBUG", "1") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true"


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def create_app() -> Flask:
    _setup_logging()
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    seed()  # idempotent: tables, first admin
    register_all(app)
    if not _running_in_reloader_watcher():
        start_workers()   # TMS_WORKERS browsers (0 = none); see helpers/tms_worker.py
        from database.source_sync import start_scheduler
        start_scheduler()  # only if SYNC_INTERVAL_MINUTES > 0

    @app.after_request
    def no_store(resp):
        # Pages and API answers carry per-user data; never let a shared PC cache them.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/")
    @app.route("/login")
    def login_page():
        return send_from_directory(BASE_DIR / "auth", "login.html")

    @app.route("/dashboard")
    def dashboard_page():
        return send_from_directory(BASE_DIR / "auth", "dashboard.html")

    @app.route("/auth/<path:filename>")
    def auth_assets(filename):
        return send_from_directory(BASE_DIR / "auth", filename)

    @app.route("/admin")
    def admin_page():
        return send_from_directory(BASE_DIR / "admin" / "templates", "dashboard.html")

    @app.route("/static/<path:filename>")
    def shared_assets(filename):
        # The shared design system (static/css/theme.css, static/js/theme.js): front-end files only.
        if not filename.endswith((".js", ".css", ".svg", ".woff2")):
            return ("Not found", 404)
        return send_from_directory(BASE_DIR / "static", filename)

    @app.route("/views/<path:filename>")
    def view_assets(filename):
        # The page CSS / JS of views/<page>/ (the HTML itself is rendered by the client blueprints).
        if not filename.endswith((".js", ".css")):
            return ("Not found", 404)
        return send_from_directory(BASE_DIR / "views", filename)

    @app.route("/admin/<path:filename>")
    def admin_assets(filename):
        # Only the static front-end files; service.py etc. must never be served.
        if not filename.endswith((".js", ".css")):
            return ("Not found", 404)
        return send_from_directory(BASE_DIR / "admin", filename)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    if os.getenv("FLASK_DEBUG", "1") == "1":
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            print(f"Running on http://{_lan_ip()}:{port} (LAN)")
        app.run(debug=True, host="0.0.0.0", port=port, threaded=True)
    else:
        from waitress import serve
        threads = int(os.getenv("WAITRESS_THREADS", "16"))
        print(f"Starting waitress on http://{_lan_ip()}:{port} (LAN) with {threads} threads")
        serve(app, host="0.0.0.0", port=port, threads=threads)

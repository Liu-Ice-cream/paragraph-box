"""Authenticated loopback settings UI. No dependency or external service required."""

from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .plugin import ParagraphPlugin, open_text_file, find_vscode
from .engine import ParagraphIndex, index_signature
from .settings import ConfigValidationError, config_path, ensure_config, load_settings, read_document, save_document


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, base: Path, token: str | None = None):
        self.base = Path(base).resolve()
        self.version = json.loads((self.base / "plugin.json").read_text(encoding="utf-8-sig"))["Version"]
        self.token = token or secrets.token_urlsafe(32)
        self.last_activity = time.monotonic()
        self.progress_lock = threading.Lock()
        self.operations = {}
        super().__init__(("127.0.0.1", 0), SettingsHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/#{self.token}"

    @contextmanager
    def track_index(self, operation: str | None):
        if operation is None:
            yield None
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation):
            raise ValueError("索引操作标识无效。")
        with self.progress_lock:
            if operation in self.operations:
                raise ValueError("请使用新的索引操作标识。")
            # A bounded history allows polling a just-completed request without
            # letting old previews grow the long-lived server's memory.
            for key in list(self.operations):
                if len(self.operations) < 64:
                    break
                if self.operations[key]["status"] != "running":
                    del self.operations[key]
            if len(self.operations) >= 64:
                raise ValueError("索引请求过多，请稍后重试。")
            self.operations[operation] = {"status": "running", "stage": "preparing", "processed": 0,
                                          "updated": 0, "skipped": 0, "started": time.monotonic()}

        def update(data):
            with self.progress_lock:
                self.operations[operation].update({key: data[key] for key in ("stage", "processed", "updated", "skipped")})

        try:
            yield update
        except BaseException:
            with self.progress_lock:
                self.operations[operation].update(status="failed", ended=time.monotonic())
            raise
        else:
            with self.progress_lock:
                self.operations[operation].update(status="completed", ended=time.monotonic())

    def index_progress(self, operation: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation):
            raise ValueError("索引操作标识无效。")
        with self.progress_lock:
            entry = self.operations.get(operation)
            if entry is None:
                return None
            return {key: entry[key] for key in ("status", "stage", "processed", "updated", "skipped")} | {
                "elapsed_ms": round((entry.get("ended", time.monotonic()) - entry["started"]) * 1000)}


class SettingsHandler(BaseHTTPRequestHandler):
    server: SettingsServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def _respond(self, status: int, data: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        try:
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            # A newer preview can cancel this request while indexing continues.
            self.close_connection = True

    def _json(self, status: int, document: object) -> None:
        self._respond(status, json.dumps(document, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _deny(self, status: int, message: str) -> None:
        # Drain a bounded POST body before closing: Windows can otherwise reset
        # the socket and discard the error response when unread bytes remain.
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 65536:
                    self.connection.settimeout(5)
                    self.rfile.read(length)
            except (OSError, ValueError):
                pass
        self.close_connection = True
        self._json(status, {"error": message})

    def _allowed(self, api: bool) -> bool:
        host = self.headers.get("Host", "")
        if host != f"127.0.0.1:{self.server.server_port}":
            self._deny(403, "请通过本机设置入口访问。")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._deny(403, "请求来源不受支持。")
            return False
        if api and not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.server.token}"):
            self._deny(401, "设置会话已失效，请从段落匣重新打开设置。")
            return False
        self.server.last_activity = time.monotonic()
        return True

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if not self._allowed(route.startswith("/api/")):
            return
        try:
            if route == "/api/health":
                self._json(200, {"service": "paragraphbox-settings", "base": str(self.server.base), "pid": os.getpid(), "version": self.server.version})
            elif route == "/api/config":
                self._json(200, self._document())
            elif route == "/api/index-progress":
                operation = parse_qs(urlsplit(self.path).query).get("operation", [""])[0]
                self._json(200, {"progress": self.server.index_progress(operation)})
            else:
                files = {"/": "ui/index.html", "/app.css": "ui/app.css", "/type.css": "ui/type.css", "/app.js": "ui/app.js", "/icon.svg": "Images/app.svg"}
                if route not in files:
                    self._json(404, {"error": "页面不存在。"})
                    return
                path = self.server.base / files[route]
                kinds = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
                self._respond(200, path.read_bytes(), kinds[path.suffix])
        except (OSError, UnicodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def _document(self) -> dict:
        base = self.server.base
        data = read_document(base)
        settings = load_settings(base)
        manifest = json.loads((base / "plugin.json").read_text(encoding="utf-8-sig"))
        files, paragraphs = 0, 0
        report = None
        if settings.cache_path.is_file():
            try:
                connection = sqlite3.connect(f"{settings.cache_path.as_uri()}?mode=ro", uri=True, timeout=1)
                try:
                    files = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    paragraphs = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
                    try:
                        row = connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()
                        report = json.loads(row[0]) if row else None
                    except (sqlite3.Error, ValueError):
                        pass
                finally:
                    connection.close()
            except sqlite3.Error:
                pass
        return {"config": data, "name": manifest["Name"], "version": manifest["Version"],
                "editors": {"vscode": find_vscode() is not None},
                "sample_mode": not config_path(base).is_file(), "config_path": str(config_path(base)),
                "root_states": [{"alias": root.alias, "exists": root.path.is_dir()} for root in settings.roots],
                "stats": {"files": files, "paragraphs": paragraphs},
                "index_status": {"report": report, "config_current": bool(report and report.get("config_signature") == index_signature(settings.roots, settings.max_file_bytes))}}

    def do_POST(self) -> None:
        if not self._allowed(True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._deny(415, "请求格式不受支持。")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self._json(413, {"error": "设置内容过大或为空。"})
                self.close_connection = True
                return
            body = json.loads(self.rfile.read(length))
            route = urlsplit(self.path).path
            if route == "/api/config":
                save_document(self.server.base, body)
                self._json(200, self._document())
            elif route == "/api/search":
                if not isinstance(body, dict) or not isinstance(body.get("query"), str) or len(body["query"]) > 500:
                    raise ValueError("请输入 500 个字符以内的关键词。")
                with self.server.track_index(self.headers.get("X-ParagraphBox-Operation")) as progress:
                    metrics = {}
                    results = ParagraphPlugin(self.server.base).query(body["query"], progress=progress, metrics=metrics)
                self._json(200, {"results": results, "search_status": metrics})
            elif route == "/api/context":
                self._json(200, ParagraphPlugin(self.server.base).surrounding(body))
            elif route == "/api/open-location":
                self._json(200, ParagraphPlugin(self.server.base).open_location(body))
            elif route == "/api/rebuild-index":
                if body != {}:
                    raise ValueError("重新建立索引不接受额外参数。")
                with self.server.track_index(self.headers.get("X-ParagraphBox-Operation")) as progress:
                    settings = load_settings(self.server.base)
                    with ParagraphIndex(settings.cache_path, settings.roots, settings.max_file_bytes) as index:
                        index.sync(force=True, progress=progress)
                self._json(200, self._document())
            elif route == "/api/open-file":
                if not isinstance(body, dict) or not isinstance(body.get("path"), str):
                    raise ValueError("请提供原文件路径。")
                ParagraphPlugin(self.server.base).open_file(body["path"])
                self._json(200, {"ok": True})
            elif route == "/api/open-config-file":
                open_text_file(ensure_config(self.server.base))
                self._json(200, {"ok": True})
            elif route == "/api/folder":
                code = "import tkinter as tk,json; from tkinter import filedialog; root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True); value=filedialog.askdirectory(title='选择资料目录'); root.destroy(); print(json.dumps({'path':value}))"
                picked = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=180, check=False)
                if picked.returncode != 0:
                    raise ValueError("无法打开目录选择器，请直接粘贴文件夹路径。")
                self._json(200, json.loads(picked.stdout))
            else:
                self._json(404, {"error": "操作不存在。"})
        except (OSError, UnicodeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            self._json(400, {"error": str(exc), "issues": exc.issues if isinstance(exc, ConfigValidationError) else []})


def start_ui(base: Path, *, open_browser: bool = True) -> None:
    base = Path(base).resolve()
    home = Path(os.environ.get("PARAGRAPH_SEARCH_HOME", str(base / ".cache")))
    home.mkdir(parents=True, exist_ok=True)
    state_path = home / "settings-ui.json"
    try:
        old = json.loads(state_path.read_text(encoding="utf-8"))
        origin = old["origin"]
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None or parsed.path not in {"", "/"}:
            raise ValueError("Invalid cached settings address")
        request = urllib.request.Request(origin + "/api/health", headers={"Authorization": "Bearer " + old["token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1) as response:
            health = json.load(response)
        current_version = json.loads((base / "plugin.json").read_text(encoding="utf-8-sig"))["Version"]
        if health.get("base") == str(base) and health.get("service") == "paragraphbox-settings" and health.get("version") == current_version:
            if open_browser:
                webbrowser.open(origin + "/#" + old["token"])
            else:
                print(json.dumps({"url": origin + "/#" + old["token"]}), flush=True)
            return
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError):
        pass
    server = SettingsServer(base)
    state = {"origin": server.origin, "token": server.token, "pid": os.getpid()}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if open_browser:
        webbrowser.open(server.url)
    else:
        print(json.dumps({"url": server.url}), flush=True)

    def stop_when_idle() -> None:
        while time.monotonic() - server.last_activity < 1800:
            time.sleep(30)
        server.shutdown()

    threading.Thread(target=stop_when_idle, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if json.loads(state_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                state_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

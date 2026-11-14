"""Local web interface: static SPA + JSON API + Server-Sent Events.

Security model
* Bound to 127.0.0.1 unless "Fernzugriff" is enabled.
* Host header must be localhost / one of our IPs (blocks DNS rebinding).
* State-changing requests need the header ``X-A7V: 1`` – browsers refuse to
  send custom headers cross-origin without CORS, which we never grant (CSRF).
* Remote clients (phone) need the PIN and can only watch, pause and start a
  new session; settings, folders and shutdown stay local-only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .app import App
from .server import local_ip_addresses

log = logging.getLogger("a7v_tether.web")
STATIC = Path(__file__).with_name("static")
MAX_BODY = 1 << 20
_SECRET = secrets.token_bytes(32)

PICK_FOLDER_SCRIPT = r"""
import sys, tkinter, tkinter.filedialog as fd
root = tkinter.Tk(); root.withdraw(); root.attributes('-topmost', True)
p = fd.askdirectory(initialdir=sys.argv[1] or None, title='Ordner wählen', mustexist=False)
print(p or '', end='')
"""


def _pin_token(pin: str) -> str:
    return hmac.new(_SECRET, pin.encode(), hashlib.sha256).hexdigest()


def pick_folder(initial: str = "") -> Optional[str]:
    """Open a native folder dialog in a helper process (Tk must own a main thread)."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Ordner wählen")'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    r = subprocess.run([sys.executable, "-c", PICK_FOLDER_SCRIPT, initial or ""],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("Kein Ordnerdialog verfügbar – Pfad bitte eintippen")
    return r.stdout.strip() or None


def open_in_file_manager(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return  # browser tab closed – nothing to report
        log.exception("Fehler bei Anfrage von %s", client_address[0])

    def __init__(self, app: App, host: str, port: int) -> None:
        self.app = app
        self.login_failures: Dict[str, Tuple[int, float]] = {}
        super().__init__((host, port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr log
        log.debug("%s %s", self.client_address[0], fmt % args)

    @property
    def app(self) -> App:
        return self.server.app

    @property
    def is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0].startswith("::ffff:127.")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        hostname = re.sub(r":\d+$", "", host).strip("[]")
        allowed = {"localhost", "127.0.0.1", "::1"}
        if self.app.settings.web_remote:
            allowed.update(local_ip_addresses())
        return hostname in allowed

    def _authorized(self) -> bool:
        if self.is_local:
            return True
        if not self.app.settings.web_remote:
            return False
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)a7v_pin=([0-9a-f]{64})", cookie)
        return bool(m) and hmac.compare_digest(m.group(1), _pin_token(self.app.settings.web_pin))

    def _send(self, status: int, body: bytes, ctype: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store", **(headers or {})})

    def error(self, status: int, message: str) -> None:
        self.json({"error": message}, status)

    def body(self) -> Dict[str, Any]:
        data = json.loads(self._raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON-Objekt erwartet")
        return data

    # -- dispatch ----------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            # Always consume the request body first: an unread body would be parsed
            # as the next request on this keep-alive connection.
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Anfrage zu groß")
            self._raw = self.rfile.read(length) if length > 0 else b""
            if not self._host_ok():
                return self.error(HTTPStatus.FORBIDDEN, "Ungültiger Host")
            url = urlparse(self.path)
            path = unquote(url.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}

            if method == "GET" and not path.startswith("/api/"):
                return self.static(path)
            if path == "/api/login" and method == "POST":
                return self.login()
            if not self._authorized():
                return self.error(HTTPStatus.UNAUTHORIZED, "PIN erforderlich")
            if method != "GET" and self.headers.get("X-A7V") != "1":
                return self.error(HTTPStatus.FORBIDDEN, "Fehlender Schutz-Header")

            route = ROUTES.get((method, path))
            if route is None:
                for (m, pattern), fn in DYNAMIC_ROUTES:
                    match = pattern.fullmatch(path) if m == method else None
                    if match:
                        return fn(self, query, *match.groups())
                return self.error(HTTPStatus.NOT_FOUND, "Nicht gefunden")
            func, local_only = route
            if local_only and not self.is_local:
                return self.error(HTTPStatus.FORBIDDEN, "Nur am Rechner selbst erlaubt")
            return func(self, query)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self.error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover
            log.exception("Fehler in der Weboberfläche")
            try:
                self.error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except OSError:
                pass

    # -- static ----------------------------------------------------------------------
    def static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            target = STATIC / "index.html"  # SPA fallback
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' "
                                       "'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        })

    # -- API handlers --------------------------------------------------------------------
    def login(self) -> None:
        ip = self.client_address[0]
        fails, until = self.server.login_failures.get(ip, (0, 0.0))
        if time.time() < until:
            return self.error(429, "Zu viele Versuche – bitte kurz warten")
        pin = str(self.body().get("pin", ""))
        if self.app.settings.web_remote and hmac.compare_digest(pin, self.app.settings.web_pin):
            self.server.login_failures.pop(ip, None)
            return self.json({"ok": True}, headers={
                "Set-Cookie": f"a7v_pin={_pin_token(pin)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"})
        fails += 1
        self.server.login_failures[ip] = (fails, time.time() + min(2 ** fails, 300) if fails >= 3 else 0)
        log.warning("Falsche PIN von %s", ip)
        self.error(HTTPStatus.UNAUTHORIZED, "Falsche PIN")

    def api_status(self, q) -> None:
        st = self.app.status()
        st["client"] = {"local": self.is_local}
        if not self.is_local:
            st["folders"] = {}
            for d in st["disks"].values():
                if d:
                    d["path"] = ""
        self.json(st)

    def api_events(self, q) -> None:
        bus = self.app.bus
        sub = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(f"retry: 2000\nevent: hello\ndata: {json.dumps({'instance': self.app.instance_id})}\n\n".encode())
            self.wfile.flush()
            while not self.app.stopping:
                try:
                    ev = sub.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev["type"] == "log" and not self.is_local and ev["data"]["level"] == "DEBUG":
                    continue
                payload = json.dumps(ev["data"], ensure_ascii=False, default=str)
                self.wfile.write(f"id: {ev['id']}\nevent: {ev['type']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        finally:
            bus.unsubscribe(sub)
            self.close_connection = True

    def api_settings_get(self, q) -> None:
        from .config import ALL_EXTENSIONS, RENAME_TOKENS

        self.json({
            "settings": self.app.settings.to_dict(),
            "derived": {"staging_path": str(self.app.settings.staging_path)},
            "meta": {"extensions": ALL_EXTENSIONS, "tokens": RENAME_TOKENS, "tls": self.app.tls.info(),
                     "data_dir": str(self.app.data_dir)},
        })

    def api_settings_put(self, q) -> None:
        ok, errors, info = self.app.apply_settings(self.body())
        if not ok:
            return self.json({"ok": False, "errors": errors}, 422)
        self.json({"ok": True, **info, "settings": self.app.settings.to_dict()})

    def api_ftp(self, q) -> None:
        action = self.body().get("action")
        if action == "start":
            ok = self.app.ftp.start()
        elif action == "stop":
            self.app.ftp.stop()
            ok = True
        elif action == "restart":
            ok = self.app.ftp.restart()
        else:
            raise ValueError("action: start|stop|restart")
        self.json({"ok": ok, "ftp": self.app.ftp.status()})

    def api_pause(self, q) -> None:
        paused = bool(self.body().get("paused"))
        released = self.app.importer.set_paused(paused)
        self.json({"ok": True, "paused": paused, "released": released})

    def api_session(self, q) -> None:
        self.app.new_session(str(self.body().get("name", "")))
        self.json({"ok": True, "session": self.app.settings.session_name})

    def api_transfers(self, q) -> None:
        self.json(self.app.history.list(
            limit=int(q.get("limit", 50)), offset=int(q.get("offset", 0)),
            q=q.get("q", ""), status=q.get("status", ""), session=q.get("session", "")))

    def api_export(self, q) -> None:
        data = self.app.history.export_csv(q=q.get("q", ""), status=q.get("status", ""),
                                           session=q.get("session", "")).encode("utf-8")
        fname = time.strftime("a7v-verlauf-%Y%m%d-%H%M.csv")
        self._send(200, data, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})

    def api_timeline(self, q) -> None:
        minutes = max(5, min(int(q.get("minutes", 60)), 24 * 60))
        self.json({"minutes": minutes, "buckets": self.app.history.timeline(minutes)})

    def api_logs(self, q) -> None:
        items = list(self.app.logbuf.records)
        if not self.is_local:
            items = [i for i in items if i["level"] != "DEBUG"]
        self.json({"items": items[-int(q.get("limit", 500)):]})

    def api_image(self, q, tid: str, kind: str) -> None:
        p = self.app.thumbs.get(int(tid), large=(kind == "preview"))
        if not p:
            return self.error(404, "Keine Vorschau")
        self._send(200, p.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=604800, immutable"})

    def api_selftest(self, q) -> None:
        self.json({"results": self.app.selftest()})

    def api_tls_generate(self, q) -> None:
        self.json({"ok": True, "tls": self.app.generate_certificate()})

    def api_tls_ca(self, q) -> None:
        if not self.app.tls.ca_cert.is_file():
            return self.error(404, "Noch kein Zertifikat erzeugt")
        self._send(200, self.app.tls.ca_cert.read_bytes(), "application/x-pem-file",
                   {"Content-Disposition": 'attachment; filename="cacert.pem"'})

    def api_pick_folder(self, q) -> None:
        try:
            path = pick_folder(str(self.body().get("initial", "")))
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return self.json({"ok": False, "error": str(exc)})
        self.json({"ok": bool(path), "path": path})

    def api_open(self, q) -> None:
        data = self.body()
        s = self.app.settings
        if "transfer" in data:
            rec = self.app.history.get(int(data["transfer"]))
            if not rec:
                return self.error(404, "Unbekannt")
            path = Path(rec["backup_path"] or rec["final_path"])
            if not path.exists():
                path = path.parent
        else:
            path = {"watch": s.watch_path, "staging": s.staging_path, "backup": s.backup_path,
                    "data": self.app.data_dir}.get(data.get("which"))
            if not path:
                raise ValueError("Unbekannter Ordner")
        try:
            open_in_file_manager(path)
        except OSError as exc:
            return self.json({"ok": False, "error": str(exc)})
        self.json({"ok": True})

    def api_shutdown(self, q) -> None:
        self.json({"ok": True})
        log.info("Beenden über die Weboberfläche")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


# (method, path) -> (handler, local_only)
ROUTES = {
    ("GET", "/api/status"): (Handler.api_status, False),
    ("GET", "/api/events"): (Handler.api_events, False),
    ("GET", "/api/settings"): (Handler.api_settings_get, True),
    ("PUT", "/api/settings"): (Handler.api_settings_put, True),
    ("POST", "/api/ftp"): (Handler.api_ftp, True),
    ("POST", "/api/pause"): (Handler.api_pause, False),
    ("POST", "/api/session"): (Handler.api_session, False),
    ("GET", "/api/transfers"): (Handler.api_transfers, False),
    ("GET", "/api/transfers.csv"): (Handler.api_export, False),
    ("GET", "/api/timeline"): (Handler.api_timeline, False),
    ("GET", "/api/logs"): (Handler.api_logs, False),
    ("POST", "/api/selftest"): (Handler.api_selftest, True),
    ("POST", "/api/tls/generate"): (Handler.api_tls_generate, True),
    ("GET", "/api/tls/cacert.pem"): (Handler.api_tls_ca, True),
    ("POST", "/api/pick-folder"): (Handler.api_pick_folder, True),
    ("POST", "/api/open"): (Handler.api_open, True),
    ("POST", "/api/shutdown"): (Handler.api_shutdown, True),
}
DYNAMIC_ROUTES = [
    (("GET", re.compile(r"/api/image/(\d+)/(thumb|preview)")), Handler.api_image),
]


def create_server(app: App, port: int, tries: int = 10) -> WebServer:
    host = "0.0.0.0" if app.settings.web_remote else "127.0.0.1"
    last: Optional[OSError] = None
    for p in range(port, port + tries):
        try:
            return WebServer(app, host, p)
        except OSError as exc:
            last = exc
    raise OSError(f"Kein freier Port für die Weboberfläche ab {port}: {last}")

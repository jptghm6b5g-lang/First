"""FTP service the camera uploads to (start/stop at runtime, live progress)."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from .config import Settings
from .events import EventBus
from .importer import Importer, Upload
from .tls import TLS_AVAILABLE, TlsFiles

log = logging.getLogger("a7v_tether.ftp")


def local_ip_addresses() -> List[str]:
    """Best-effort list of this computer's LAN IPv4 addresses (primary first)."""
    ips: List[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class CameraHandlerMixin:
    """Hooks shared by the plain and the TLS handler class."""

    service: "FTPService"

    def handle(self) -> None:  # called for every new control connection
        if not self.service.ip_allowed(self.remote_ip):
            log.warning("Verbindung von %s abgelehnt (nicht in der Freigabeliste)", self.remote_ip)
            self.respond("421 Zugriff von dieser Adresse nicht erlaubt.")
            self.close_when_done()
            return
        super().handle()

    def on_connect(self) -> None:
        self.service.connection_opened(self)

    def on_disconnect(self) -> None:
        self.service.connection_closed(self)

    def on_login(self, username: str) -> None:
        log.info("Kamera angemeldet: %s (%s)", self.remote_ip, username)
        self.service.bus.publish("camera", self.service.cameras())

    def on_login_failed(self, username: str, password: str) -> None:
        log.warning("Anmeldung fehlgeschlagen von %s (Benutzer '%s') – Zugangsdaten in der Kamera prüfen",
                    self.remote_ip, username)

    def ftp_STOR(self, file, mode="w"):
        self._upload_file, self._upload_t0 = file, time.monotonic()
        return super().ftp_STOR(file, mode)

    def on_file_received(self, file: str) -> None:
        t0 = getattr(self, "_upload_t0", None)
        upload_s = time.monotonic() - t0 if t0 else None
        self._upload_file = None
        self.service.files_received += 1
        self.service.importer.submit(Upload(Path(file), self.remote_ip, upload_s))

    def on_incomplete_file_received(self, file: str) -> None:
        self._upload_file = None
        log.warning("Übertragung abgebrochen, verworfen: %s", Path(file).name)
        Path(file).unlink(missing_ok=True)
        self.service.bus.publish("aborted", {"name": Path(file).name, "ip": self.remote_ip})


class FTPService:
    def __init__(self, settings: Settings, importer: Importer, bus: EventBus, tls_files: TlsFiles) -> None:
        self.settings = settings
        self.importer, self.bus, self.tls_files = importer, bus, tls_files
        self.files_received = 0
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.tls_active = False
        self._server: Optional[FTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._handlers: List[FTPHandler] = []
        self._lock = threading.RLock()
        self._networks: List = []

    # -- state ----------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ip_allowed(self, ip: str) -> bool:
        if not self._networks:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return addr.is_loopback or any(addr in n for n in self._networks)

    def connection_opened(self, handler: FTPHandler) -> None:
        with self._lock:
            handler._connected_at = time.time()
            self._handlers.append(handler)
        log.info("Kamera verbunden: %s", handler.remote_ip)
        self.bus.publish("camera", self.cameras())

    def connection_closed(self, handler: FTPHandler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)
        log.info("Kamera getrennt: %s", handler.remote_ip)
        self.bus.publish("camera", self.cameras())

    def cameras(self) -> List[Dict]:
        with self._lock:
            handlers = list(self._handlers)
        by_ip: Dict[str, Dict] = {}
        for h in handlers:
            c = by_ip.setdefault(h.remote_ip, {"ip": h.remote_ip, "since": getattr(h, "_connected_at", None),
                                               "connections": 0, "authenticated": False})
            c["connections"] += 1
            c["authenticated"] = c["authenticated"] or bool(getattr(h, "authenticated", False))
        return list(by_ip.values())

    def uploads(self) -> List[Dict]:
        """Currently running uploads (read from the FTP thread; values are plain ints)."""
        out = []
        with self._lock:
            handlers = list(self._handlers)
        for h in handlers:
            name = getattr(h, "_upload_file", None)
            dc = getattr(h, "data_channel", None)
            if not name or dc is None:
                continue
            try:
                done = int(dc.get_transmitted_bytes())
            except Exception:
                done = 0
            elapsed = max(time.monotonic() - getattr(h, "_upload_t0", time.monotonic()), 1e-3)
            out.append({"name": Path(name).name, "ip": h.remote_ip, "bytes": done,
                        "elapsed_s": elapsed, "mbps": done * 8 / elapsed / 1e6})
        return out

    def status(self) -> Dict:
        s = self.settings
        return {
            "running": self.running,
            "error": self.error,
            "port": s.ftp_port,
            "host": s.ftp_host,
            "ips": local_ip_addresses(),
            "tls": self.tls_active,
            "started_at": self.started_at,
            "files_received": self.files_received,
            "cameras": self.cameras(),
            "uploads": self.uploads(),
        }

    # -- lifecycle --------------------------------------------------------------------
    def _build(self) -> FTPServer:
        s = self.settings
        s.staging_path.mkdir(parents=True, exist_ok=True)
        authorizer = DummyAuthorizer()
        authorizer.add_user(s.ftp_user, s.ftp_password, str(s.staging_path), perm="elradfmwMT")

        base = FTPHandler
        attrs: Dict = {}
        self.tls_active = False
        if s.tls_enabled:
            if not TLS_AVAILABLE:
                raise RuntimeError("FTPS benötigt die Pakete 'pyopenssl' und 'cryptography'")
            if not self.tls_files.exists():
                raise RuntimeError("Kein Zertifikat vorhanden – unter Einstellungen → Sicherheit erzeugen")
            from pyftpdlib.handlers import TLS_FTPHandler

            base = TLS_FTPHandler
            attrs.update(certfile=str(self.tls_files.cert), keyfile=str(self.tls_files.key),
                         tls_control_required=True, tls_data_required=True)
            self.tls_active = True

        handler = type("CameraHandler", (CameraHandlerMixin, base), attrs)
        handler.service = self
        handler.authorizer = authorizer
        handler.passive_ports = range(s.passive_port_min, s.passive_port_max + 1)
        handler.timeout = 600
        handler.banner = "a7v-tether bereit"
        handler.use_sendfile = False

        self._networks = [ipaddress.ip_network(n, strict=False) for n in s.allowed_ips]
        server = FTPServer((s.ftp_host, s.ftp_port), handler)
        server.max_cons = 32
        server.max_cons_per_ip = 16
        return server

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            try:
                self._server = self._build()
            except PermissionError:
                self.error = (f"Port {self.settings.ftp_port} benötigt Administratorrechte. "
                              f"Wähle einen Port ≥ 1024 (z. B. 2121) und trage ihn in der Kamera ein.")
            except OSError as exc:
                self.error = (f"Port {self.settings.ftp_port} ist belegt oder nicht verfügbar ({exc.strerror or exc}). "
                              f"Läuft ein anderer FTP-Server?")
            except Exception as exc:
                self.error = str(exc)
            else:
                self.error = None
            if self.error:
                log.error("FTP-Server konnte nicht starten: %s", self.error)
                self.bus.publish("ftp", self.status())
                return False

            self._stop.clear()
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._loop, args=(self._server,), name="ftp", daemon=True)
            self._thread.start()
        log.info("FTP-Server läuft auf Port %d%s", self.settings.ftp_port, " (FTPS)" if self.tls_active else "")
        self.bus.publish("ftp", self.status())
        return True

    def _loop(self, server: FTPServer) -> None:
        try:
            while not self._stop.is_set():
                server.ioloop.loop(0.25, blocking=False)
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            log.exception("FTP-Server abgestürzt")
        finally:
            server.close_all()
            with self._lock:
                self._handlers.clear()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread:
            thread.join(5)
        with self._lock:
            self._thread = None
            self._server = None
            self.started_at = None
        log.info("FTP-Server gestoppt")
        self.bus.publish("ftp", self.status())
        self.bus.publish("camera", [])

    def restart(self) -> bool:
        self.stop()
        return self.start()

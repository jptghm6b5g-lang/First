"""Wires all components together and implements the higher-level actions."""

from __future__ import annotations

import ftplib
import logging
import os
import shutil
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .config import FTP_FIELDS, Settings, SettingsStore, validate
from .events import EventBus, LogBuffer
from .history import History
from .importer import PART_SUFFIX, Importer, LightroomWatcher
from .server import FTPService, local_ip_addresses
from .thumbs import HAVE_PIL, ThumbStore
from .tls import TLS_AVAILABLE, TlsFiles, generate as generate_tls

log = logging.getLogger("a7v_tether")


def _disk(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return None
    return {"path": str(path), "free": u.free, "total": u.total}


class App:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.store = SettingsStore(self.data_dir)
        self.settings: Settings = self.store.load()
        self.bus = EventBus()
        self.logbuf = LogBuffer(self.bus)
        logging.getLogger("a7v_tether").addHandler(self.logbuf)
        self.history = History(self.data_dir / "history.db")
        self.thumbs = ThumbStore(self.data_dir / "thumbs")
        self.tls = TlsFiles(self.data_dir)
        self.watcher = LightroomWatcher(self.history, self.bus)
        self.watcher.timeout_s = self.settings.lightroom_timeout_s
        self.importer = Importer(self.settings, self.history, self.thumbs, self.bus, self.watcher)
        self.ftp = FTPService(self.settings, self.importer, self.bus, self.tls)
        self.instance_id = uuid.uuid4().hex
        self.web_url: Optional[str] = None
        self.stopping = False
        self._last_disk_warn = 0.0

    # -- lifecycle ----------------------------------------------------------------
    def ensure_dirs(self) -> None:
        s = self.settings
        for p in (s.watch_path, s.staging_path, s.backup_path):
            if p:
                p.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        try:
            self.ensure_dirs()
        except OSError as exc:
            log.error("Ordner konnten nicht angelegt werden: %s", exc)
        self.watcher.start()
        self.importer.start()
        held = self.importer.release_held()
        if held:
            log.info("%d wartende Bilder aus der letzten Sitzung an Lightroom übergeben", held)
        if self.settings.autostart_ftp:
            self.ftp.start()

    def stop(self) -> None:
        self.stopping = True
        self.ftp.stop()
        self.importer.stop()
        self.watcher.stop()
        self.history.close()

    # -- status ----------------------------------------------------------------------
    def watch_dir_files(self) -> int:
        try:
            return sum(1 for e in os.scandir(self.settings.watch_path)
                       if e.is_file() and not e.name.endswith(PART_SUFFIX) and not e.name.startswith("."))
        except OSError:
            return 0

    def status(self) -> Dict[str, Any]:
        s = self.settings
        disks = {"watch": _disk(s.watch_path), "backup": _disk(s.backup_path)}
        warnings: List[Dict[str, str]] = []
        ftp = self.ftp.status()
        lr = self.watcher.state()
        lr["files_in_watch_dir"] = self.watch_dir_files()
        if ftp["error"]:
            warnings.append({"level": "error", "text": ftp["error"]})
        elif not ftp["running"]:
            warnings.append({"level": "warn", "text": "FTP-Server ist gestoppt – die Kamera kann nichts senden."})
        if lr["status"] == "stalled":
            warnings.append({"level": "warn", "text": "Lightroom übernimmt keine Bilder. Ist Lightroom geöffnet und "
                             "„Datei → Automatisch importieren → Automatischen Import aktivieren“ eingeschaltet?"})
        min_free = s.min_free_gb * 1e9
        for key, label in (("watch", "Lightroom-Ordner"), ("backup", "Sicherungsordner")):
            d = disks[key]
            if d and d["free"] < min_free:
                warnings.append({"level": "error", "text": f"Wenig Speicherplatz auf dem {label}: "
                                 f"{d['free'] / 1e9:.1f} GB frei"})
                if time.time() - self._last_disk_warn > 300:
                    self._last_disk_warn = time.time()
                    log.warning("Wenig Speicherplatz (%s): %.1f GB frei", label, d["free"] / 1e9)
        if self.importer.paused:
            held = self.history.list(limit=1, status="held")["total"]
            warnings.append({"level": "info", "text": f"Übergabe pausiert – {held} Bild(er) warten."})
        if s.tls_enabled and not self.tls.exists():
            warnings.append({"level": "error", "text": "FTPS ist aktiv, aber es wurde noch kein Zertifikat erzeugt."})
        if not ftp["ips"]:
            warnings.append({"level": "warn", "text": "Keine Netzwerkverbindung gefunden – ist der Rechner im WLAN?"})
        return {
            "version": __version__,
            "instance": self.instance_id,
            "time": time.time(),
            "session": s.session_name,
            "paused": self.importer.paused,
            "queue": self.importer.queue_size,
            "ftp": ftp,
            "lightroom": lr,
            "disks": disks,
            "stats": self.history.stats(s.session_name),
            "warnings": warnings,
            "features": {"pillow": HAVE_PIL, "tls": TLS_AVAILABLE, "thumbs_oriented": self.thumbs.oriented},
            "web_url": self.web_url,
            "folders": {"watch": str(s.watch_path), "staging": str(s.staging_path),
                        "backup": str(s.backup_path) if s.backup_path else ""},
        }

    # -- actions ------------------------------------------------------------------------
    def apply_settings(self, data: Dict[str, Any]) -> Tuple[bool, Dict[str, str], Dict[str, bool]]:
        old = self.settings.copy()
        new = old.copy()
        try:
            new.update(data)
        except (TypeError, ValueError) as exc:
            return False, {"_": f"Ungültiger Wert: {exc}"}, {}
        errors = validate(new)
        if errors:
            return False, errors, {}
        try:
            for p in (new.watch_path, new.staging_path, new.backup_path):
                if p:
                    p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, {"watch_dir": f"Ordner kann nicht angelegt werden: {exc}"}, {}

        ftp_changed = any(getattr(old, f) != getattr(new, f) for f in FTP_FIELDS)
        self.settings.update(new.to_dict())  # in place: every component shares this object
        self.watcher.timeout_s = self.settings.lightroom_timeout_s
        if old.session_name != new.session_name:
            self.importer.new_session(new.session_name)
        self.store.save(self.settings)
        restarted = False
        if ftp_changed and self.ftp.running:
            self.ftp.restart()
            restarted = True
        elif ftp_changed and self.ftp.error:
            restarted = self.ftp.start()
        log.info("Einstellungen gespeichert%s", " – FTP-Server neu gestartet" if restarted else "")
        self.bus.publish("settings", {"changed": True})
        return True, {}, {
            "ftp_restarted": restarted,
            "web_restart_required": (old.web_port != new.web_port or old.web_remote != new.web_remote),
        }

    def new_session(self, name: str) -> None:
        name = (name or "").strip()[:80] or time.strftime("Shooting %Y-%m-%d %H%M")
        self.importer.new_session(name)
        self.store.save(self.settings)
        log.info("Neue Session: %s", name)
        self.bus.publish("session", {"name": name})

    def generate_certificate(self) -> Dict[str, Any]:
        ips = generate_tls(self.tls, local_ip_addresses())
        log.info("Zertifikat erzeugt für %s", ", ".join(ips))
        if self.settings.tls_enabled and self.ftp.running:
            self.ftp.restart()
        return self.tls.info()

    def selftest(self) -> List[Dict[str, str]]:
        s = self.settings
        results: List[Dict[str, str]] = []

        def add(name: str, status: str, detail: str = "") -> None:
            results.append({"name": name, "status": status, "detail": detail})

        ips = local_ip_addresses()
        add("Netzwerk", "ok" if ips else "error",
            f"IP-Adresse(n): {', '.join(ips)}" if ips else "Keine Netzwerkverbindung gefunden")

        for label, path in (("Lightroom-Ordner", s.watch_path), ("Eingangsordner", s.staging_path),
                            ("Sicherungsordner", s.backup_path)):
            if not path:
                continue
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / f".a7v-test-{uuid.uuid4().hex[:6]}.part"
                probe.write_bytes(b"ok")
                probe.unlink()
                add(label, "ok", str(path))
            except OSError as exc:
                add(label, "error", f"{path}: nicht beschreibbar ({exc})")
        try:
            same = os.stat(s.watch_path).st_dev == os.stat(s.staging_path).st_dev
            add("Gleiche Festplatte", "ok" if same else "warn",
                "Übergabe per sofortigem Umbenennen" if same else
                "Eingangs- und Lightroom-Ordner liegen auf verschiedenen Laufwerken – Übergabe dauert länger")
        except OSError:
            pass

        lingering = self.watch_dir_files()
        add("Lightroom-Auto-Import", "ok" if lingering == 0 else "warn",
            "Überwachter Ordner ist leer" if lingering == 0 else
            f"{lingering} Datei(en) liegen noch im überwachten Ordner – Auto-Import in Lightroom aktiv?")

        if not self.ftp.running:
            add("FTP-Server", "error", self.ftp.error or "Gestoppt")
        else:
            add("FTP-Server", "ok", f"Läuft auf Port {s.ftp_port}" + (" mit FTPS" if self.ftp.tls_active else ""))
            try:
                if self.ftp.tls_active:
                    ctx = ssl.create_default_context(cafile=str(self.tls.ca_cert))
                    client = ftplib.FTP_TLS(context=ctx, timeout=5)
                else:
                    client = ftplib.FTP(timeout=5)
                client.connect("127.0.0.1", s.ftp_port)
                client.login(s.ftp_user, s.ftp_password)
                if self.ftp.tls_active:
                    client.prot_p()
                client.set_pasv(True)
                client.nlst()
                client.quit()
                add("Anmeldung & Datenkanal", "ok", "Benutzer, Passwort und Passiv-Ports funktionieren")
            except Exception as exc:
                add("Anmeldung & Datenkanal", "error", str(exc))

        d = _disk(s.watch_path)
        if d:
            free = d["free"] / 1e9
            add("Speicherplatz", "ok" if free >= s.min_free_gb else "error", f"{free:.1f} GB frei")
        add("Vorschaubilder", "ok" if HAVE_PIL else "info",
            "Pillow installiert" if HAVE_PIL else "Ohne Pillow werden die Kamera-Vorschauen unverändert genutzt")
        if s.tls_enabled:
            info = self.tls.info()
            missing = [ip for ip in ips if ip not in info["ips"]]
            add("Zertifikat", "ok" if info["exists"] and not missing else "warn",
                "Gültig für " + ", ".join(info["ips"]) if not missing else
                f"Zertifikat enthält {', '.join(missing)} nicht – neu erzeugen")
        add("Firewall", "info", f"Freigeben: TCP {s.ftp_port} und {s.passive_port_min}–{s.passive_port_max} "
            "(kann nur von der Kamera aus geprüft werden)")
        log.info("Selbsttest: %d OK, %d Warnungen, %d Fehler",
                 sum(r["status"] == "ok" for r in results), sum(r["status"] == "warn" for r in results),
                 sum(r["status"] == "error" for r in results))
        return results

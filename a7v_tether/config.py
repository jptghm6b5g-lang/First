"""Persistent settings (JSON in the user's config folder) plus validation."""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

RAW_EXTENSIONS = ["arw"]
IMAGE_EXTENSIONS = ["arw", "jpg", "jpeg", "hif", "heif"]
ALL_EXTENSIONS = IMAGE_EXTENSIONS + ["mp4", "xml"]
DUPLICATE_POLICIES = ("rename", "skip")
RENAME_TOKENS = ("orig", "seq", "date", "time", "session", "camera")
FTP_FIELDS = {
    "ftp_host", "ftp_port", "ftp_user", "ftp_password", "passive_port_min",
    "passive_port_max", "allowed_ips", "tls_enabled", "staging_dir",
}


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "a7v-tether"


def _default_watch() -> str:
    return str(Path.home() / "Pictures" / "Lightroom-Watch")


@dataclass
class Settings:
    # --- FTP ---------------------------------------------------------------
    ftp_host: str = "0.0.0.0"
    ftp_port: int = 21
    ftp_user: str = "a7v"
    ftp_password: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    passive_port_min: int = 60000
    passive_port_max: int = 60100
    allowed_ips: List[str] = field(default_factory=list)
    tls_enabled: bool = False
    autostart_ftp: bool = True
    # --- folders -----------------------------------------------------------
    watch_dir: str = field(default_factory=_default_watch)
    staging_dir: str = ""
    backup_dir: str = ""
    backup_structure: str = "{session}/{date}"
    # --- import ------------------------------------------------------------
    extensions: List[str] = field(default_factory=lambda: list(IMAGE_EXTENSIONS))
    session_name: str = field(default_factory=lambda: datetime.now().strftime("Shooting %Y-%m-%d"))
    rename_enabled: bool = False
    rename_pattern: str = "{date}_{time}_{seq}"
    duplicate_policy: str = "skip"
    lightroom_timeout_s: int = 30
    min_free_gb: float = 5.0
    # --- web UI --------------------------------------------------------------
    web_port: int = 8765
    web_remote: bool = False
    web_pin: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")
    open_browser: bool = True

    # -- derived paths --------------------------------------------------------
    @property
    def watch_path(self) -> Path:
        return Path(self.watch_dir).expanduser().resolve()

    @property
    def staging_path(self) -> Path:
        if self.staging_dir:
            return Path(self.staging_dir).expanduser().resolve()
        w = self.watch_path
        return w.with_name(w.name + "-Eingang")

    @property
    def backup_path(self) -> Optional[Path]:
        return Path(self.backup_dir).expanduser().resolve() if self.backup_dir else None

    # -- (de)serialisation ------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        s = cls()
        s.update(data)
        return s

    def update(self, data: Dict[str, Any]) -> None:
        known = {f.name: f for f in fields(self)}
        for key, value in data.items():
            if key not in known:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, list):
                if isinstance(value, str):
                    value = [v.strip() for v in value.replace(";", ",").split(",")]
                value = [str(v).strip() for v in value if str(v).strip()]
            elif isinstance(current, str):
                value = "" if value is None else str(value).strip()
            setattr(self, key, value)
        self.extensions = sorted({e.lower().lstrip(".") for e in self.extensions})

    def copy(self) -> "Settings":
        return Settings.from_dict(self.to_dict())


def render_pattern(pattern: str, values: Dict[str, str]) -> str:
    return pattern.format_map(values)


def validate(s: Settings) -> Dict[str, str]:
    """Return {field: message} for every invalid setting."""
    err: Dict[str, str] = {}

    def port_ok(name: str, value: int) -> None:
        if not 1 <= value <= 65535:
            err[name] = "Port muss zwischen 1 und 65535 liegen"

    port_ok("ftp_port", s.ftp_port)
    port_ok("passive_port_min", s.passive_port_min)
    port_ok("passive_port_max", s.passive_port_max)
    port_ok("web_port", s.web_port)
    if s.passive_port_min > s.passive_port_max:
        err["passive_port_max"] = "Ende muss ≥ Anfang sein"
    elif s.passive_port_min <= s.ftp_port <= s.passive_port_max:
        err["ftp_port"] = "FTP-Port darf nicht im Passiv-Bereich liegen"
    if s.web_port == s.ftp_port:
        err["web_port"] = "Darf nicht dem FTP-Port entsprechen"
    try:
        ipaddress.ip_address(s.ftp_host)
    except ValueError:
        err["ftp_host"] = "Ungültige IP-Adresse (0.0.0.0 = alle Netzwerke)"
    if not s.ftp_user or any(c.isspace() for c in s.ftp_user):
        err["ftp_user"] = "Benutzername ohne Leerzeichen angeben"
    if len(s.ftp_password) < 4:
        err["ftp_password"] = "Mindestens 4 Zeichen"
    for ip in s.allowed_ips:
        try:
            ipaddress.ip_network(ip, strict=False)
        except ValueError:
            err["allowed_ips"] = f"Ungültige Adresse: {ip}"
            break
    if not s.watch_dir:
        err["watch_dir"] = "Ordner angeben"
    else:
        w = s.watch_path
        st = s.staging_path
        if st == w or w in st.parents:
            err["staging_dir"] = "Darf nicht der überwachte Ordner oder darin sein"
        b = s.backup_path
        if b and (b == w or w in b.parents):
            err["backup_dir"] = "Darf nicht im überwachten Ordner liegen"
        if b and (b == st or st in b.parents):
            err["backup_dir"] = "Darf nicht im Eingangsordner liegen"
    if not s.extensions:
        err["extensions"] = "Mindestens einen Dateityp wählen"
    else:
        unknown = [e for e in s.extensions if e not in ALL_EXTENSIONS]
        if unknown:
            err["extensions"] = f"Unbekannt: {', '.join(unknown)}"
    sample = {t: "x" for t in RENAME_TOKENS}
    for name in ("rename_pattern", "backup_structure"):
        pattern = getattr(s, name)
        if name == "rename_pattern" and not pattern:
            err[name] = "Muster angeben"
            continue
        try:
            out = render_pattern(pattern, sample)
        except (KeyError, IndexError, ValueError) as exc:
            err[name] = f"Unbekannter Platzhalter {exc}. Erlaubt: " + ", ".join("{%s}" % t for t in RENAME_TOKENS)
            continue
        if ".." in out.replace("\\", "/").split("/"):
            err[name] = "'..' ist nicht erlaubt"
    if s.duplicate_policy not in DUPLICATE_POLICIES:
        err["duplicate_policy"] = "Ungültige Auswahl"
    if not 5 <= s.lightroom_timeout_s <= 3600:
        err["lightroom_timeout_s"] = "5 bis 3600 Sekunden"
    if s.min_free_gb < 0:
        err["min_free_gb"] = "Darf nicht negativ sein"
    if s.web_remote and (len(s.web_pin) < 4):
        err["web_pin"] = "PIN mit mindestens 4 Zeichen für Fernzugriff"
    return err


class SettingsStore:
    """Thread-safe load/save of settings.json."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "settings.json"
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Settings:
        with self._lock:
            if self.path.is_file():
                try:
                    return Settings.from_dict(json.loads(self.path.read_text("utf-8")))
                except (ValueError, OSError):
                    broken = self.path.with_suffix(".defekt.json")
                    self.path.replace(broken)
            s = Settings()
            self._write(s)
            return s

    def save(self, s: Settings) -> None:
        with self._lock:
            self._write(s)

    def _write(self, s: Settings) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s.to_dict(), indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="a7v-tether",
        description="WLAN-Tethering Sony α7 V → Lightroom Classic",
    )
    p.add_argument("--data-dir", type=Path, default=default_data_dir(),
                   help="Ordner für Einstellungen, Verlauf und Vorschaubilder")
    p.add_argument("--no-browser", action="store_true", help="Browser beim Start nicht öffnen")
    p.add_argument("--headless", action="store_true", help="Ohne Weboberfläche (nur FTP + Import)")
    p.add_argument("--web-port", type=int, help="Port der Weboberfläche (Standard 8765)")
    p.add_argument("--port", type=int, help="FTP-Port überschreiben")
    p.add_argument("--watch-dir", help="Lightroom-Überwachungsordner überschreiben")
    p.add_argument("-v", "--verbose", action="store_true", help="Ausführliche Protokollierung")
    return p.parse_args(argv)

"""Configuration: defaults < config.toml < command line."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older Python
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_EXTENSIONS = ["arw", "jpg", "jpeg", "hif", "heif"]


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 21
    user: str = "a7v"
    password: str = "lightroom"
    passive_ports: Tuple[int, int] = (60000, 60100)
    watch_dir: Path = field(default_factory=lambda: Path.home() / "Pictures" / "Lightroom-Watch")
    staging_dir: Optional[Path] = None
    backup_dir: Optional[Path] = None
    extensions: List[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    verbose: bool = False

    def finalize(self) -> "Config":
        self.watch_dir = Path(self.watch_dir).expanduser().resolve()
        if self.staging_dir is None:
            # Sibling of the watched folder -> same disk -> atomic rename.
            self.staging_dir = self.watch_dir.with_name(self.watch_dir.name + "-Eingang")
        self.staging_dir = Path(self.staging_dir).expanduser().resolve()
        if self.backup_dir:
            self.backup_dir = Path(self.backup_dir).expanduser().resolve()
        if self.staging_dir == self.watch_dir:
            raise ValueError("staging_dir darf nicht der überwachte Ordner sein")
        if self.watch_dir in self.staging_dir.parents:
            raise ValueError("staging_dir darf nicht innerhalb des überwachten Ordners liegen")
        lo, hi = self.passive_ports
        if not (0 < lo <= hi < 65536):
            raise ValueError(f"Ungültiger passive_ports-Bereich: {self.passive_ports}")
        self.extensions = [e.lower().lstrip(".") for e in self.extensions]
        return self


def _apply_toml(cfg: Config, data: dict) -> None:
    ftp = data.get("ftp", {})
    imp = data.get("import", {})
    for key in ("host", "port", "user", "password"):
        if key in ftp:
            setattr(cfg, key, ftp[key])
    if "passive_ports" in ftp:
        lo, hi = ftp["passive_ports"]
        cfg.passive_ports = (int(lo), int(hi))
    for key in ("watch_dir", "staging_dir", "backup_dir"):
        if imp.get(key):
            setattr(cfg, key, Path(imp[key]))
    if "extensions" in imp:
        cfg.extensions = list(imp["extensions"]) or list(DEFAULT_EXTENSIONS)
    if "verbose" in data:
        cfg.verbose = bool(data["verbose"])


def parse_args(argv: Optional[List[str]] = None) -> Config:
    p = argparse.ArgumentParser(
        prog="a7v-tether",
        description="WLAN-Tethering Sony α7 V → Lightroom Classic (über FTP + Auto-Import)",
    )
    p.add_argument("-c", "--config", type=Path, help="Pfad zur config.toml (Standard: ./config.toml, falls vorhanden)")
    p.add_argument("--watch-dir", type=Path, help="Von Lightroom überwachter Ordner")
    p.add_argument("--staging-dir", type=Path, help="Eingangsordner für laufende Uploads")
    p.add_argument("--backup-dir", type=Path, help="Optional: zusätzliche Sicherungskopie jeder Datei")
    p.add_argument("--port", type=int, help="FTP-Port (Standard 21)")
    p.add_argument("--user", help="FTP-Benutzername")
    p.add_argument("--password", help="FTP-Passwort")
    p.add_argument("--extensions", help="Kommagetrennt, z.B. ARW,JPG")
    p.add_argument("-v", "--verbose", action="store_true", help="Ausführliche Protokollierung")
    args = p.parse_args(argv)

    cfg = Config()
    config_path = args.config or (Path("config.toml") if Path("config.toml").is_file() else None)
    if config_path:
        with open(config_path, "rb") as fh:
            _apply_toml(cfg, tomllib.load(fh))

    if args.watch_dir:
        cfg.watch_dir = args.watch_dir
    if args.staging_dir:
        cfg.staging_dir = args.staging_dir
    if args.backup_dir:
        cfg.backup_dir = args.backup_dir
    if args.port is not None:
        cfg.port = args.port
    if args.user:
        cfg.user = args.user
    if args.password:
        cfg.password = args.password
    if args.extensions:
        cfg.extensions = [e.strip() for e in args.extensions.split(",") if e.strip()]
    if args.verbose:
        cfg.verbose = True
    return cfg.finalize()

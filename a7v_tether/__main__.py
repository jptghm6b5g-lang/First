"""Entry point: ``python -m a7v_tether``."""

from __future__ import annotations

import logging
import socket
import sys

from .config import parse_args
from .importer import Importer
from .server import build_server

log = logging.getLogger("a7v_tether")


def local_ip_addresses() -> list:
    """Best-effort list of this computer's LAN IPv4 addresses."""
    ips = []
    try:
        # No packet is sent; this only asks the OS which interface it would use.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
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
    return ips or ["<IP dieses Rechners>"]


def main(argv=None) -> int:
    try:
        cfg = parse_args(argv)
    except (ValueError, OSError) as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    if not cfg.verbose:
        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)

    importer = Importer(cfg.watch_dir, cfg.extensions, cfg.backup_dir).start()
    try:
        server = build_server(cfg, importer)
    except PermissionError:
        print(
            f"Port {cfg.port} ist nicht erlaubt. Mit Admin-Rechten starten oder einen Port ≥1024 "
            f"wählen (z.B. --port 2121) und denselben Port in der Kamera eintragen.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"FTP-Server konnte nicht starten: {exc}", file=sys.stderr)
        return 1

    ips = ", ".join(local_ip_addresses())
    print(
        "\n"
        "  Sony α7 V  →  Lightroom Classic   (WLAN-Tethering)\n"
        "  ─────────────────────────────────────────────────\n"
        f"  In der Kamera eintragen:  Host {ips}   Port {cfg.port}\n"
        f"                            Benutzer '{cfg.user}'   Passwort '{cfg.password}'\n"
        "                            Passivmodus EIN, Sicheres Protokoll AUS\n"
        f"  Lightroom-Überwachungsordner: {cfg.watch_dir}\n"
        f"  Eingangsordner (intern):      {cfg.staging_dir}\n"
        + (f"  Sicherungskopien:             {cfg.backup_dir}\n" if cfg.backup_dir else "")
        + f"  Dateitypen:                   {', '.join(e.upper() for e in cfg.extensions)}\n"
        "  Beenden mit Strg+C\n"
    )

    try:
        server.serve_forever(handle_exit=False)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Beende … (%d Bilder übergeben)", importer.imported_count)
        server.close_all()
        importer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

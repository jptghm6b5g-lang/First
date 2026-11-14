"""Entry point: ``python -m a7v_tether``."""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser

from .app import App
from .config import parse_args, validate
from .server import local_ip_addresses

log = logging.getLogger("a7v_tether")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("a7v_tether").setLevel(logging.DEBUG)
    logging.getLogger().handlers[0].setLevel(logging.DEBUG if args.verbose else logging.INFO)
    if not args.verbose:
        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)

    app = App(args.data_dir)
    overrides = {}
    if args.port:
        overrides["ftp_port"] = args.port
    if args.watch_dir:
        overrides["watch_dir"] = args.watch_dir
    if args.web_port:
        overrides["web_port"] = args.web_port
    if overrides:
        candidate = app.settings.copy()
        candidate.update(overrides)
        errors = validate(candidate)
        if errors:
            for k, v in errors.items():
                print(f"Ungültig: {k}: {v}", file=sys.stderr)
            return 2
        app.settings.update(overrides)
        app.store.save(app.settings)

    app.start()
    s = app.settings
    ips = ", ".join(local_ip_addresses()) or "<keine Netzwerkverbindung>"

    web = None
    if not args.headless:
        from .webui import create_server

        try:
            web = create_server(app, s.web_port)
        except OSError as exc:
            print(f"Weboberfläche konnte nicht starten: {exc}", file=sys.stderr)
            app.stop()
            return 1
        port = web.server_address[1]
        app.web_url = f"http://127.0.0.1:{port}/"

    print(
        "\n"
        "  Sony α7 V  →  Lightroom Classic   ·   a7v-tether\n"
        "  ──────────────────────────────────────────────────\n"
        f"  FTP:    {ips}  Port {s.ftp_port}   Benutzer '{s.ftp_user}'\n"
        f"  Ordner: {s.watch_path}\n"
        + (f"  App:    {app.web_url}\n" if app.web_url else "")
        + (f"  Handy:  http://{local_ip_addresses()[0]}:{web.server_address[1]}/  (PIN {s.web_pin})\n"
           if web and s.web_remote and local_ip_addresses() else "")
        + "  Beenden mit Strg+C\n"
    )

    try:
        if web:
            if s.open_browser and not args.no_browser:
                threading.Timer(0.6, lambda: webbrowser.open(app.web_url)).start()
            web.serve_forever(poll_interval=0.5)
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Beende …")
        app.stop()
        if web:
            web.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

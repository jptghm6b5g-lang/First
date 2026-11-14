"""FTP server the Sony camera uploads to via its "FTP transfer" function."""

from __future__ import annotations

import logging
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from .config import Config
from .importer import Importer

log = logging.getLogger(__name__)


class CameraHandler(FTPHandler):
    importer: Importer  # set by build_server()
    banner = "a7v-tether ready"

    def on_connect(self) -> None:
        log.info("Kamera verbunden: %s", self.remote_ip)

    def on_disconnect(self) -> None:
        log.info("Kamera getrennt: %s", self.remote_ip)

    def on_login_failed(self, username: str, password: str) -> None:
        log.warning("Anmeldung fehlgeschlagen (Benutzer '%s') – Kameraeinstellungen prüfen", username)

    def on_file_received(self, file: str) -> None:
        log.debug("Empfangen: %s", file)
        self.importer.submit(Path(file))

    def on_incomplete_file_received(self, file: str) -> None:
        log.warning("Übertragung abgebrochen, verworfen: %s", Path(file).name)
        Path(file).unlink(missing_ok=True)


def build_server(config: Config, importer: Importer) -> FTPServer:
    config.staging_dir.mkdir(parents=True, exist_ok=True)

    authorizer = DummyAuthorizer()
    # e=cd l=list r=read a=append d=delete f=rename m=mkdir w=write M=chmod T=mtime
    authorizer.add_user(
        config.user, config.password, str(config.staging_dir), perm="elradfmwMT"
    )

    handler = type("BoundCameraHandler", (CameraHandler,), {})
    handler.authorizer = authorizer
    handler.importer = importer
    handler.passive_ports = range(config.passive_ports[0], config.passive_ports[1] + 1)
    handler.timeout = 600

    server = FTPServer((config.host, config.port), handler)
    server.max_cons = 16
    server.max_cons_per_ip = 8
    return server

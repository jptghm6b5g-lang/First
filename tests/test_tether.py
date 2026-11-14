import errno
import ftplib
import io
import os
import socket
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from a7v_tether import importer as importer_mod
from a7v_tether.config import Config, parse_args
from a7v_tether.importer import Importer, unique_path
from a7v_tether.server import build_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- importer --

def test_unique_path(tmp_path):
    target = tmp_path / "DSC00001.ARW"
    assert unique_path(target) == target
    target.write_bytes(b"x")
    assert unique_path(target).name == "DSC00001_1.ARW"
    (tmp_path / "DSC00001_1.ARW").write_bytes(b"x")
    assert unique_path(target).name == "DSC00001_2.ARW"


def test_process_moves_and_filters(tmp_path):
    watch, staging = tmp_path / "watch", tmp_path / "in"
    staging.mkdir()
    imp = Importer(watch, ["arw", "jpg"])
    raw = staging / "DSC00001.ARW"
    raw.write_bytes(b"raw")
    mp4 = staging / "C0001.MP4"
    mp4.write_bytes(b"video")

    assert imp.process(raw) == watch / "DSC00001.ARW"
    assert not raw.exists()
    assert imp.process(mp4) is None
    assert mp4.exists()  # stays in staging
    assert sorted(p.name for p in watch.iterdir()) == ["DSC00001.ARW"]


def test_backup_copy(tmp_path):
    watch, backup, staging = tmp_path / "w", tmp_path / "b", tmp_path / "s"
    staging.mkdir()
    src = staging / "A.JPG"
    src.write_bytes(b"jpeg")
    Importer(watch, None, backup).process(src)
    assert (watch / "A.JPG").read_bytes() == b"jpeg"
    assert (backup / "A.JPG").read_bytes() == b"jpeg"


def test_cross_device_uses_part_file_then_rename(tmp_path):
    watch, staging = tmp_path / "w", tmp_path / "s"
    staging.mkdir()
    src = staging / "B.ARW"
    src.write_bytes(b"0123456789" * 1000)
    real_replace = os.replace
    seen = []

    def fake_replace(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "cross-device")
        seen.append((Path(a).name, Path(b).name))
        return real_replace(a, b)

    with mock.patch.object(importer_mod.os, "replace", fake_replace):
        Importer(watch).process(src)

    assert seen == [("B.ARW.part", "B.ARW")]
    assert (watch / "B.ARW").stat().st_size == 10000
    assert not src.exists()
    assert list(watch.iterdir()) == [watch / "B.ARW"]


# ------------------------------------------------------------------ config --

def test_config_defaults_and_validation(tmp_path):
    cfg = Config(watch_dir=tmp_path / "LR").finalize()
    assert cfg.staging_dir == (tmp_path / "LR-Eingang").resolve()
    with pytest.raises(ValueError):
        Config(watch_dir=tmp_path / "LR", staging_dir=tmp_path / "LR" / "sub").finalize()


def test_toml_and_cli_override(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text(
        '[ftp]\nport = 2121\nuser = "cam"\npassword = "pw"\n'
        f'[import]\nwatch_dir = "{(tmp_path / "W").as_posix()}"\nextensions = ["ARW"]\n'
    )
    cfg = parse_args(["-c", str(toml), "--port", "2200"])
    assert (cfg.port, cfg.user, cfg.password) == (2200, "cam", "pw")
    assert cfg.extensions == ["arw"]
    assert cfg.watch_dir == (tmp_path / "W").resolve()


# ------------------------------------------------- end-to-end over real FTP --

@pytest.fixture
def running_server(tmp_path):
    lo = free_port()
    cfg = Config(
        host="127.0.0.1",
        port=free_port(),
        user="a7v",
        password="secret",
        passive_ports=(lo, lo),
        watch_dir=tmp_path / "LR-Watch",
    ).finalize()
    imp = Importer(cfg.watch_dir, cfg.extensions).start()
    server = build_server(cfg, imp)
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1, "handle_exit": False}, daemon=True)
    t.start()
    yield cfg, imp
    server.close_all()
    imp.stop()


def connect(cfg):
    ftp = ftplib.FTP()
    ftp.connect(cfg.host, cfg.port, timeout=5)
    ftp.login(cfg.user, cfg.password)
    ftp.set_pasv(True)
    return ftp


def test_camera_style_upload_lands_in_watch_folder(running_server):
    cfg, imp = running_server
    payload = os.urandom(3 * 1024 * 1024)
    ftp = connect(cfg)
    # Sony cameras may create a destination directory first.
    ftp.mkd("DCIM")
    ftp.cwd("DCIM")
    ftp.storbinary("STOR DSC01234.ARW", io.BytesIO(payload))
    ftp.storbinary("STOR DSC01234.JPG", io.BytesIO(b"jpeg"))
    ftp.storbinary("STOR C0001.MP4", io.BytesIO(b"video"))
    ftp.quit()

    deadline = time.time() + 5
    while imp.imported_count < 2 and time.time() < deadline:
        time.sleep(0.05)
    imp.wait_idle()

    files = sorted(p.name for p in cfg.watch_dir.iterdir())
    assert files == ["DSC01234.ARW", "DSC01234.JPG"]
    assert (cfg.watch_dir / "DSC01234.ARW").read_bytes() == payload
    assert (cfg.staging_dir / "DCIM" / "C0001.MP4").exists()


def test_wrong_password_rejected(running_server):
    cfg, _ = running_server
    ftp = ftplib.FTP()
    ftp.connect(cfg.host, cfg.port, timeout=5)
    with pytest.raises(ftplib.error_perm):
        ftp.login(cfg.user, "wrong")
    ftp.close()


def test_aborted_upload_never_reaches_lightroom(running_server):
    cfg, imp = running_server
    ftp = connect(cfg)
    conn = ftp.transfercmd("STOR DSC09999.ARW")
    conn.sendall(b"half a file")
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    ftp.abort()
    conn.close()
    try:
        ftp.quit()
    except ftplib.all_errors:
        pass
    time.sleep(0.5)
    imp.wait_idle()
    assert list(cfg.watch_dir.iterdir()) == []

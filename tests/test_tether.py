import errno
import ftplib
import http.client
import io
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from a7v_tether import importer as importer_mod
from a7v_tether.config import Settings, validate
from a7v_tether.importer import Upload, unique_path
from a7v_tether.metadata import extract_preview, read_info
from a7v_tether.server import local_ip_addresses
from a7v_tether.tls import TLS_AVAILABLE

from conftest import make_arw, make_jpeg_with_exif, wait_for


def put(dir_: Path, name: str, data: bytes) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------ metadata --

def test_read_arw_metadata_and_preview(tmp_path):
    p = put(tmp_path, "DSC00001.ARW", make_arw(orientation=6))
    info = read_info(p)
    assert (info.make, info.model, info.lens) == ("SONY", "ILCE-TEST", "FE 35mm F1.4 GM")
    assert info.exposure == "1/250" and info.fnumber == 2.8 and info.iso == 400
    assert info.focal_mm == 35 and info.orientation == 6
    assert info.datetime == "2026:11:14 15:30:45"
    assert info.summary() == "1/250 s · f/2.8 · ISO 400 · 35 mm"
    preview = extract_preview(p, info)
    assert preview[:2] == b"\xff\xd8" and preview.endswith(b"\xff\xd9")


def test_read_jpeg_exif(tmp_path):
    p = put(tmp_path, "DSC00002.JPG", make_jpeg_with_exif())
    info = read_info(p)
    assert info.model == "ILCE-TEST" and info.exposure == "1/1000" and info.iso == 100
    assert extract_preview(p, info)[:2] == b"\xff\xd8"


@pytest.mark.parametrize("data", [b"", b"II*\x00\xff\xff\xff\xff", b"MM\x00*" + b"\x00" * 10, os.urandom(5000),
                                  b"\xff\xd8\xff\xe1\x00\x02"])
def test_metadata_never_raises_on_garbage(tmp_path, data):
    p = put(tmp_path, "bad.ARW", data)
    info = read_info(p)
    assert extract_preview(p, info) is None


# -------------------------------------------------------------------- config --

def test_validation():
    s = Settings(watch_dir="/tmp/LR")
    assert validate(s) == {}
    bad = Settings(watch_dir="/tmp/LR", staging_dir="/tmp/LR/in", ftp_port=60010,
                   rename_pattern="{nope}", extensions=[], backup_structure="../x")
    errors = validate(bad)
    assert set(errors) >= {"staging_dir", "ftp_port", "rename_pattern", "extensions", "backup_structure"}


def test_settings_roundtrip_and_coercion(tmp_path):
    s = Settings()
    s.update({"ftp_port": "2121", "allowed_ips": "192.168.1.5, 10.0.0.0/8", "extensions": [".ARW", "JPG"],
              "tls_enabled": 1, "unknown_key": "ignored"})
    assert s.ftp_port == 2121 and s.allowed_ips == ["192.168.1.5", "10.0.0.0/8"]
    assert s.extensions == ["arw", "jpg"] and s.tls_enabled is True
    assert Settings.from_dict(json.loads(json.dumps(s.to_dict()))).to_dict() == s.to_dict()


# ------------------------------------------------------------------ pipeline --

def test_import_moves_file_with_history_and_thumbnail(make_app):
    app = make_app()
    s = app.settings
    src = put(s.staging_path, "DSC00001.ARW", make_arw())
    tid = app.importer.process(Upload(src, "192.168.1.50", 1.5))
    rec = app.history.get(tid)
    assert rec["status"] == "imported" and rec["camera_ip"] == "192.168.1.50"
    assert (s.watch_path / "DSC00001.ARW").is_file() and not src.exists()
    assert rec["exif"]["lens"] == "FE 35mm F1.4 GM" and rec["has_thumb"]
    assert app.thumbs.get(tid) and app.thumbs.get(tid, large=True)


def test_rename_keeps_raw_jpeg_pairs_together(make_app):
    app = make_app()
    app.apply_settings({"rename_enabled": True, "rename_pattern": "{session}_{seq}_{orig}"})
    st = app.settings.staging_path
    for name in ("DSC00001.ARW", "DSC00001.JPG", "DSC00002.ARW"):
        app.importer.process(Upload(put(st, name, make_arw(seed=hash(name)))))
    names = sorted(p.name for p in app.settings.watch_path.iterdir())
    assert names == ["Test_0001_DSC00001.ARW", "Test_0001_DSC00001.JPG", "Test_0002_DSC00002.ARW"]


def test_duplicates_are_skipped(make_app):
    app = make_app(duplicate_policy="skip")
    st, data = app.settings.staging_path, make_arw()
    first = app.importer.process(Upload(put(st, "A.ARW", data)))
    second = app.importer.process(Upload(put(st, "A.ARW", data)))
    assert app.history.get(first)["status"] == "imported"
    rec = app.history.get(second)
    assert rec["status"] == "duplicate" and Path(rec["final_path"]).is_file()
    assert [p.name for p in app.settings.watch_path.iterdir()] == ["A.ARW"]


def test_pause_collects_and_resume_delivers(make_app):
    app = make_app()
    app.importer.set_paused(True)
    tid = app.importer.process(Upload(put(app.settings.staging_path, "B.ARW", make_arw())))
    assert app.history.get(tid)["status"] == "held"
    assert list(app.settings.watch_path.iterdir()) == []
    assert app.importer.set_paused(False) == 1
    assert app.history.get(tid)["status"] == "imported"
    assert (app.settings.watch_path / "B.ARW").is_file()


def test_unselected_types_stay_and_backup_is_structured(make_app, tmp_path):
    app = make_app(extensions=["arw"], backup_dir=str(tmp_path / "Backup"), backup_structure="{session}/{date}")
    st = app.settings.staging_path
    jpg = app.importer.process(Upload(put(st, "C.JPG", make_jpeg_with_exif())))
    raw = app.importer.process(Upload(put(st, "C.ARW", make_arw())))
    assert app.history.get(jpg)["status"] == "ignored" and (st / "C.JPG").is_file()
    assert app.history.get(raw)["status"] == "imported"
    backup = tmp_path / "Backup" / "Test" / "2026-11-14"
    assert sorted(p.name for p in backup.iterdir()) == ["C.ARW", "C.JPG"]


def test_cross_device_uses_part_file_then_rename(make_app):
    app = make_app()
    src = put(app.settings.staging_path, "D.ARW", make_arw())
    real_replace, seen = os.replace, []

    def fake_replace(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "cross-device")
        seen.append((Path(a).name, Path(b).name))
        return real_replace(a, b)

    with mock.patch.object(importer_mod.os, "replace", fake_replace):
        app.importer.process(Upload(src))
    assert seen == [("D.ARW.part", "D.ARW")]
    assert [p.name for p in app.settings.watch_path.iterdir()] == ["D.ARW"]


def test_unique_path(tmp_path):
    t = tmp_path / "X.ARW"
    assert unique_path(t) == t
    t.write_bytes(b"1")
    assert unique_path(t).name == "X_1.ARW"


def test_lightroom_pickup_and_timeout(make_app):
    app = make_app()
    events = app.bus.subscribe()
    tid = app.importer.process(Upload(put(app.settings.staging_path, "E.ARW", make_arw())))
    app.watcher.timeout_s = 0
    time.sleep(0.01)
    app.watcher.check_once()
    assert app.watcher.state()["status"] == "stalled"
    (app.settings.watch_path / "E.ARW").unlink()  # "Lightroom" takes the file
    app.watcher.check_once()
    assert app.watcher.state()["status"] == "ok"
    assert app.history.get(tid)["lr_picked_up"]
    kinds = []
    while not events.empty():
        kinds.append(events.get()["type"])
    assert "lightroom" in kinds and "transfer" in kinds


# ----------------------------------------------------------------- FTP e2e --

def ftp_login(app, tls=False):
    s = app.settings
    if tls:
        client = ftplib.FTP_TLS(context=ssl.create_default_context(cafile=str(app.tls.ca_cert)), timeout=5)
    else:
        client = ftplib.FTP(timeout=5)
    client.connect("127.0.0.1", s.ftp_port)
    client.login(s.ftp_user, s.ftp_password)
    if tls:
        client.prot_p()
    client.set_pasv(True)
    return client


def test_camera_upload_over_ftp(make_app):
    app = make_app(start=True, autostart_ftp=True)
    assert app.ftp.running, app.ftp.error
    c = ftp_login(app)
    c.mkd("DCIM")
    c.cwd("DCIM")
    c.storbinary("STOR DSC01234.ARW", io.BytesIO(make_arw(raw_bytes=2_000_000)))
    c.storbinary("STOR C0001.MP4", io.BytesIO(b"video"))
    c.quit()
    assert wait_for(lambda: app.history.list(status="imported")["total"] == 1)
    app.importer.wait_idle()
    assert [p.name for p in app.settings.watch_path.iterdir()] == ["DSC01234.ARW"]
    rec = app.history.list(status="imported")["items"][0]
    assert rec["upload_s"] is not None and rec["camera_ip"] == "127.0.0.1"
    assert app.history.list(status="ignored")["total"] == 1


def test_wrong_password_and_ip_allowlist(make_app):
    app = make_app(start=True, autostart_ftp=True, allowed_ips=["192.168.77.0/24"])
    c = ftplib.FTP(timeout=5)
    c.connect("127.0.0.1", app.settings.ftp_port)
    with pytest.raises(ftplib.error_perm):
        c.login(app.settings.ftp_user, "wrong")
    c.close()
    assert app.ftp.ip_allowed("192.168.77.10") and not app.ftp.ip_allowed("192.168.1.5")


def test_aborted_upload_never_reaches_lightroom(make_app):
    app = make_app(start=True, autostart_ftp=True)
    c = ftp_login(app)
    conn = c.transfercmd("STOR DSC09999.ARW")
    conn.sendall(b"half a file")
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    c.abort()
    conn.close()
    try:
        c.quit()
    except ftplib.all_errors:
        pass
    time.sleep(0.5)
    app.importer.wait_idle()
    assert list(app.settings.watch_path.iterdir()) == []


def test_port_in_use_reports_friendly_error(make_app):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    app = make_app(ftp_port=blocker.getsockname()[1])
    assert app.ftp.start() is False and "belegt" in app.ftp.error
    blocker.close()


def test_settings_change_restarts_ftp(make_app):
    from conftest import free_port

    app = make_app(start=True, autostart_ftp=True)
    new_port = free_port()
    ok, errors, info = app.apply_settings({"ftp_port": new_port})
    assert ok and info["ftp_restarted"] and app.ftp.running
    ftp_login(app).quit()


@pytest.mark.skipif(not TLS_AVAILABLE, reason="pyopenssl/cryptography fehlen")
def test_ftps_upload(make_app):
    app = make_app()
    app.generate_certificate()
    app.apply_settings({"tls_enabled": True})
    assert app.ftp.start(), app.ftp.error
    c = ftp_login(app, tls=True)
    c.storbinary("STOR DSC05555.ARW", io.BytesIO(make_arw()))
    c.quit()
    app.importer.start()
    assert wait_for(lambda: (app.settings.watch_path / "DSC05555.ARW").exists())
    results = {r["name"]: r["status"] for r in app.selftest()}
    assert results["Anmeldung & Datenkanal"] == "ok"


# ------------------------------------------------------------------- web API --

@pytest.fixture
def web(make_app):
    from a7v_tether.webui import create_server

    app = make_app(start=True, autostart_ftp=True)
    server = create_server(app, app.settings.web_port)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield app, server.server_address[1]
    app.stopping = True
    server.shutdown()
    server.server_close()


def call(port, method, path, body=None, headers=None, host="127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = {"X-A7V": "1", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, data, hdrs)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res.status, json.loads(raw), res
    except ValueError:
        return res.status, raw, res


def test_web_status_and_static(web):
    app, port = web
    status, data, _ = call(port, "GET", "/api/status")
    assert status == 200 and data["ftp"]["running"] and data["client"]["local"]
    status, body, res = call(port, "GET", "/")
    assert status == 200 and b"a7v Tether" in body and "Content-Security-Policy" in res.headers
    assert call(port, "GET", "/../../etc/passwd")[0] == 200  # falls back to index.html, never escapes


def test_web_security_checks(web):
    app, port = web
    assert call(port, "PUT", "/api/settings", {}, headers={"X-A7V": "0"})[0] == 403
    assert call(port, "GET", "/api/status", headers={"Host": "evil.example:80"})[0] == 403


def test_web_settings_validation_and_save(web):
    app, port = web
    status, data, _ = call(port, "PUT", "/api/settings", {"rename_pattern": "{bogus}"})
    assert status == 422 and "rename_pattern" in data["errors"]
    status, data, _ = call(port, "PUT", "/api/settings", {"rename_enabled": True, "lightroom_timeout_s": 45})
    assert status == 200 and data["ok"]
    assert json.loads(app.store.path.read_text())["lightroom_timeout_s"] == 45


def test_web_transfers_images_csv_and_sse(web):
    app, port = web
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/events")
    stream = conn.getresponse()
    assert stream.getheader("Content-Type").startswith("text/event-stream")
    assert b"event: hello" in stream.readline() + stream.readline() + stream.readline()

    c = ftp_login(app)
    c.storbinary("STOR DSC07777.ARW", io.BytesIO(make_arw()))
    c.quit()

    got = b""
    end = time.time() + 5
    while b"event: transfer" not in got and time.time() < end:
        got += stream.readline()
    assert b"event: transfer" in got
    conn.close()

    status, data, _ = call(port, "GET", "/api/transfers?limit=5")
    tid = data["items"][0]["id"]
    assert data["items"][0]["status"] == "imported"
    status, img, res = call(port, "GET", f"/api/image/{tid}/thumb")
    assert status == 200 and img[:2] == b"\xff\xd8"
    status, csv_body, res = call(port, "GET", "/api/transfers.csv")
    assert status == 200 and "DSC07777.ARW" in csv_body.decode("utf-8-sig")
    status, tl, _ = call(port, "GET", "/api/timeline?minutes=10")
    assert sum(tl["buckets"]) == 1


def test_web_pause_session_selftest(web):
    app, port = web
    assert call(port, "POST", "/api/pause", {"paused": True})[1]["paused"] is True
    assert app.importer.paused
    call(port, "POST", "/api/pause", {"paused": False})
    assert call(port, "POST", "/api/session", {"name": "Hochzeit"})[1]["session"] == "Hochzeit"
    results = call(port, "POST", "/api/selftest", {})[1]["results"]
    assert {r["name"]: r["status"] for r in results}["Anmeldung & Datenkanal"] == "ok"


@pytest.mark.skipif(not [ip for ip in local_ip_addresses() if not ip.startswith("127.")],
                    reason="keine LAN-Adresse")
def test_remote_client_needs_pin_and_is_read_only(make_app):
    from a7v_tether.webui import create_server

    app = make_app(start=True, web_remote=True, web_pin="4711")
    server = create_server(app, app.settings.web_port)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    port, ip = server.server_address[1], local_ip_addresses()[0]
    try:
        assert call(port, "GET", "/api/status", host=ip)[0] == 401
        assert call(port, "POST", "/api/login", {"pin": "0000"}, host=ip)[0] == 401
        status, _, res = call(port, "POST", "/api/login", {"pin": "4711"}, host=ip)
        assert status == 200
        cookie = res.getheader("Set-Cookie").split(";")[0]
        status, data, _ = call(port, "GET", "/api/status", headers={"Cookie": cookie}, host=ip)
        assert status == 200 and data["client"]["local"] is False and data["folders"] == {}
        assert call(port, "GET", "/api/settings", headers={"Cookie": cookie}, host=ip)[0] == 403
        assert call(port, "POST", "/api/pause", {"paused": True}, headers={"Cookie": cookie}, host=ip)[0] == 200
    finally:
        app.stopping = True
        server.shutdown()
        server.server_close()

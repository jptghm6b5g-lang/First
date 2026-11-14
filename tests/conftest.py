import io
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from a7v_tether.app import App


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def small_jpeg(color=(200, 120, 40), size=(320, 213)) -> bytes:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return b"\xff\xd8\xff\xdb" + b"\x00" * 64 + b"\xff\xd9"
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _entry(tag, typ, count, value: bytes):
    return (tag, typ, count, value)


def ascii_e(tag, text):
    b = text.encode() + b"\x00"
    return _entry(tag, 2, len(b), b)


def short_e(tag, v):
    return _entry(tag, 3, 1, struct.pack("<H", v))


def long_e(tag, v):
    return _entry(tag, 4, 1, struct.pack("<I", v))


def rat_e(tag, num, den):
    return _entry(tag, 5, 1, struct.pack("<II", num, den))


def build_tiff(ifd0, exif, jpeg=b"", pad=0) -> bytes:
    """Little-endian TIFF with IFD0 -> ExifIFD and an embedded JPEG preview."""

    def ifd_size(entries):
        return 2 + 12 * len(entries) + 4

    # placeholders so sizes are known before offsets are filled in
    ifd0 = list(ifd0) + [long_e(0x8769, 0), long_e(0x0201, 0), long_e(0x0202, len(jpeg))]
    ifd0.sort(key=lambda e: e[0])
    exif = sorted(exif, key=lambda e: e[0])

    def data_len(entries):
        return sum(len(e[3]) for e in entries if len(e[3]) > 4)

    off_ifd0 = 8
    off_exif = off_ifd0 + ifd_size(ifd0) + data_len(ifd0)
    off_jpeg = off_exif + ifd_size(exif) + data_len(exif) + pad
    ifd0 = [long_e(t, off_exif) if t == 0x8769 else long_e(t, off_jpeg) if t == 0x0201 else (t, ty, c, v)
            for (t, ty, c, v) in ifd0]

    def pack(entries, start):
        data_off = start + ifd_size(entries)
        head, data = struct.pack("<H", len(entries)), b""
        for tag, typ, count, value in entries:
            if len(value) <= 4:
                head += struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\x00")
            else:
                head += struct.pack("<HHII", tag, typ, count, data_off + len(data))
                data += value
        return head + struct.pack("<I", 0) + data

    out = b"II*\x00" + struct.pack("<I", off_ifd0) + pack(ifd0, off_ifd0) + pack(exif, off_exif)
    out += b"\x00" * pad
    assert len(out) == off_jpeg
    return out + jpeg


def make_arw(model="ILCE-TEST", orientation=1, jpeg=None, raw_bytes=200_000, seed=0) -> bytes:
    jpeg = small_jpeg() if jpeg is None else jpeg
    ifd0 = [ascii_e(0x010F, "SONY"), ascii_e(0x0110, model), short_e(0x0112, orientation)]
    exif = [rat_e(0x829A, 1, 250), rat_e(0x829D, 28, 10), short_e(0x8827, 400),
            ascii_e(0x9003, "2026:11:14 15:30:45"), rat_e(0x920A, 35, 1), ascii_e(0xA434, "FE 35mm F1.4 GM")]
    return build_tiff(ifd0, exif, jpeg) + bytes([seed % 256]) * raw_bytes


def make_jpeg_with_exif(model="ILCE-TEST") -> bytes:
    tiff = build_tiff([ascii_e(0x010F, "SONY"), ascii_e(0x0110, model)],
                      [rat_e(0x829A, 1, 1000), short_e(0x8827, 100)])
    app1 = b"Exif\x00\x00" + tiff
    body = small_jpeg((30, 90, 200))
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + body[2:]


def wait_for(cond, timeout=5.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.fixture
def make_app(tmp_path):
    apps = []

    def factory(start=False, **overrides):
        data = tmp_path / f"data{len(apps)}"
        app = App(data)
        base = dict(
            ftp_host="127.0.0.1", ftp_port=free_port(), ftp_password="secret",
            watch_dir=str(tmp_path / "LR-Watch"), autostart_ftp=False, web_port=free_port(),
            session_name="Test", lightroom_timeout_s=5,
        )
        lo = free_port()
        base.update(passive_port_min=lo, passive_port_max=lo)
        base.update(overrides)
        app.settings.update(base)
        app.store.save(app.settings)
        apps.append(app)
        if start:
            app.start()
        else:
            app.ensure_dirs()
        return app

    yield factory
    for a in apps:
        try:
            a.stop()
        except Exception:
            pass

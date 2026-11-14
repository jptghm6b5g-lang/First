"""Camera simulator: uploads test photos via FTP like the α7 V does.

    python -m a7v_tether.simulate --port 21 --user a7v --password ... --count 10

Sends real JPEG files with EXIF data, so Lightroom imports them normally –
handy for testing the whole chain before the shoot without a camera.
"""

from __future__ import annotations

import argparse
import ftplib
import io
import math
import random
import ssl
import sys
import time
from datetime import datetime

try:
    from PIL import Image, ImageDraw
    from PIL.TiffImagePlugin import IFDRational
except ImportError:  # pragma: no cover
    sys.exit("Der Simulator benötigt Pillow:  pip install Pillow")

LENSES = ["FE 24-70mm F2.8 GM II", "FE 35mm F1.4 GM", "FE 85mm F1.4 GM II", "FE 70-200mm F2.8 GM OSS II"]
SHUTTER = [(1, 60), (1, 125), (1, 250), (1, 500), (1, 1000), (1, 2000)]
APERTURE = [1.4, 1.8, 2.8, 4.0, 5.6, 8.0]
ISO = [100, 200, 400, 800, 1600, 3200]


def make_photo(index: int, size=(3000, 2000), seed=None) -> bytes:
    rnd = random.Random(seed if seed is not None else index)
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    hue = rnd.random()
    top = tuple(int(255 * (0.5 + 0.5 * math.sin(2 * math.pi * (hue + k / 3)))) for k in range(3))
    bottom = tuple(max(0, c // 4) for c in top)
    for y in range(0, h, 4):  # sky gradient
        t = y / h
        draw.rectangle([0, y, w, y + 4], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    for _ in range(4):  # hills
        base = rnd.uniform(0.55, 0.85) * h
        amp = rnd.uniform(0.03, 0.12) * h
        freq = rnd.uniform(1, 3)
        phase = rnd.uniform(0, 6.28)
        shade = tuple(max(0, int(c * rnd.uniform(0.15, 0.45))) for c in top)
        pts = [(x, base + amp * math.sin(freq * x / w * 6.28 + phase)) for x in range(0, w + 50, 50)]
        draw.polygon(pts + [(w, h), (0, h)], fill=shade)
    r = rnd.uniform(0.05, 0.09) * w
    cx, cy = rnd.uniform(0.2, 0.8) * w, rnd.uniform(0.15, 0.4) * h
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 236, 200))
    draw.text((40, h - 60), f"a7v Tether · Testbild {index}", fill=(255, 255, 255))

    exif = Image.Exif()
    now = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    exif[0x010F] = "SONY"
    exif[0x0110] = "a7 V Simulator"
    exif[0x0132] = now
    ifd = exif.get_ifd(0x8769)
    ifd[0x829A] = IFDRational(*rnd.choice(SHUTTER))
    ifd[0x829D] = IFDRational(int(rnd.choice(APERTURE) * 10), 10)
    ifd[0x8827] = rnd.choice(ISO)
    ifd[0x9003] = now
    ifd[0x920A] = IFDRational(rnd.choice([24, 35, 50, 85, 135, 200]), 1)
    ifd[0xA434] = rnd.choice(LENSES)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="a7v-simulate", description="Kamera-Simulator für a7v Tether")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=21)
    p.add_argument("--user", default="a7v")
    p.add_argument("--password", required=True)
    p.add_argument("--count", type=int, default=5, help="Anzahl Bilder")
    p.add_argument("--interval", type=float, default=2.0, help="Sekunden zwischen Bildern")
    p.add_argument("--start", type=int, default=1, help="Erste Bildnummer (DSC0xxxx)")
    p.add_argument("--tls", action="store_true", help="FTPS verwenden")
    p.add_argument("--cafile", help="cacert.pem zum Prüfen des Servers (bei --tls)")
    p.add_argument("--size", default="3000x2000", help="Bildgröße, z. B. 6000x4000")
    args = p.parse_args(argv)
    size = tuple(int(x) for x in args.size.lower().split("x"))

    if args.tls:
        ctx = ssl.create_default_context(cafile=args.cafile) if args.cafile else ssl._create_unverified_context()
        ftp: ftplib.FTP = ftplib.FTP_TLS(context=ctx, timeout=30)
    else:
        ftp = ftplib.FTP(timeout=30)
    ftp.connect(args.host, args.port)
    ftp.login(args.user, args.password)
    if args.tls:
        ftp.prot_p()  # type: ignore[attr-defined]
    ftp.set_pasv(True)
    print(f"Verbunden mit {args.host}:{args.port}")
    try:
        for i in range(args.start, args.start + args.count):
            data = make_photo(i, size)
            name = f"DSC{i:05d}.JPG"
            t0 = time.monotonic()
            ftp.storbinary(f"STOR {name}", io.BytesIO(data))
            print(f"  ↑ {name}  {len(data) / 1e6:.1f} MB  {time.monotonic() - t0:.2f} s")
            if i < args.start + args.count - 1:
                time.sleep(args.interval)
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

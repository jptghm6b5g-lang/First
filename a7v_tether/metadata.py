"""Minimal, dependency-free EXIF and embedded-preview reader for ARW / JPEG.

Sony ARW files are TIFF containers: IFD0 holds the camera model and a pointer
to a ~1616 px JPEG preview, IFD1 a small thumbnail, and the EXIF sub-IFD the
exposure data. JPEG files carry the same TIFF structure inside the APP1
"Exif" segment. Only small parts of the file are read (seek + read), so even
100 MB RAWs are handled in milliseconds.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
MAX_VALUE_BYTES = 1 << 20
MAX_PREVIEW_BYTES = 32 << 20

T_MAKE, T_MODEL, T_ORIENT, T_DATETIME = 0x010F, 0x0110, 0x0112, 0x0132
T_SUBIFDS, T_JPEG_OFF, T_JPEG_LEN, T_EXIF = 0x014A, 0x0201, 0x0202, 0x8769
T_WIDTH, T_HEIGHT = 0x0100, 0x0101
T_EXPOSURE, T_FNUMBER, T_ISO, T_DT_ORIG = 0x829A, 0x829D, 0x8827, 0x9003
T_BIAS, T_FOCAL, T_FOCAL35, T_LENS = 0x9204, 0x920A, 0xA405, 0xA434
T_PIXEL_X, T_PIXEL_Y = 0xA002, 0xA003


@dataclass
class ImageInfo:
    make: str = ""
    model: str = ""
    lens: str = ""
    datetime: str = ""          # "YYYY:MM:DD HH:MM:SS" (EXIF format)
    exposure: str = ""          # "1/250"
    exposure_s: Optional[float] = None
    fnumber: Optional[float] = None
    iso: Optional[int] = None
    focal_mm: Optional[float] = None
    focal35_mm: Optional[int] = None
    bias_ev: Optional[float] = None
    orientation: int = 1
    width: Optional[int] = None
    height: Optional[int] = None
    previews: List[Tuple[int, int]] = field(default_factory=list)  # (offset, length) absolute

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.pop("previews")
        return d

    def summary(self) -> str:
        parts = []
        if self.exposure:
            parts.append(f"{self.exposure} s")
        if self.fnumber:
            parts.append(f"f/{self.fnumber:g}")
        if self.iso:
            parts.append(f"ISO {self.iso}")
        if self.focal_mm:
            parts.append(f"{self.focal_mm:g} mm")
        return " · ".join(parts)


class _Tiff:
    def __init__(self, f: BinaryIO, base: int, size: int) -> None:
        self.f, self.base, self.size = f, base, size
        f.seek(base)
        head = f.read(8)
        if head[:2] == b"II":
            self.e = "<"
        elif head[:2] == b"MM":
            self.e = ">"
        else:
            raise ValueError("kein TIFF")
        magic, self.ifd0 = struct.unpack(self.e + "HI", head[2:8])
        if magic != 42:
            raise ValueError("kein TIFF")

    def read_at(self, offset: int, n: int) -> bytes:
        if offset < 0 or n < 0 or offset + n > self.size:
            raise ValueError("außerhalb der Datei")
        self.f.seek(offset)
        return self.f.read(n)

    def ifd(self, offset: int) -> Tuple[Dict[int, Tuple[int, int, bytes, int]], int]:
        """Return ({tag: (type, count, raw_value_field, field_pos)}, next_ifd)."""
        pos = self.base + offset
        (n,) = struct.unpack(self.e + "H", self.read_at(pos, 2))
        if n > 1000:
            raise ValueError("IFD zu groß")
        raw = self.read_at(pos + 2, n * 12 + 4)
        entries = {}
        for i in range(n):
            tag, typ, count = struct.unpack(self.e + "HHI", raw[i * 12: i * 12 + 8])
            entries[tag] = (typ, count, raw[i * 12 + 8: i * 12 + 12], pos + 2 + i * 12 + 8)
        (nxt,) = struct.unpack(self.e + "I", raw[n * 12: n * 12 + 4])
        return entries, nxt

    def value(self, entry: Tuple[int, int, bytes, int]):
        typ, count, field4, _ = entry
        size = TYPE_SIZES.get(typ)
        if size is None:
            return None
        total = size * count
        if total > MAX_VALUE_BYTES:
            return None
        if total <= 4:
            data = field4[:total]
        else:
            (off,) = struct.unpack(self.e + "I", field4)
            data = self.read_at(self.base + off, total)
        if typ == 2:
            return data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if typ in (1, 6, 7):
            return data
        fmt = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
        if fmt:
            vals = struct.unpack(self.e + fmt * count, data)
        else:  # rationals
            fmt = "II" if typ == 5 else "ii"
            nums = struct.unpack(self.e + fmt * count, data)
            vals = tuple(Fraction(nums[i], nums[i + 1]) if nums[i + 1] else Fraction(0)
                         for i in range(0, len(nums), 2))
        return vals[0] if count == 1 else vals


def _first(v):
    if isinstance(v, tuple):
        return v[0] if v else None
    return v


def _format_exposure(t: Fraction) -> str:
    if t <= 0:
        return ""
    if t >= 1:
        return f"{float(t):g}"
    return f"1/{round(1 / t)}"


def _parse_tiff(f: BinaryIO, base: int, size: int, info: ImageInfo) -> None:
    t = _Tiff(f, base, size)
    seen = set()
    ifds_to_scan: List[int] = []

    # IFD chain (IFD0, IFD1, ...)
    offset, index = t.ifd0, 0
    exif_off = None
    while offset and offset not in seen and index < 8:
        seen.add(offset)
        entries, nxt = t.ifd(offset)
        if index == 0:
            info.make = str(t.value(entries[T_MAKE])) if T_MAKE in entries else ""
            info.model = str(t.value(entries[T_MODEL])) if T_MODEL in entries else ""
            if T_ORIENT in entries:
                info.orientation = int(_first(t.value(entries[T_ORIENT])) or 1)
            if T_DATETIME in entries:
                info.datetime = str(t.value(entries[T_DATETIME]))
            if T_EXIF in entries:
                exif_off = _first(t.value(entries[T_EXIF]))
            if T_SUBIFDS in entries:
                sub = t.value(entries[T_SUBIFDS])
                ifds_to_scan.extend(sub if isinstance(sub, tuple) else [sub])
        _collect_preview(t, entries, info)
        offset, index = nxt, index + 1

    for sub in ifds_to_scan[:8]:
        if sub and sub not in seen:
            seen.add(sub)
            entries, _ = t.ifd(sub)
            _collect_preview(t, entries, info)

    if exif_off:
        entries, _ = t.ifd(exif_off)
        v = lambda tag: t.value(entries[tag]) if tag in entries else None  # noqa: E731
        exp = _first(v(T_EXPOSURE))
        if isinstance(exp, Fraction) and exp > 0:
            info.exposure = _format_exposure(exp)
            info.exposure_s = float(exp)
        fn = _first(v(T_FNUMBER))
        if isinstance(fn, Fraction) and fn > 0:
            info.fnumber = round(float(fn), 1)
        iso = _first(v(T_ISO))
        if isinstance(iso, int) and iso > 0:
            info.iso = iso
        dto = v(T_DT_ORIG)
        if isinstance(dto, str) and dto.strip(": 0"):
            info.datetime = dto
        fl = _first(v(T_FOCAL))
        if isinstance(fl, Fraction) and fl > 0:
            info.focal_mm = round(float(fl), 1)
        f35 = _first(v(T_FOCAL35))
        if isinstance(f35, int) and f35 > 0:
            info.focal35_mm = f35
        bias = _first(v(T_BIAS))
        if isinstance(bias, Fraction):
            info.bias_ev = round(float(bias), 2)
        lens = v(T_LENS)
        if isinstance(lens, str):
            info.lens = lens
        px, py = _first(v(T_PIXEL_X)), _first(v(T_PIXEL_Y))
        if isinstance(px, int) and isinstance(py, int) and px and py:
            info.width, info.height = px, py


def _collect_preview(t: _Tiff, entries, info: ImageInfo) -> None:
    if T_JPEG_OFF in entries and T_JPEG_LEN in entries:
        off = _first(t.value(entries[T_JPEG_OFF]))
        length = _first(t.value(entries[T_JPEG_LEN]))
        if isinstance(off, int) and isinstance(length, int) and 0 < length <= MAX_PREVIEW_BYTES:
            absolute = t.base + off
            if absolute + length <= t.size:
                info.previews.append((absolute, length))


def _find_jpeg_exif(f: BinaryIO, size: int) -> Optional[int]:
    """Return absolute offset of the TIFF header inside a JPEG's APP1 segment."""
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= size:
        f.seek(pos)
        marker = f.read(4)
        if len(marker) < 4 or marker[0] != 0xFF:
            return None
        code = marker[1]
        if code in (0xD9, 0xDA):  # EOI / SOS: no more metadata
            return None
        (length,) = struct.unpack(">H", marker[2:4])
        if code == 0xE1 and f.read(6) == b"Exif\x00\x00":
            return pos + 10
        pos += 2 + length
    return None


def read_info(path: Path) -> ImageInfo:
    """Read metadata; never raises – returns whatever could be parsed."""
    info = ImageInfo()
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(4)
            if head[:2] in (b"II", b"MM"):
                _parse_tiff(f, 0, size, info)
            elif head[:2] == b"\xff\xd8":
                base = _find_jpeg_exif(f, size)
                if base is not None:
                    _parse_tiff(f, base, size, info)
    except (OSError, ValueError, struct.error, ZeroDivisionError):
        pass
    return info


def extract_preview(path: Path, info: Optional[ImageInfo] = None) -> Optional[bytes]:
    """Return the largest embedded JPEG preview (or the JPEG itself)."""
    path = Path(path)
    info = info or read_info(path)
    try:
        with open(path, "rb") as f:
            for offset, length in sorted(info.previews, key=lambda p: -p[1]):
                f.seek(offset)
                data = f.read(length)
                if data[:2] == b"\xff\xd8":
                    return data
            if path.suffix.lower() in (".jpg", ".jpeg"):
                if path.stat().st_size <= MAX_PREVIEW_BYTES:
                    f.seek(0)
                    return f.read()
    except OSError:
        pass
    return None

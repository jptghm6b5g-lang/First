"""Thumbnail / preview cache for the dashboard.

With Pillow installed, previews are resized and rotated correctly. Without it,
the embedded camera preview is stored as-is and the browser rotates it.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import Optional

try:
    from PIL import Image  # type: ignore

    HAVE_PIL = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_PIL = False

log = logging.getLogger(__name__)

THUMB_PX = 480
PREVIEW_PX = 1920
# EXIF orientation -> PIL transpose operations
_ORIENT = {2: [0], 3: [3], 4: [1], 5: [5], 6: [4], 7: [6], 8: [2]}


class ThumbStore:
    def __init__(self, cache_dir: Path, keep_previews: int = 400) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_previews = keep_previews
        self._lock = threading.Lock()

    @property
    def oriented(self) -> bool:
        """True when stored images are already rotated upright."""
        return HAVE_PIL

    def thumb_path(self, transfer_id: int) -> Path:
        return self.dir / f"{transfer_id}_t.jpg"

    def preview_path(self, transfer_id: int) -> Path:
        return self.dir / f"{transfer_id}_p.jpg"

    def store(self, transfer_id: int, jpeg: bytes, orientation: int = 1) -> bool:
        try:
            if HAVE_PIL:
                img = Image.open(io.BytesIO(jpeg))
                img.load()
                for op in _ORIENT.get(orientation, []):
                    img = img.transpose(Image.Transpose(op))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                preview = img.copy()
                preview.thumbnail((PREVIEW_PX, PREVIEW_PX))
                preview.save(self.preview_path(transfer_id), "JPEG", quality=85)
                img.thumbnail((THUMB_PX, THUMB_PX))
                img.save(self.thumb_path(transfer_id), "JPEG", quality=78)
            else:
                self.preview_path(transfer_id).write_bytes(jpeg)
                self.thumb_path(transfer_id).write_bytes(jpeg)
        except Exception as exc:  # broken preview must never break an import
            log.debug("Vorschau fehlgeschlagen für #%s: %s", transfer_id, exc)
            return False
        self._prune(transfer_id)
        return True

    def _prune(self, newest_id: int) -> None:
        cutoff = newest_id - self.keep_previews
        if cutoff <= 0 or newest_id % 25:
            return
        with self._lock:
            for p in self.dir.glob("*_p.jpg"):
                try:
                    if int(p.name.split("_")[0]) < cutoff:
                        p.unlink()
                except (ValueError, OSError):
                    pass

    def get(self, transfer_id: int, large: bool = False) -> Optional[Path]:
        p = self.preview_path(transfer_id) if large else self.thumb_path(transfer_id)
        if p.is_file():
            return p
        if large:
            t = self.thumb_path(transfer_id)
            return t if t.is_file() else None
        return None

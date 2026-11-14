"""Import pipeline: received file  ->  (backup)  ->  checks  ->  Lightroom watch folder.

Lightroom's "Auto Import" picks up every supported file that appears in the
watched folder, so a file must appear there *complete* and in one step:

* same filesystem  -> a single atomic ``os.replace``
* other filesystem -> copy to ``<name>.part`` (ignored by Lightroom), fsync,
  then atomically rename to the final name.
"""

from __future__ import annotations

import collections
import errno
import hashlib
import logging
import os
import queue
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import Settings, render_pattern
from .events import EventBus
from .history import History
from .metadata import ImageInfo, extract_preview, read_info
from .thumbs import ThumbStore

log = logging.getLogger(__name__)

PART_SUFFIX = ".part"
HELD_DIR = "_Wartend"
DUP_DIR = "_Duplikate"
METADATA_EXT = {"arw", "jpg", "jpeg"}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------- helpers ----

def sanitize(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip(" .")
    return name[:180] or "Bild"


def unique_path(target: Path) -> Path:
    """Return ``target`` or ``stem_1.ext``, ``stem_2.ext`` ... if taken."""

    def taken(p: Path) -> bool:
        return p.exists() or p.with_name(p.name + PART_SUFFIX).exists()

    if not taken(target):
        return target
    counter = 1
    while True:
        candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        if not taken(candidate):
            return candidate
        counter += 1


def _copy_then_rename(src: Path, dest: Path, keep_src: bool = False) -> None:
    tmp = dest.with_name(dest.name + PART_SUFFIX)
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=4 * 1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
        shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not keep_src:
        src.unlink()


def move_atomic(src: Path, dest: Path) -> None:
    """Move ``src`` to ``dest`` so that ``dest`` appears complete in one step."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_then_rename(src, dest)


def copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_then_rename(src, dest, keep_src=True)


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def shot_datetime(info: ImageInfo) -> datetime:
    try:
        return datetime.strptime(info.datetime[:19], "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.now()


# ------------------------------------------------------ Lightroom watcher ----

class LightroomWatcher:
    """Notices when Lightroom moves a file out of the watched folder."""

    def __init__(self, history: History, bus: EventBus) -> None:
        self.history, self.bus = history, bus
        self.timeout_s = 30
        self._pending: Dict[int, Tuple[Path, float, bool]] = {}
        self._lock = threading.Lock()
        self.last_pickup: Optional[float] = None
        self.last_delay_s: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lr-watch", daemon=True)

    def start(self) -> "LightroomWatcher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def track(self, transfer_id: int, path: Path) -> None:
        with self._lock:
            self._pending[transfer_id] = (path, time.time(), False)

    def state(self) -> Dict:
        now = time.time()
        with self._lock:
            pending = list(self._pending.values())
        overdue = sum(1 for _, t0, _w in pending if now - t0 > self.timeout_s)
        if overdue:
            status = "stalled"
        elif pending:
            status = "waiting"
        elif self.last_pickup:
            status = "ok"
        else:
            status = "unknown"
        return {
            "status": status,
            "pending": len(pending),
            "overdue": overdue,
            "last_pickup": self.last_pickup,
            "last_delay_s": self.last_delay_s,
        }

    def check_once(self) -> None:
        now = time.time()
        with self._lock:
            items = list(self._pending.items())
        for tid, (path, t0, warned) in items:
            if not path.exists():
                with self._lock:
                    self._pending.pop(tid, None)
                self.last_pickup, self.last_delay_s = now, now - t0
                self.history.update(tid, lr_picked_up=now)
                self.bus.publish("lightroom", {"id": tid, "picked_up": True, "delay_s": now - t0})
            elif not warned and now - t0 > self.timeout_s:
                with self._lock:
                    if tid in self._pending:
                        self._pending[tid] = (path, t0, True)
                log.warning(
                    "Lightroom hat %s nach %d s noch nicht übernommen – läuft Lightroom und ist "
                    "„Automatischen Import aktivieren“ eingeschaltet?", path.name, self.timeout_s)
                self.bus.publish("lightroom", {"id": tid, "picked_up": False})

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.check_once()
            except Exception:  # pragma: no cover
                log.exception("Lightroom-Überwachung")


# ---------------------------------------------------------------- importer ----

@dataclass
class Upload:
    path: Path
    camera_ip: str = ""
    upload_s: Optional[float] = None
    received_at: float = field(default_factory=time.time)


class Importer:
    def __init__(self, settings: Settings, history: History, thumbs: ThumbStore,
                 bus: EventBus, watcher: LightroomWatcher) -> None:
        self.settings = settings
        self.history, self.thumbs, self.bus, self.watcher = history, thumbs, bus, watcher
        self.paused = False
        self._queue: "queue.Queue[Optional[Upload]]" = queue.Queue()
        self._pairs: "collections.OrderedDict[Tuple[str, str], str]" = collections.OrderedDict()
        self._held_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="importer", daemon=True)

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> "Importer":
        self._thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def wait_idle(self) -> None:
        self._queue.join()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def submit(self, upload: Upload) -> None:
        self._queue.put(upload)

    def new_session(self, name: str) -> None:
        self._pairs.clear()
        self.settings.session_name = name

    # -- pause / resume ---------------------------------------------------------
    def set_paused(self, paused: bool) -> int:
        self.paused = paused
        released = 0
        if not paused:
            released = self.release_held()
        self.bus.publish("paused", {"paused": paused, "released": released})
        log.info("Übergabe an Lightroom %s", "pausiert – Bilder werden gesammelt" if paused
                 else f"fortgesetzt ({released} wartende Bilder)")
        return released

    def release_held(self) -> int:
        with self._held_lock:
            held = self.history.list(limit=500, status="held")["items"]
            count = 0
            for rec in reversed(held):
                src = Path(rec["final_path"])
                if not src.is_file():
                    self.history.update(rec["id"], status="failed", message="Wartende Datei fehlt")
                    continue
                try:
                    self._deliver(rec["id"], src, rec["final_name"])
                    count += 1
                except OSError as exc:
                    self.history.update(rec["id"], status="failed", message=str(exc))
                    log.error("Übergabe fehlgeschlagen: %s (%s)", src.name, exc)
                self._publish(rec["id"])
            return count

    # -- core ----------------------------------------------------------------------
    def _final_stem(self, src: Path, info: ImageInfo, s: Settings, when: datetime) -> str:
        if not s.rename_enabled:
            return src.stem
        key = (s.session_name, src.stem.upper())
        if key in self._pairs:          # RAW + JPEG of one shot share a name
            return self._pairs[key]
        seq = self.history.session_seq(s.session_name) + 1
        stem = sanitize(render_pattern(s.rename_pattern, self._tokens(src, info, s, when, seq)))
        self._pairs[key] = stem
        while len(self._pairs) > 1000:
            self._pairs.popitem(last=False)
        return stem

    @staticmethod
    def _tokens(src: Path, info: ImageInfo, s: Settings, when: datetime, seq: int) -> Dict[str, str]:
        return {
            "orig": src.stem,
            "seq": f"{seq:04d}",
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H%M%S"),
            "session": s.session_name,
            "camera": (info.model or "Kamera").replace(" ", ""),
        }

    def _backup(self, src: Path, final_name: str, info: ImageInfo, s: Settings, when: datetime) -> str:
        root = s.backup_path
        if not root:
            return ""
        sub = render_pattern(s.backup_structure or "", self._tokens(src, info, s, when, 0))
        parts = [sanitize(p) for p in re.split(r"[\\/]+", sub) if p.strip(" .")]
        dest = unique_path(root.joinpath(*parts, final_name))
        copy_atomic(src, dest)
        return str(dest)

    def _deliver(self, tid: int, src: Path, final_name: str) -> Path:
        s = self.settings
        dest = unique_path(s.watch_path / final_name)
        move_atomic(src, dest)
        self.history.update(tid, status="imported", final_name=dest.name, final_path=str(dest), message="")
        self.watcher.timeout_s = s.lightroom_timeout_s
        self.watcher.track(tid, dest)
        return dest

    def _publish(self, tid: int) -> None:
        rec = self.history.get(tid)
        if rec:
            self.bus.publish("transfer", rec)

    def process(self, up: Upload) -> Optional[int]:
        """Handle one upload synchronously; returns the history id."""
        s = self.settings
        src = Path(up.path)
        if not src.is_file():
            log.warning("Datei verschwunden, übersprungen: %s", src)
            return None
        t_start = time.monotonic()
        ext = src.suffix.lower().lstrip(".")
        size = src.stat().st_size
        info = read_info(src) if ext in METADATA_EXT else ImageInfo()
        preview = extract_preview(src, info) if ext in METADATA_EXT else None
        when = shot_datetime(info)
        tid = self.history.add(
            received_at=up.received_at, session=s.session_name, camera_ip=up.camera_ip,
            original_name=src.name, ext=ext, size=size, upload_s=up.upload_s,
            status="processing", exif=info.to_dict(),
        )
        try:
            final_name = self._final_stem(src, info, s, when) + src.suffix
            backup = self._backup(src, final_name, info, s, when)
            if backup:
                self.history.update(tid, backup_path=backup)

            if ext not in s.extensions:
                self.history.update(tid, status="ignored", final_name=final_name, final_path=str(src),
                                    message="Dateityp nicht für Lightroom ausgewählt")
                log.info("Nicht an Lightroom übergeben (Dateityp): %s", src.name)
            else:
                digest = sha1_of(src)
                self.history.update(tid, sha1=digest)
                dup = self.history.find_imported_sha1(digest) if s.duplicate_policy == "skip" else None
                if dup and dup["id"] != tid:
                    target = unique_path(s.staging_path / DUP_DIR / src.name)
                    move_atomic(src, target)
                    self.history.update(tid, status="duplicate", final_path=str(target),
                                        message=f"Identisch mit {dup['final_name'] or dup['original_name']}")
                    log.info("Duplikat übersprungen: %s", src.name)
                elif self.paused:
                    target = unique_path(s.staging_path / HELD_DIR / final_name)
                    move_atomic(src, target)
                    self.history.update(tid, status="held", final_name=target.name, final_path=str(target))
                    log.info("Gesammelt (Pause): %s", final_name)
                else:
                    dest = self._deliver(tid, src, final_name)
                    log.info("→ Lightroom: %s (%.1f MB%s, %.2f s Verarbeitung)",
                             dest.name if dest.name == src.name else f"{src.name} → {dest.name}",
                             size / 1e6,
                             f", Upload {up.upload_s:.1f} s" if up.upload_s else "",
                             time.monotonic() - t_start)
        except Exception as exc:
            self.history.update(tid, status="failed", message=str(exc))
            log.error("Import fehlgeschlagen: %s (%s)", src.name, exc)
        if preview and self.thumbs.store(tid, preview, info.orientation):
            self.history.update(tid, has_thumb=1)
        self._publish(tid)
        return tid

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self.process(item)
            except Exception:  # keep the worker alive whatever happens
                log.exception("Unerwarteter Fehler beim Import: %s", item)
            finally:
                self._queue.task_done()

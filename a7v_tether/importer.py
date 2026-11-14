"""Moves received files into the folder watched by Lightroom Classic.

Lightroom's "Auto Import" picks up every supported file that appears in the
watched folder. A file must therefore show up there *complete* and in one step,
never while it is still being written. The importer guarantees that:

* same filesystem  -> a single atomic ``os.replace``
* other filesystem -> copy to ``<name>.part`` (ignored by Lightroom), flush,
  then atomically rename to the final name.

Work is done on a background thread so large RAW files never block the FTP
server while it is receiving the next shot.
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

PART_SUFFIX = ".part"


def unique_path(target: Path) -> Path:
    """Return ``target`` or ``stem_1.ext``, ``stem_2.ext`` ... if it exists."""
    if not target.exists() and not target.with_name(target.name + PART_SUFFIX).exists():
        return target
    counter = 1
    while True:
        candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        if not candidate.exists() and not candidate.with_name(candidate.name + PART_SUFFIX).exists():
            return candidate
        counter += 1


def _copy_then_rename(src: Path, dest: Path) -> None:
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
    src.unlink()


def move_atomic(src: Path, dest: Path) -> None:
    """Move ``src`` to ``dest`` so ``dest`` appears complete in one step."""
    try:
        os.replace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_then_rename(src, dest)


class Importer:
    """Queue of finished uploads that are handed over to Lightroom."""

    def __init__(
        self,
        watch_dir: Path,
        extensions: Optional[Iterable[str]] = None,
        backup_dir: Optional[Path] = None,
        on_imported: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.watch_dir = Path(watch_dir)
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.extensions = (
            {e.lower().lstrip(".") for e in extensions} if extensions else None
        )
        self.on_imported = on_imported
        self.imported_count = 0
        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="importer", daemon=True)

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        if self.backup_dir:
            self.backup_dir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "Importer":
        self._thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def wait_idle(self) -> None:
        """Block until every queued file has been processed (used by tests)."""
        self._queue.join()

    # -- public API --------------------------------------------------------
    def submit(self, path: Path) -> None:
        self._queue.put(Path(path))

    def accepts(self, path: Path) -> bool:
        if self.extensions is None:
            return True
        return path.suffix.lower().lstrip(".") in self.extensions

    def process(self, src: Path) -> Optional[Path]:
        """Import one file synchronously. Returns the final path or None."""
        if not src.is_file():
            log.warning("Datei verschwunden, übersprungen: %s", src)
            return None
        if not self.accepts(src):
            log.info("Dateityp nicht ausgewählt, bleibt im Eingangsordner: %s", src.name)
            return None

        size_mb = src.stat().st_size / (1024 * 1024)

        if self.backup_dir:
            backup = unique_path(self.backup_dir / src.name)
            shutil.copy2(src, backup)

        dest = unique_path(self.watch_dir / src.name)
        start = time.monotonic()
        move_atomic(src, dest)
        self.imported_count += 1
        log.info(
            "→ Lightroom: %s (%.1f MB, %.2f s)  [#%d]",
            dest.name,
            size_mb,
            time.monotonic() - start,
            self.imported_count,
        )
        if self.on_imported:
            self.on_imported(dest)
        return dest

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self.process(item)
            except Exception:  # keep the worker alive whatever happens
                log.exception("Import fehlgeschlagen: %s", item)
            finally:
                self._queue.task_done()

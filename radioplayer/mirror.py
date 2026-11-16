"""Keep a full local copy of the music share and play from that.

With the mirror enabled the engine never reads the share directly, so a NAS
or network outage has no effect on air; the mirror just stops updating until
the share is back.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .emergency import AUDIO_EXTENSIONS

log = logging.getLogger(__name__)

_sync_lock = threading.Lock()


@dataclass
class SyncResult:
    copied: int = 0
    deleted: int = 0
    failed: int = 0
    total: int = 0
    deletes_skipped: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.deleted)


def _scan(root: Path) -> tuple[dict[str, tuple[int, float]], bool]:
    """Audio files under root as {relative path: (size, mtime)}, and whether the walk was clean."""
    files: dict[str, tuple[int, float]] = {}
    clean = True

    def on_error(_err):
        nonlocal clean
        clean = False

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                clean = False
                continue
            files[str(full.relative_to(root))] = (st.st_size, st.st_mtime)
    return files, clean


def sync(cfg: Config, force: bool = False) -> SyncResult | None:
    """Make the mirror match the share. Returns None if the share is unusable.

    Never empties the mirror because the share looks empty or half-readable:
    deletions are skipped if the share could not be read cleanly, or (without
    force) if they would remove more than half of the mirror.
    """
    source = Path(cfg["music"]["root"])
    dest = cfg.mirror_dir
    with _sync_lock:
        if not source.is_dir():
            log.warning("Mirror: share %s is not available; keeping the current mirror", source)
            return None
        wanted, clean = _scan(source)
        if not wanted:
            log.warning("Mirror: no audio files found in %s; keeping the current mirror", source)
            return None
        dest.mkdir(parents=True, exist_ok=True)
        have, _ = _scan(dest)
        result = SyncResult(total=len(wanted))

        for rel, (size, mtime) in wanted.items():
            old = have.get(rel)
            if old and old[0] == size and abs(old[1] - mtime) < 2:
                continue
            target = dest / rel
            tmp = target.with_name(f".{target.name}.part")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / rel, tmp)
                os.utime(tmp, (mtime, mtime))
                tmp.replace(target)
                result.copied += 1
            except OSError as e:
                log.warning("Mirror: could not copy %s: %s", rel, e)
                result.failed += 1
                clean = False
                try:
                    tmp.unlink()
                except OSError:
                    pass

        extra = [rel for rel in have if rel not in wanted]
        if extra and not clean:
            log.warning("Mirror: the share was not fully readable; not deleting %d file(s) this time", len(extra))
            result.deletes_skipped = True
        elif extra and not force and len(extra) > len(have) / 2:
            log.warning("Mirror: %d of %d local files are no longer on the share; not deleting them. "
                        "If that is intended, run: radioplayer sync --force", len(extra), len(have))
            result.deletes_skipped = True
        else:
            for rel in extra:
                try:
                    (dest / rel).unlink()
                    result.deleted += 1
                except OSError as e:
                    log.warning("Mirror: could not delete %s: %s", rel, e)
            _remove_empty_dirs(dest)

        if result.changed or result.failed:
            log.info("Mirror: %d files on the share, %d copied, %d deleted, %d failed",
                     result.total, result.copied, result.deleted, result.failed)
        return result


def _remove_empty_dirs(root: Path) -> None:
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            path.rmdir()  # only succeeds when empty
        except OSError:
            pass

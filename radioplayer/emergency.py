"""Keep a local copy of some music on the SD card for when the share is down."""

from __future__ import annotations

import logging
import os
import random
import shutil
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"}


def _audio_files(root: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in AUDIO_EXTENSIONS and not name.startswith("."):
                files.append(Path(dirpath) / name)
    return files


def refresh(cfg: Config, max_mb: int) -> int:
    """Copy a random selection of the library (up to max_mb) into the emergency folder.

    New files are copied before old ones are removed, so the folder is never
    empty mid-refresh. Returns the number of files in the folder afterwards.
    """
    root = Path(cfg["music"]["root"])
    dest = Path(cfg["music"]["emergency_dir"])
    dest.mkdir(parents=True, exist_ok=True)

    candidates = _audio_files(root)
    if not candidates:
        log.error("No audio files found under %s; leaving emergency folder unchanged", root)
        return len(_audio_files(dest))
    random.shuffle(candidates)

    budget = max_mb * 1024 * 1024
    chosen: dict[str, Path] = {}
    total = 0
    for f in candidates:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if total + size > budget:
            continue
        # Flatten into one folder; prefix with the parent to avoid name clashes.
        name = f"{f.parent.name}__{f.name}" if f.parent != root else f.name
        if name in chosen:
            continue
        chosen[name] = f
        total += size

    copied = 0
    for name, src in chosen.items():
        target = dest / name
        if target.exists():
            continue
        tmp = dest / f".{name}.part"
        try:
            shutil.copyfile(src, tmp)
            tmp.replace(target)
            copied += 1
        except OSError as e:
            log.warning("Could not copy %s: %s", src, e)
            tmp.unlink(missing_ok=True)

    removed = 0
    for existing in dest.iterdir():
        if existing.is_file() and existing.name not in chosen and not existing.name.startswith("."):
            existing.unlink()
            removed += 1

    log.info("Emergency folder: %d files (%.0f MB), %d copied, %d removed",
             len(chosen), total / 1024 / 1024, copied, removed)
    return len(chosen)

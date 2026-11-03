"""Build the playlist list and write the settings file Liquidsoap includes."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from pathlib import Path

from .config import Config, Playlist

log = logging.getLogger(__name__)

SCAN_TIMEOUT_SECONDS = 20


def _scan_top_level(root: Path, exclude: set[str]) -> list[str]:
    names = []
    with os.scandir(root) as it:
        for entry in it:
            if entry.name.startswith(".") or entry.name.lower() in exclude:
                continue
            if entry.is_dir(follow_symlinks=True):
                names.append(entry.name)
    return sorted(names, key=str.lower)


def scan_folders(cfg: Config) -> list[str] | None:
    """Top-level folders of the music share, or None if the share is unreachable.

    Runs in a worker thread with a timeout so a hung SMB server cannot block
    startup. (The worker may linger; the process moves on regardless.)
    """
    root = Path(cfg["music"]["root"])
    exclude = {e.lower() for e in cfg["music"]["exclude"] or []}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_scan_top_level, root, exclude).result(timeout=SCAN_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        log.warning("Timed out scanning %s", root)
    except OSError as e:
        log.warning("Cannot scan %s: %s", root, e)
    finally:
        pool.shutdown(wait=False)
    return None


def resolve_playlists(cfg: Config, scanned: list[str] | None = None, use_scan: bool = True) -> list[Playlist]:
    """Explicit playlists, then one per top-level folder, then "All Music"."""
    music = cfg["music"]
    root = Path(music["root"])

    def abspath(folder: str) -> str:
        p = Path(folder)
        return str(p if p.is_absolute() else root / p)

    playlists = [Playlist(p.name, [abspath(f) for f in p.folders]) for p in cfg.explicit_playlists]
    taken = {p.name.lower() for p in playlists}

    if music["auto_playlists"]:
        folders = scanned
        if folders is None and use_scan:
            folders = scan_folders(cfg)
            if folders is not None:
                _write_json(cfg.playlist_cache, folders)
        if folders is None:
            folders = _read_json(cfg.playlist_cache, default=[])
            if folders:
                log.warning("Share unavailable; using cached folder list (%d folders)", len(folders))
        for name in folders:
            if "#{" in name or name.lower() in taken:
                continue
            playlists.append(Playlist(name, [str(root / name)]))
            taken.add(name.lower())

    all_name = music["all_playlist_name"]
    if all_name and all_name.lower() not in taken:
        playlists.append(Playlist(all_name, [str(root)]))

    if not playlists:
        # Never hand Liquidsoap an empty list.
        playlists.append(Playlist("All Music", [str(root)]))
    return playlists


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("Cannot write %s: %s", path, e)


def read_state(cfg: Config) -> dict:
    state = _read_json(cfg.state_file, default={})
    return state if isinstance(state, dict) else {}


# Pre-emphasis curve -> (zero time constant in seconds, k) for the emphasis
# filter in radio.liq. Fitted at 44.1 kHz so the one-pole discrete filter
# tracks the analog 1 + jw*tau curve within 1 dB from 200 Hz to 15 kHz
# (the plain values undershoot by ~2 dB near 15 kHz).
EMPHASIS_FIT = {
    0: (0.0, 0.0),
    50: (47.7e-6, 80.0),
    75: (76.7e-6, 80.0),
}


# --- Liquidsoap literal helpers ------------------------------------------

def liq_str(s: str) -> str:
    # JSON string escaping is valid Liquidsoap string syntax; validate_name
    # already rejected "#{" so nothing gets interpolated.
    return json.dumps(str(s), ensure_ascii=False)


def liq_float(x) -> str:
    # Liquidsoap has no exponent notation (1e-05), so always write decimals.
    text = repr(float(x))
    if "e" in text or "E" in text:
        text = f"{float(x):.12f}".rstrip("0")
    return text if "." in text else text + ".0"


def liq_bool(b) -> str:
    return "true" if b else "false"


def render(cfg: Config, playlists: list[Playlist], state: dict) -> str:
    music, out, proc = cfg["music"], cfg["output"], cfg["processing"]
    presets = cfg.presets
    names = [p.name for p in playlists]

    initial_playlist = state.get("playlist")
    if initial_playlist not in names:
        initial_playlist = music["default_playlist"] if music["default_playlist"] in names else names[0]

    volume = state.get("volume_db", out["default_volume_db"])
    if not isinstance(volume, (int, float)):
        volume = out["default_volume_db"]
    volume = min(max(volume, out["min_volume_db"]), out["max_volume_db"])

    preset = state.get("preset")
    if preset not in presets:
        preset = proc["default_preset"]

    def pl(p: Playlist) -> str:
        return f"({liq_str(p.name)}, [{', '.join(liq_str(f) for f in p.folders)}])"

    def band(b: dict) -> str:
        return (f"{{threshold={liq_float(b['threshold'])}, ratio={liq_float(b['ratio'])}, "
                f"gain={liq_float(b['gain'])}}}")

    def preset_liq(name: str, p: dict) -> str:
        bands = ", ".join(band(b) for b in p["bands"])
        return (f"  ({liq_str(name)}, {{drive={liq_float(p['drive'])}, bass={liq_float(p['bass'])}, "
                f"presence={liq_float(p['presence'])}, air={liq_float(p['air'])}, "
                f"limiter_drive={liq_float(p['limiter_drive'])}, wideband={band(p['wideband'])}, "
                f"bands=[{bands}]}})")

    xo = proc["crossovers"]
    lines = [
        "# Generated by `radioplayer generate` from config.yaml. Do not edit.",
        f"rp_music_root = {liq_str(music['root'])}",
        f"rp_emergency_dir = {liq_str(music['emergency_dir'])}",
        "rp_playlists = [",
        ",\n".join("  " + pl(p) for p in playlists),
        "]",
        f"rp_initial_playlist = {liq_str(initial_playlist)}",
        f"rp_fallback_to_all = {liq_bool(music['fallback_to_all'])}",
        f"rp_reload_seconds = {int(music['reload_minutes'] * 60)}",
        f"rp_max_blank = {liq_float(music['max_blank_seconds'])}",
        "rp_watchdog_seconds = 10",
        f"rp_output = {liq_str(out['type'])}",
        f"rp_alsa_device = {liq_str(out['device'])}",
        f"rp_output_file = {liq_str(out['file'])}",
        f"rp_initial_volume_db = {liq_float(volume)}",
        f"rp_min_volume_db = {liq_float(out['min_volume_db'])}",
        f"rp_max_volume_db = {liq_float(out['max_volume_db'])}",
        f"rp_ceiling_db = {liq_float(out['ceiling_db'])}",
        f"rp_crossfade = {liq_float(out['crossfade_seconds'])}",
        f"rp_processing_mode = {liq_str(proc['mode'])}",
        f"rp_emphasis_tau = {liq_float(EMPHASIS_FIT.get(proc['preemphasis_us'], (0.0, 0.0))[0])}",
        f"rp_emphasis_k = {liq_float(EMPHASIS_FIT.get(proc['preemphasis_us'], (0.0, 0.0))[1])}",
        f"rp_agc = {liq_bool(proc['agc'])}",
        f"rp_agc_target = {liq_float(proc['agc_target_db'])}",
        f"rp_highpass_hz = {liq_float(proc['highpass_hz'])}",
        f"rp_lowpass_hz = {liq_float(proc['lowpass_hz'])}",
        f"rp_crossovers = {{c1={liq_float(xo[0])}, c2={liq_float(xo[1])}, c3={liq_float(xo[2])}}}",
        f"rp_initial_preset = {liq_str(preset)}",
        "rp_presets = [",
        ",\n".join(preset_liq(n, p) for n, p in presets.items()),
        "]",
        f"rp_http_bind = {liq_str(cfg['http']['bind'])}",
        f"rp_http_port = {int(cfg['http']['port'])}",
        f"rp_state_file = {liq_str(str(cfg.state_file))}",
        "",
    ]
    return "\n".join(lines)


def generate(cfg: Config, use_scan: bool = True) -> list[Playlist]:
    playlists = resolve_playlists(cfg, use_scan=use_scan)
    text = render(cfg, playlists, read_state(cfg))
    target = cfg.generated_liq
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    log.info("Wrote %s with %d playlists: %s", target, len(playlists), ", ".join(p.name for p in playlists))
    return playlists

"""Load and validate /etc/radioplayer/config.yaml."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/etc/radioplayer/config.yaml")


class ConfigError(Exception):
    pass


def _band(threshold: float, ratio: float, gain: float) -> dict:
    return {"threshold": threshold, "ratio": ratio, "gain": gain}


# Band order: low (< c1), low-mid, high-mid, high (> c3).
# The high band gets a lower threshold and higher ratio than the others
# because FM transmitters apply 75 us pre-emphasis (+17 dB at 15 kHz), so
# treble is what overmodulates first.
DEFAULT_PRESETS: dict[str, dict] = {
    "flat": {
        "drive": 0.0, "bass": 0.0, "presence": 0.0, "air": 0.0, "limiter_drive": 0.0,
        "wideband": _band(0.0, 1.0, 0.0),
        "bands": [_band(0.0, 1.0, 0.0)] * 4,
    },
    "light": {
        "drive": 0.0, "bass": 1.5, "presence": 0.5, "air": 0.5, "limiter_drive": 1.0,
        "wideband": _band(-14.0, 2.0, 2.0),
        "bands": [
            _band(-20.0, 2.0, 2.0),
            _band(-20.0, 2.0, 2.0),
            _band(-20.0, 2.0, 2.0),
            _band(-24.0, 2.5, 1.0),
        ],
    },
    "medium": {
        "drive": 2.0, "bass": 2.5, "presence": 1.0, "air": 1.0, "limiter_drive": 3.0,
        "wideband": _band(-16.0, 3.0, 3.0),
        "bands": [
            _band(-24.0, 3.0, 4.0),
            _band(-24.0, 3.0, 4.0),
            _band(-24.0, 3.0, 4.0),
            _band(-28.0, 4.0, 2.5),
        ],
    },
    "heavy": {
        "drive": 4.0, "bass": 3.0, "presence": 1.5, "air": 1.5, "limiter_drive": 6.0,
        "wideband": _band(-18.0, 4.0, 4.0),
        "bands": [
            _band(-28.0, 4.0, 6.0),
            _band(-27.0, 4.0, 6.0),
            _band(-27.0, 4.0, 6.0),
            _band(-32.0, 5.0, 4.0),
        ],
    },
}

DEFAULTS: dict[str, Any] = {
    "music": {
        "root": "/mnt/radio",
        "emergency_dir": "/var/lib/radioplayer/emergency",
        "auto_playlists": True,
        "all_playlist_name": "All Music",
        "exclude": [],
        "playlists": [],
        "default_playlist": None,
        "reload_minutes": 60,
        "fallback_to_all": True,
        "max_blank_seconds": 10,
    },
    "output": {
        "type": "alsa",
        "device": "plughw:CARD=Headphones,DEV=0",
        "file": "/var/lib/radioplayer/output.wav",
        "default_volume_db": -6.0,
        "min_volume_db": -40.0,
        "max_volume_db": 0.0,
        "ceiling_db": -1.0,
        "crossfade_seconds": 2.0,
    },
    "processing": {
        "mode": "full",
        "default_preset": "medium",
        "agc": True,
        "agc_target_db": -16.0,
        "highpass_hz": 30.0,
        "lowpass_hz": 15000.0,
        "crossovers": [150.0, 800.0, 5000.0],
        "presets": {},
    },
    "http": {"bind": "127.0.0.1", "port": 8005},
    "mqtt": {
        "host": "homeassistant.local",
        "port": 1883,
        "username": None,
        "password": None,
        "tls": False,
        "client_id": "radioplayer",
        "base_topic": "radioplayer",
        "discovery_prefix": "homeassistant",
        "device_name": "FM Radio Player",
        "poll_seconds": 2.0,
    },
    "paths": {
        "state_dir": "/var/lib/radioplayer",
        "engine_service": "radioplayer-engine.service",
    },
}


@dataclass
class Playlist:
    name: str
    folders: list[str]


@dataclass
class Config:
    raw: dict
    path: Path | None = None
    explicit_playlists: list[Playlist] = field(default_factory=list)

    def __getitem__(self, key: str) -> dict:
        return self.raw[key]

    @property
    def state_dir(self) -> Path:
        return Path(self.raw["paths"]["state_dir"])

    @property
    def generated_liq(self) -> Path:
        return self.state_dir / "generated.liq"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def playlist_cache(self) -> Path:
        return self.state_dir / "playlists.json"

    @property
    def presets(self) -> dict[str, dict]:
        presets = copy.deepcopy(DEFAULT_PRESETS)
        for name, p in (self.raw["processing"].get("presets") or {}).items():
            base = copy.deepcopy(presets.get(name, DEFAULT_PRESETS["flat"]))
            base.update(p)
            presets[name] = base
        return presets


def _merge(defaults: dict, override: dict) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_name(name: str) -> str:
    name = str(name).strip()
    if not name:
        raise ConfigError("playlist name cannot be empty")
    # Liquidsoap interpolates #{...} inside string literals.
    if "#{" in name or "\n" in name:
        raise ConfigError(f"playlist name {name!r} contains unsupported characters")
    return name


def _validate_preset(name: str, p: dict) -> None:
    for key in ("drive", "bass", "presence", "air", "limiter_drive"):
        if not isinstance(p.get(key), (int, float)):
            raise ConfigError(f"preset {name!r}: {key} must be a number")
    wb = p.get("wideband")
    if not isinstance(wb, dict) or not all(isinstance(wb.get(k), (int, float)) for k in ("threshold", "ratio", "gain")):
        raise ConfigError(f"preset {name!r}: wideband needs numeric threshold, ratio and gain")
    if wb["ratio"] < 1:
        raise ConfigError(f"preset {name!r}: wideband ratio must be >= 1")
    if not 0 <= p["limiter_drive"] <= 12:
        raise ConfigError(f"preset {name!r}: limiter_drive must be between 0 and 12 dB")
    bands = p.get("bands")
    if not isinstance(bands, list) or len(bands) != 4:
        raise ConfigError(f"preset {name!r}: bands must be a list of 4 entries")
    for i, b in enumerate(bands):
        for key in ("threshold", "ratio", "gain"):
            if not isinstance(b.get(key), (int, float)):
                raise ConfigError(f"preset {name!r} band {i + 1}: {key} must be a number")
        if b["ratio"] < 1:
            raise ConfigError(f"preset {name!r} band {i + 1}: ratio must be >= 1")


def load(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    raw = _merge(DEFAULTS, user)
    cfg = Config(raw=raw, path=path)

    out = raw["output"]
    if out["type"] not in ("alsa", "dummy", "wav"):
        raise ConfigError("output.type must be 'alsa', 'dummy' or 'wav'")
    if out["max_volume_db"] > 0:
        raise ConfigError("output.max_volume_db must be <= 0 (gain after the limiter would defeat it)")
    if out["min_volume_db"] >= out["max_volume_db"]:
        raise ConfigError("output.min_volume_db must be below max_volume_db")
    if not out["min_volume_db"] <= out["default_volume_db"] <= out["max_volume_db"]:
        raise ConfigError("output.default_volume_db must be between min_volume_db and max_volume_db")
    if not -20 <= out["ceiling_db"] <= 0:
        raise ConfigError("output.ceiling_db must be between -20 and 0")

    proc = raw["processing"]
    if proc["mode"] not in ("full", "lite"):
        raise ConfigError("processing.mode must be 'full' or 'lite'")
    xo = proc["crossovers"]
    if len(xo) != 3 or not all(a < b for a, b in zip(xo, xo[1:])):
        raise ConfigError("processing.crossovers must be 3 increasing frequencies")
    if not 0 < proc["highpass_hz"] < xo[0]:
        raise ConfigError("processing.highpass_hz must be below the first crossover")
    if not xo[2] < proc["lowpass_hz"] <= 18000:
        raise ConfigError("processing.lowpass_hz must be above the last crossover and <= 18000 (19 kHz pilot)")
    presets = cfg.presets
    for name, p in presets.items():
        validate_name(name)
        _validate_preset(name, p)
    if proc["default_preset"] not in presets:
        raise ConfigError(f"processing.default_preset {proc['default_preset']!r} is not a defined preset")

    music = raw["music"]
    seen = set()
    for entry in music["playlists"] or []:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ConfigError("each music.playlists entry needs a name")
        name = validate_name(entry["name"])
        folders = entry.get("folders") or ([entry["folder"]] if entry.get("folder") else [])
        if not folders:
            raise ConfigError(f"playlist {name!r} needs 'folder' or 'folders'")
        if name.lower() in seen:
            raise ConfigError(f"duplicate playlist name {name!r}")
        seen.add(name.lower())
        cfg.explicit_playlists.append(Playlist(name, [str(f) for f in folders]))

    return cfg

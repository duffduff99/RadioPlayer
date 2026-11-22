"""Load and validate /etc/radioplayer/config.yaml."""

from __future__ import annotations

import copy
import os
import platform
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

# Measured transmitter chains. Each sets processing.* values; anything set in
# config.yaml or the environment wins.
MODULATOR_PROFILES: dict[str, dict] = {
    # Blonder Tongue ZFMSM in stereo mode, fed by an AB13X USB sound card.
    # Measured 2026-11-17 with an SDR on the coax (tools/make_test_signal.py,
    # no de-emphasis): a resonance of about +14 dB near 100 Hz with heavy
    # distortion of 40-70 Hz tones, a midrange dip of about 3 dB near 500 Hz,
    # and pre-emphasis of about 25 us rather than the 75 us US radios undo.
    # The filters make it measure within 1.5 dB from 80 Hz to 10 kHz as heard
    # on a radio with 75 us de-emphasis. The bass resonance moved with the
    # sound card, so re-measure if the sound card changes.
    "zfmsm-stereo": {
        "preemphasis_us": 25,
        "highpass_hz": 60.0,
        "correction": [
            {"freq": 110.0, "gain": -13.3, "q": 2.62},
            {"freq": 455.0, "gain": 4.0, "q": 2.72},
            {"freq": 8860.0, "gain": 7.2, "q": 0.31},
        ],
    },
    # The same ZFMSM in mono mode, AB13X USB sound card through an RCA
    # ground loop isolator. Measured 2026-11-22 the same way. In mono mode
    # the modulator uses only its LEFT input (a right-only tone did not come
    # through at all), so this profile also sends the mono mix to both
    # outputs. The main channel has the same ~+12 dB resonance near 115 Hz
    # and dip near 475 Hz as stereo mode, and rolls the treble off sooner.
    # The filters make it measure within 2.4 dB from 100 Hz to 10 kHz on a
    # radio with 75 us de-emphasis. The treble boost is capped at 9 dB (at
    # 9 kHz), so above 10 kHz it still falls off (-9 dB at 12.5 kHz) rather
    # than feeding more treble into the pre-emphasis limiter.
    "zfmsm-mono": {
        "preemphasis_us": 25,
        "highpass_hz": 60.0,
        "correction": [
            {"freq": 115.0, "gain": -12.6, "q": 2.01},
            {"freq": 475.0, "gain": 5.1, "q": 3.65},
            {"freq": 9000.0, "gain": 9.0, "q": 0.38},
        ],
        "output": {"mono": True},
    },
}

DEFAULTS: dict[str, Any] = {
    "music": {
        "root": "/mnt/radio",
        "emergency_dir": "/var/lib/radioplayer/emergency",
        "emergency_refresh_days": 7,
        "emergency_max_mb": 2000,
        "mirror": {
            "enabled": False,
            "dir": "/var/lib/radioplayer/library",
            "interval_minutes": 15,
        },
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
        "alsa_buffer_seconds": None,
        # Send the same mono mix (left + right) to both outputs, e.g. for a
        # modulator in mono mode that only uses one of its inputs.
        "mono": False,
        "default_volume_db": -6.0,
        "min_volume_db": -40.0,
        "max_volume_db": 0.0,
        "ceiling_db": -1.0,
        "crossfade_seconds": 2.0,
    },
    "processing": {
        "mode": "full",
        "default_preset": "medium",
        # Named set of measured corrections for a specific modulator (and the
        # sound card feeding it); see MODULATOR_PROFILES. Its values apply
        # unless config.yaml or the environment sets them.
        "modulator": None,
        # Fixed correction filters for the transmitter chain, applied in every
        # mode before the limiter: [{freq: Hz, gain: dB, q: width}, ...].
        "correction": [],
        "preemphasis_us": 75,
        # How song levels are evened out:
        #   track: each song at one fixed gain from its ReplayGain (tags, or
        #          measured when the song loads); no level riding inside a song
        #   agc:   slow automatic gain control that rides the level
        #   none:  songs play at their mastered level
        #   auto:  track on PCs/servers, agc on ARM boards (measuring every
        #          song costs too much CPU on a Raspberry Pi)
        "leveling": "auto",
        # Largest boost track leveling may give a quiet song, and an offset
        # added to every song's gain.
        "track_gain_max_db": 6.0,
        "track_gain_offset_db": 0.0,
        "agc": True,
        "agc_target_db": -16.0,
        "highpass_hz": 30.0,
        "lowpass_hz": 15000.0,
        "crossovers": [150.0, 800.0, 5000.0],
        "presets": {},
    },
    "engine": {"low_cpu": "auto"},
    # bind: where the engine's control API listens. host: where the bridge
    # and CLI reach it (default: the bind address, or localhost).
    "http": {"bind": "127.0.0.1", "port": 8005, "host": None},
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
    def mirror_enabled(self) -> bool:
        return bool(self.raw["music"]["mirror"]["enabled"])

    @property
    def mirror_dir(self) -> Path:
        return Path(self.raw["music"]["mirror"]["dir"])

    @property
    def play_root(self) -> Path:
        """Where the engine reads music: the local mirror if enabled, else the share."""
        return self.mirror_dir if self.mirror_enabled else Path(self.raw["music"]["root"])

    @property
    def leveling(self) -> str:
        proc = self.raw["processing"]
        value = proc["leveling"]
        if value == "auto":
            if not proc["agc"]:
                return "none"  # older configs turned the AGC off with agc: false
            return "agc" if self.low_cpu else "track"
        return value

    @property
    def low_cpu(self) -> bool:
        """Engine profile. auto: on for ARM boards (Raspberry Pi), off for PCs/servers."""
        value = self.raw["engine"]["low_cpu"]
        if value == "auto":
            return platform.machine().lower().startswith(("arm", "aarch"))
        return bool(value)

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


ENV_PREFIX = "RADIOPLAYER__"
# Settings that are text even when they look like a number or a boolean.
_ENV_ALWAYS_TEXT = {("mqtt", "username"), ("mqtt", "password")}


def _apply_env(raw: dict, environ) -> None:
    """Override settings from the environment, on top of config.yaml.

    RADIOPLAYER__MQTT__HOST=broker sets mqtt.host; nested keys use more double
    underscores (RADIOPLAYER__MUSIC__MIRROR__ENABLED=true). Empty values are
    ignored, so a compose file can pass variables that may be unset.
    """
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX) or value == "":
            continue
        keys = tuple(k.lower() for k in name[len(ENV_PREFIX):].split("__"))
        node = raw
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict) or keys[-1] not in node or isinstance(node[keys[-1]], (dict, list)):
            raise ConfigError(f"{name}: no such setting {'.'.join(keys)}")
        current = node[keys[-1]]
        if keys in _ENV_ALWAYS_TEXT or isinstance(current, str):
            node[keys[-1]] = value
        else:
            try:
                node[keys[-1]] = yaml.safe_load(value)
            except yaml.YAMLError:
                raise ConfigError(f"{name}: cannot parse {value!r}") from None


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
    _apply_env(raw, os.environ)
    profile = raw["processing"]["modulator"]
    if profile:
        if profile not in MODULATOR_PROFILES:
            raise ConfigError(f"processing.modulator {profile!r} is not one of: "
                              f"{', '.join(MODULATOR_PROFILES)}")
        prof = dict(MODULATOR_PROFILES[profile])
        # A profile's "output" entry sets output settings (e.g. mono); the
        # rest are processing settings.
        base = _merge(DEFAULTS, {"processing": prof, "output": prof.pop("output", {})})
        raw = _merge(base, user)
        _apply_env(raw, os.environ)
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
    if out["alsa_buffer_seconds"] is not None and not 0.1 <= out["alsa_buffer_seconds"] <= 5:
        raise ConfigError("output.alsa_buffer_seconds must be between 0.1 and 5")
    if raw["engine"]["low_cpu"] not in (True, False, "auto"):
        raise ConfigError("engine.low_cpu must be true, false or auto")
    mirror = raw["music"]["mirror"]
    if not isinstance(mirror.get("enabled"), bool):
        raise ConfigError("music.mirror.enabled must be true or false")
    if mirror["enabled"] and Path(mirror["dir"]) == Path(raw["music"]["root"]):
        raise ConfigError("music.mirror.dir must differ from music.root")
    if not mirror["interval_minutes"] >= 1:
        raise ConfigError("music.mirror.interval_minutes must be at least 1")
    if not -20 <= out["ceiling_db"] <= 0:
        raise ConfigError("output.ceiling_db must be between -20 and 0")

    proc = raw["processing"]
    if proc["leveling"] not in ("auto", "track", "agc", "none"):
        raise ConfigError("processing.leveling must be auto, track, agc or none")
    if not 0 <= proc["track_gain_max_db"] <= 20:
        raise ConfigError("processing.track_gain_max_db must be between 0 and 20")
    if not -20 <= proc["track_gain_offset_db"] <= 20:
        raise ConfigError("processing.track_gain_offset_db must be between -20 and 20")
    if not isinstance(raw["output"]["mono"], bool):
        raise ConfigError("output.mono must be true or false")
    if not isinstance(proc["preemphasis_us"], (int, float)) or not 0 <= proc["preemphasis_us"] <= 150:
        raise ConfigError("processing.preemphasis_us must be a number of microseconds from 0 (off) to 150; "
                          "75 is standard in the Americas, 50 elsewhere")
    corr = proc["correction"]
    if not isinstance(corr, list) or len(corr) > 8:
        raise ConfigError("processing.correction must be a list of at most 8 filters")
    for i, f in enumerate(corr):
        if not isinstance(f, dict) or not all(isinstance(f.get(k), (int, float)) for k in ("freq", "gain", "q")):
            raise ConfigError(f"processing.correction[{i}] needs numeric freq, gain and q")
        if not (20 <= f["freq"] <= 16000 and -24 <= f["gain"] <= 12 and 0.1 <= f["q"] <= 10):
            raise ConfigError(f"processing.correction[{i}]: freq 20-16000 Hz, gain -24 to +12 dB, q 0.1-10")
    if proc["mode"] not in ("full", "lite", "minimal"):
        raise ConfigError("processing.mode must be 'full', 'lite' or 'minimal'")
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

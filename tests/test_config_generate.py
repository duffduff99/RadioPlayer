import json
from pathlib import Path

import pytest
import yaml

from radioplayer import config as config_mod
from radioplayer.bridge import Bridge, is_fallback, now_playing
from radioplayer.config import ConfigError
from radioplayer.generate import (EMPHASIS_FIT, _parse_generated_names, generate, liq_float, render,
                                  resolve_playlists)

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


def write_config(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return config_mod.load(p)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    for d in ("Rock", "Jazz", "#recycle", ".hidden", "Christmas", "Winter"):
        (root / d).mkdir(parents=True)
    (root / "loose.mp3").write_bytes(b"")
    return root


def base(tmp_path, library, **music):
    return {
        "music": {"root": str(library), **music},
        "paths": {"state_dir": str(tmp_path / "state")},
    }


def test_example_config_is_valid():
    cfg = config_mod.load(EXAMPLE)
    assert cfg["output"]["max_volume_db"] <= 0
    assert set(cfg.presets) >= {"flat", "light", "medium", "heavy"}


def test_auto_playlists_explicit_and_all(tmp_path, library):
    cfg = write_config(tmp_path, base(
        tmp_path, library,
        exclude=["#RECYCLE", "christmas", "winter"],
        playlists=[{"name": "Holiday Mix", "folders": ["Christmas", "Winter"]},
                   {"name": "Rock", "folder": "/elsewhere/rock"}],
    ))
    pls = resolve_playlists(cfg)
    names = [p.name for p in pls]
    # Explicit first, explicit "Rock" wins over the auto one, hidden and
    # excluded folders skipped, "All Music" last.
    assert names == ["Holiday Mix", "Rock", "Jazz", "All Music"]
    assert pls[0].folders == [str(library / "Christmas"), str(library / "Winter")]
    assert pls[1].folders == ["/elsewhere/rock"]
    assert pls[-1].folders == [str(library)]


def test_unreachable_share_uses_cache_then_all(tmp_path, library):
    cfg = write_config(tmp_path, base(tmp_path, library))
    first = [p.name for p in resolve_playlists(cfg)]
    assert "Rock" in first
    # Share gone: the cached folder list keeps the same playlists.
    cfg.raw["music"]["root"] = str(tmp_path / "missing")
    assert [p.name for p in resolve_playlists(cfg)] == first
    # No cache either: still never empty.
    cfg.playlist_cache.unlink()
    assert [p.name for p in resolve_playlists(cfg)] == ["All Music"]
    cfg.raw["music"]["all_playlist_name"] = None
    assert [p.name for p in resolve_playlists(cfg)] == ["All Music"]


@pytest.mark.parametrize("bad, msg", [
    ({"output": {"max_volume_db": 3}}, "max_volume_db"),
    ({"output": {"default_volume_db": -60}}, "default_volume_db"),
    ({"output": {"type": "pulse"}}, "output.type"),
    ({"processing": {"lowpass_hz": 19000}}, "lowpass_hz"),
    ({"processing": {"crossovers": [800, 150, 5000]}}, "crossovers"),
    ({"processing": {"default_preset": False}}, "default_preset"),
    ({"processing": {"mode": "turbo"}}, "processing.mode"),
    ({"processing": {"preemphasis_us": 60}}, "preemphasis_us"),
    ({"processing": {"presets": {"x": {"bands": []}}}}, "bands"),
    ({"processing": {"presets": {"x": {"limiter_drive": 20}}}}, "limiter_drive"),
    ({"music": {"playlists": [{"name": "A #{x}", "folder": "a"}]}}, "unsupported"),
    ({"music": {"playlists": [{"name": "A"}]}}, "folder"),
    ({"music": {"playlists": [{"name": "A", "folder": "a"}, {"name": "a", "folder": "b"}]}}, "duplicate"),
])
def test_validation(tmp_path, bad, msg):
    with pytest.raises(ConfigError, match=msg):
        write_config(tmp_path, bad)


def test_preset_override_merges_with_default(tmp_path):
    cfg = write_config(tmp_path, {"processing": {"presets": {"medium": {"bass": 0.5}}}})
    assert cfg.presets["medium"]["bass"] == 0.5
    assert cfg.presets["medium"]["drive"] == config_mod.DEFAULT_PRESETS["medium"]["drive"]


def test_render_uses_state_and_clamps(tmp_path, library):
    cfg = write_config(tmp_path, base(tmp_path, library, default_playlist="Jazz"))
    pls = resolve_playlists(cfg)
    text = render(cfg, pls, {"playlist": "Rock", "volume_db": 12, "preset": "heavy"})
    assert 'rp_initial_playlist = "Rock"' in text
    assert "rp_initial_volume_db = 0.0" in text  # clamped to max_volume_db
    assert 'rp_initial_preset = "heavy"' in text
    # Unknown saved values fall back to configured defaults.
    text = render(cfg, pls, {"playlist": "Gone", "volume_db": "loud", "preset": "nope"})
    assert 'rp_initial_playlist = "Jazz"' in text
    assert "rp_initial_volume_db = -6.0" in text
    assert 'rp_initial_preset = "medium"' in text


def test_generated_names_roundtrip(tmp_path, library):
    (library / 'Quote "This" \\ Folder').mkdir()
    cfg = write_config(tmp_path, base(tmp_path, library))
    pls = generate(cfg)
    parsed = _parse_generated_names(cfg.generated_liq.read_text())
    assert [p.name for p in parsed] == [p.name for p in pls]
    assert [p.folders for p in parsed] == [p.folders for p in pls]


def test_now_playing_and_fallback():
    assert now_playing({"artist": "A", "title": "T"}) == "A - T"
    assert now_playing({"title": "T"}) == "T"
    assert now_playing({"filename": "/m/Rock/Song One.mp3"}) == "Song One"
    assert now_playing({}) == ""
    assert is_fallback({"playlist": "Rock", "source": "emergency"})
    assert not is_fallback({"playlist": "Rock", "source": "Rock"})
    assert not is_fallback({"playlist": "Rock", "source": ""})


def test_discovery_payloads(tmp_path):
    cfg = write_config(tmp_path, {"mqtt": {"base_topic": "fm"}})
    bridge = Bridge(cfg)
    status = {"playlists": ["Rock", "Jazz"], "presets": ["flat", "medium"]}
    payloads = bridge.discovery_payloads(status)
    sel = payloads["select/radioplayer/playlist"]
    assert sel["options"] == ["Rock", "Jazz"]
    assert sel["command_topic"] == "fm/playlist/set"
    vol = payloads["number/radioplayer/volume"]
    assert (vol["min"], vol["max"]) == (-40.0, 0.0)
    assert {"topic": "fm/engine"} in vol["availability"]
    ids = [p["unique_id"] for p in payloads.values()]
    assert len(ids) == len(set(ids))
    # Must be serialisable.
    json.dumps(payloads)


def test_liq_float_never_uses_exponents():
    for v, want in [(7.67e-05, "0.0000767"), (0, "0.0"), (-24, "-24.0"), (15000.0, "15000.0"), (-1.5, "-1.5")]:
        assert liq_float(v) == want


@pytest.mark.parametrize("us", [0, 50, 75])
def test_preemphasis_rendered(tmp_path, library, us):
    cfg = write_config(tmp_path, {**base(tmp_path, library), "processing": {"preemphasis_us": us}})
    text = render(cfg, resolve_playlists(cfg), {})
    tau, k = EMPHASIS_FIT[us]
    assert f"rp_emphasis_tau = {liq_float(tau)}" in text
    assert f"rp_emphasis_k = {liq_float(k)}" in text
    assert "e-" not in text.split("rp_emphasis_tau = ")[1].split("\n")[0]


def test_emphasis_fit_tracks_analog_curve():
    """The discrete emphasis filter in radio.liq is within 1 dB of 1 + jw*tau."""
    import numpy as np
    sr, dt = 44100, 1 / 44100
    f = np.linspace(200, 15000, 300)
    z = np.exp(1j * 2 * np.pi * f / sr)
    for us in (50, 75):
        tau_z, k = EMPHASIS_FIT[us]
        a = dt / (tau_z / (1 + k) + dt)
        lp = a / (1 - (1 - a) / z)
        got = 20 * np.log10(abs((1 + k) - k * lp))
        want = 20 * np.log10(abs(1 + 1j * 2 * np.pi * f * us * 1e-6))
        assert np.max(abs(got - want)) < 1.0

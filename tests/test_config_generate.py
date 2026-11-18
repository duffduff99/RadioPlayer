import json
from pathlib import Path

import pytest
import yaml

from radioplayer import config as config_mod
from radioplayer.bridge import Bridge, is_fallback, now_playing
from radioplayer.config import ConfigError
from radioplayer.generate import EMPHASIS_FIT, generate, liq_float, liq_str, render, resolve_playlists

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
    ({"processing": {"preemphasis_us": 200}}, "preemphasis_us"),
    ({"processing": {"modulator": "nope"}}, "not one of"),
    ({"processing": {"correction": [{"freq": 100, "gain": -40, "q": 1}]}}, "correction"),
    ({"processing": {"correction": [{"freq": 100}]}}, "correction"),
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


def test_generated_names_are_escaped(tmp_path, library):
    (library / 'Quote "This" \\ Folder').mkdir()
    cfg = write_config(tmp_path, base(tmp_path, library))
    pls = generate(cfg)
    text = cfg.generated_liq.read_text()
    for p in pls:
        assert liq_str(p.name) in text
    assert '"Quote \\"This\\" \\\\ Folder"' in text


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


def mirror_cfg(tmp_path, source):
    return write_config(tmp_path, {
        "music": {"root": str(source), "mirror": {"enabled": True, "dir": str(tmp_path / "mirror")}},
        "paths": {"state_dir": str(tmp_path / "state")},
    })


def test_mirror_sync_copies_updates_and_deletes(tmp_path):
    from radioplayer.mirror import sync
    src = tmp_path / "share"
    for rel in ("Rock/a.mp3", "Rock/sub/b.flac", "Jazz/c.mp3", "Jazz/cover.jpg", ".hidden/x.mp3"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(rel.encode())
    cfg = mirror_cfg(tmp_path, src)
    dest = cfg.mirror_dir

    r = sync(cfg)
    assert (r.copied, r.deleted, r.total) == (3, 0, 3)
    assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()) == \
        ["Jazz/c.mp3", "Rock/a.mp3", "Rock/sub/b.flac"]
    assert not sync(cfg).changed  # second run: nothing to do

    # Playlists now come from the mirror, not the share.
    assert [p.folders for p in resolve_playlists(cfg)][0] == [str(dest / "Jazz")]

    (src / "Rock/a.mp3").write_bytes(b"new content")
    (src / "Jazz/c.mp3").unlink()
    r = sync(cfg)
    assert (r.copied, r.deleted) == (1, 1)
    assert (dest / "Rock/a.mp3").read_bytes() == b"new content"
    assert not (dest / "Jazz").exists()  # emptied folder removed


def test_mirror_sync_never_empties_the_mirror(tmp_path):
    from radioplayer.mirror import sync
    src = tmp_path / "share"
    for i in range(4):
        (src / "Rock").mkdir(parents=True, exist_ok=True)
        (src / f"Rock/{i}.mp3").write_bytes(b"x")
    cfg = mirror_cfg(tmp_path, src)
    sync(cfg)
    count = lambda: len(list(cfg.mirror_dir.rglob("*.mp3")))  # noqa: E731
    assert count() == 4

    # Share looks empty (e.g. unmounted): nothing happens.
    for f in (src / "Rock").iterdir():
        f.rename(tmp_path / f.name)
    assert sync(cfg) is None and count() == 4

    # Share missing entirely.
    (src / "Rock").rmdir()
    src.rmdir()
    assert sync(cfg) is None and count() == 4

    # Most of the library gone: deletions need --force.
    (src / "Pop").mkdir(parents=True)
    (src / "Pop/only.mp3").write_bytes(b"y")
    r = sync(cfg)
    assert r.deletes_skipped and r.copied == 1 and count() == 5
    r = sync(cfg, force=True)
    assert r.deleted == 4 and count() == 1


def test_engine_profiles(tmp_path, library):
    for low_cpu, frame in ((True, "0.2"), (False, "0.05")):
        cfg = write_config(tmp_path, {**base(tmp_path, library), "engine": {"low_cpu": low_cpu}})
        text = render(cfg, resolve_playlists(cfg), {})
        assert f"rp_frame_seconds = {frame}" in text
    cfg = write_config(tmp_path, {**base(tmp_path, library), "engine": {"low_cpu": False},
                                  "output": {"alsa_buffer_seconds": 2}})
    assert "rp_alsa_buffer = 2.0" in render(cfg, resolve_playlists(cfg), {})
    with pytest.raises(ConfigError, match="low_cpu"):
        write_config(tmp_path, {"engine": {"low_cpu": "fast"}})


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIOPLAYER__MQTT__HOST", "broker.lan")
    monkeypatch.setenv("RADIOPLAYER__MQTT__PORT", "8883")
    monkeypatch.setenv("RADIOPLAYER__MQTT__PASSWORD", "12345")       # stays text
    monkeypatch.setenv("RADIOPLAYER__MQTT__USERNAME", "")            # empty: ignored
    monkeypatch.setenv("RADIOPLAYER__MUSIC__MIRROR__ENABLED", "true")
    monkeypatch.setenv("RADIOPLAYER__HTTP__HOST", "engine")
    cfg = write_config(tmp_path, {"mqtt": {"host": "from-file", "username": "fileuser"}})
    assert cfg["mqtt"]["host"] == "broker.lan"
    assert cfg["mqtt"]["port"] == 8883
    assert cfg["mqtt"]["password"] == "12345"
    assert cfg["mqtt"]["username"] == "fileuser"
    assert cfg.mirror_enabled is True
    from radioplayer.engine import Engine
    assert Engine(cfg).base == "http://engine:8005"


def test_env_override_unknown_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIOPLAYER__MQTT__HOSTNAME", "x")
    with pytest.raises(ConfigError, match="no such setting mqtt.hostname"):
        write_config(tmp_path, {})


def test_modulator_profile(tmp_path, library, monkeypatch):
    from radioplayer.config import MODULATOR_PROFILES
    prof = MODULATOR_PROFILES["zfmsm-stereo"]
    cfg = write_config(tmp_path, {**base(tmp_path, library), "processing": {"modulator": "zfmsm-stereo"}})
    assert cfg["processing"]["preemphasis_us"] == prof["preemphasis_us"]
    assert cfg["processing"]["correction"] == prof["correction"]
    text = render(cfg, resolve_playlists(cfg), {})
    assert "def rp_correction(s) =" in text
    assert text.count("filter.iir.eq.peak(") == len(prof["correction"])
    from radioplayer.generate import peak_gain_db
    makeup = -sum(peak_gain_db(f["freq"], f["gain"], f["q"], 1000.0) for f in prof["correction"])
    assert f"amplify(lin_of_dB({round(makeup, 2)}), s)" in text and makeup < 0
    assert "rp_highpass_hz = 60.0" in text
    # config.yaml beats the profile, the environment beats both.
    cfg = write_config(tmp_path, {**base(tmp_path, library),
                                  "processing": {"modulator": "zfmsm-stereo", "highpass_hz": 40}})
    assert cfg["processing"]["highpass_hz"] == 40
    assert cfg["processing"]["preemphasis_us"] == prof["preemphasis_us"]
    monkeypatch.setenv("RADIOPLAYER__PROCESSING__PREEMPHASIS_US", "75")
    assert write_config(tmp_path, {"processing": {"modulator": "zfmsm-stereo"}})["processing"]["preemphasis_us"] == 75
    # The profile can be chosen from the environment too.
    monkeypatch.delenv("RADIOPLAYER__PROCESSING__PREEMPHASIS_US")
    monkeypatch.setenv("RADIOPLAYER__PROCESSING__MODULATOR", "zfmsm-stereo")
    assert write_config(tmp_path, {})["processing"]["preemphasis_us"] == prof["preemphasis_us"]


def test_no_correction_by_default(tmp_path, library):
    cfg = write_config(tmp_path, base(tmp_path, library))
    text = render(cfg, resolve_playlists(cfg), {})
    assert "def rp_correction(s) =\n  s\nend" in text


@pytest.mark.parametrize("us", [25, 40, 100])
def test_emphasis_fit_any_value(us):
    import numpy as np
    from radioplayer.generate import emphasis_fit
    sr, dt = 44100, 1 / 44100
    f = np.linspace(200, 15000, 300)
    z = np.exp(1j * 2 * np.pi * f / sr)
    tau_z, k = emphasis_fit(us)
    a = dt / (tau_z / (1 + k) + dt)
    got = 20 * np.log10(abs((1 + k) - k * a / (1 - (1 - a) / z)))
    want = 20 * np.log10(abs(1 + 1j * 2 * np.pi * f * us * 1e-6))
    assert np.max(abs(got - want)) < 1.2


def test_peak_gain_matches_scipy():
    import numpy as np
    from scipy.signal import freqz
    from radioplayer.generate import peak_gain_db
    sr, f0, g, q = 44100, 8860, 7.2, 0.31
    w0 = 2 * np.pi * f0 / sr; al = np.sin(w0) / (2 * q); A = 10 ** (g / 40)
    b = [1 + al * A, -2 * np.cos(w0), 1 - al * A]; a = [1 + al / A, -2 * np.cos(w0), 1 - al / A]
    for at in (100, 1000, 8860, 15000):
        _, h = freqz(b, a, worN=[2 * np.pi * at / sr])
        assert abs(peak_gain_db(f0, g, q, at) - 20 * np.log10(abs(h[0]))) < 0.01

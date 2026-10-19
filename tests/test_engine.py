"""Boots the real Liquidsoap engine with dummy output. Skipped without liquidsoap."""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from radioplayer import config as config_mod
from radioplayer.engine import Engine, EngineError
from radioplayer.generate import generate

RADIO_LIQ = Path(__file__).resolve().parent.parent / "liquidsoap" / "radio.liq"

pytestmark = pytest.mark.skipif(
    not (shutil.which("liquidsoap") and shutil.which("ffmpeg")), reason="needs liquidsoap and ffmpeg"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tone(path, freq):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=30",
         "-ac", "2", "-metadata", f"title=Tone {freq}", "-metadata", "artist=Test", str(path)],
        check=True,
    )


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("engine")
    music, state = tmp / "music", tmp / "state"
    for i, d in enumerate(("Rock", "Jazz")):
        (music / d).mkdir(parents=True)
        tone(music / d / f"{d}.mp3", 300 + 100 * i)
    (state / "emergency").mkdir(parents=True)
    tone(state / "emergency" / "e.mp3", 1000)
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "music": {"root": str(music), "emergency_dir": str(state / "emergency"), "default_playlist": "Rock"},
        "output": {"type": "dummy", "max_volume_db": -3.0, "default_volume_db": -6.0},
        "http": {"port": free_port()},
        "paths": {"state_dir": str(state)},
    }))
    cfg = config_mod.load(cfg_path)
    generate(cfg)
    script = tmp / "radio.liq"
    script.write_text(RADIO_LIQ.read_text().replace("/var/lib/radioplayer/generated.liq", str(cfg.generated_liq)))
    log = open(tmp / "engine.log", "w")
    proc = subprocess.Popen(["liquidsoap", str(script)], stdout=log, stderr=subprocess.STDOUT)
    eng = Engine(cfg)
    deadline = time.time() + 90
    while True:
        try:
            if eng.status().get("source"):
                break
        except EngineError:
            pass
        if proc.poll() is not None or time.time() > deadline:
            proc.kill()
            pytest.fail("engine did not start:\n" + (tmp / "engine.log").read_text()[-3000:])
        time.sleep(1)
    yield eng, cfg
    proc.terminate()
    proc.wait(timeout=20)


def test_status(engine):
    eng, _ = engine
    st = eng.status()
    assert st["playlist"] == "Rock"
    assert st["playlists"] == ["Jazz", "Rock", "All Music"]
    assert st["source"] == "Rock"
    assert st["volume_db"] == -6.0


def test_controls(engine):
    eng, cfg = engine
    assert eng.set_volume(10)["volume_db"] == -3.0  # clamped to max_volume_db
    assert eng.set_volume(-100)["volume_db"] == -40.0
    assert eng.set_preset("heavy")["preset"] == "heavy"
    with pytest.raises(EngineError, match="unknown preset"):
        eng.set_preset("nope")
    with pytest.raises(EngineError, match="unknown playlist"):
        eng.set_playlist("Nope")
    assert eng.set_playlist("Jazz")["playlist"] == "Jazz"
    deadline = time.time() + 20
    while eng.status()["source"] != "Jazz":
        assert time.time() < deadline, "switch to Jazz never happened"
        time.sleep(0.5)
    eng.skip()
    eng.reload()
    saved = cfg.state_file.read_text()
    assert '"playlist":"Jazz"' in saved and '"preset":"heavy"' in saved

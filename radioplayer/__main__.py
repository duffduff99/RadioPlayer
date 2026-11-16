"""radioplayer command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config as config_mod
from .config import ConfigError
from .engine import Engine, EngineError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="radioplayer", description="FM radio playout control")
    ap.add_argument("-c", "--config", default=str(config_mod.DEFAULT_CONFIG_PATH), help="config file")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("generate", help="write the Liquidsoap settings file from config.yaml")
    sub.add_parser("bridge", help="run the Home Assistant MQTT bridge")
    sub.add_parser("check", help="validate config.yaml and list playlists")
    sub.add_parser("status", help="show what is playing")
    sub.add_parser("skip", help="skip the current song")
    sub.add_parser("reload", help="re-read all folders now")
    p = sub.add_parser("playlist", help="switch playlist")
    p.add_argument("name")
    p = sub.add_parser("volume", help="set output level in dB (<= max_volume_db)")
    p.add_argument("db", type=float)
    p = sub.add_parser("preset", help="set processing preset")
    p.add_argument("name")
    p = sub.add_parser("tone", help="400 Hz calibration tone at the output ceiling (ON AIR!)")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("signal", help="test signal: tone (same as 'tone on') or sweep, "
                                      "stepped tones for measuring the transmitter (ON AIR!)")
    p.add_argument("mode", choices=["off", "tone", "sweep"])
    p = sub.add_parser("sync", help="update the local library mirror from the share now")
    p.add_argument("--force", action="store_true", help="allow deleting more than half of the mirror")
    p = sub.add_parser("refresh-emergency", help="copy a random part of the library to the SD card")
    p.add_argument("--max-mb", type=int, default=2000)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = config_mod.load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "generate":
        from .generate import generate
        generate(cfg)
        return 0
    if args.cmd == "check":
        from .generate import resolve_playlists
        for pl in resolve_playlists(cfg):
            print(f"{pl.name}: {', '.join(pl.folders)}")
        print(f"presets: {', '.join(cfg.presets)}")
        return 0
    if args.cmd == "bridge":
        from .bridge import main as bridge_main
        bridge_main(cfg)
        return 0
    if args.cmd == "sync":
        from .mirror import sync
        if not cfg.mirror_enabled:
            print("music.mirror.enabled is false in the config", file=sys.stderr)
            return 2
        result = sync(cfg, force=args.force)
        return 0 if result is not None else 1
    if args.cmd == "refresh-emergency":
        from .emergency import refresh
        return 0 if refresh(cfg, args.max_mb) > 0 else 1

    engine = Engine(cfg)
    try:
        if args.cmd == "status":
            result = engine.status()
        elif args.cmd == "skip":
            result = engine.skip()
        elif args.cmd == "reload":
            result = engine.reload()
        elif args.cmd == "playlist":
            result = engine.set_playlist(args.name)
        elif args.cmd == "volume":
            result = engine.set_volume(args.db)
        elif args.cmd == "preset":
            result = engine.set_preset(args.name)
        elif args.cmd == "tone":
            result = engine.signal("tone" if args.state == "on" else "off")
        elif args.cmd == "signal":
            result = engine.signal(args.mode)
        else:
            ap.error(f"unknown command {args.cmd}")
    except EngineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

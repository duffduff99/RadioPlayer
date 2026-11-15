"""MQTT bridge: exposes the engine to Home Assistant via MQTT discovery."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path, PurePath

import paho.mqtt.client as mqtt

from . import __version__
from .config import Config
from .engine import Engine, EngineError
from . import emergency
from .generate import resolve_playlists

log = logging.getLogger(__name__)


def now_playing(status: dict) -> str:
    title, artist = status.get("title") or "", status.get("artist") or ""
    if title and artist:
        return f"{artist} - {title}"
    if title:
        return title
    filename = status.get("filename") or ""
    return PurePath(filename).stem if filename else ""


# After a playlist change the old song keeps playing through the crossfade,
# so only report a fallback once the mismatch has lasted this long.
FALLBACK_GRACE_SECONDS = 20


def is_fallback(status: dict) -> bool:
    """True when we are not playing the playlist that was asked for."""
    source = status.get("source") or ""
    return bool(source) and source != status.get("playlist")


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        m = cfg["mqtt"]
        self.base = m["base_topic"].rstrip("/")
        self.prefix = m["discovery_prefix"].rstrip("/")
        self.node = "".join(c if c.isalnum() else "_" for c in m["client_id"]).strip("_") or "radioplayer"
        self.engine = Engine(cfg)
        self.status: dict | None = None
        self.last_published: str | None = None
        self.discovered_options: tuple | None = None
        self.engine_online: bool | None = None
        self.mismatch_since: float | None = None
        self.stop = threading.Event()
        self.lock = threading.Lock()

        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=m["client_id"])
        except AttributeError:  # paho-mqtt < 2.0
            self.client = mqtt.Client(client_id=m["client_id"])
        if m["username"]:
            self.client.username_pw_set(m["username"], m["password"])
        if m["tls"]:
            self.client.tls_set()
        self.client.will_set(self.t("availability"), "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def t(self, suffix: str) -> str:
        return f"{self.base}/{suffix}"

    # --- discovery ----------------------------------------------------------

    def _device(self) -> dict:
        return {
            "identifiers": [self.node],
            "name": self.cfg["mqtt"]["device_name"],
            "manufacturer": "RadioPlayer",
            "model": "Liquidsoap FM playout",
            "sw_version": __version__,
        }

    def _common(self, key: str, name: str) -> dict:
        return {
            "name": name,
            "unique_id": f"{self.node}_{key}",
            "device": self._device(),
            "availability": [
                {"topic": self.t("availability")},
                {"topic": self.t("engine")},
            ],
            "availability_mode": "all",
        }

    def discovery_payloads(self, status: dict) -> dict[str, dict]:
        out = self.cfg["output"]
        state = self.t("state")
        p = {}
        p[f"select/{self.node}/playlist"] = {
            **self._common("playlist", "Playlist"),
            "icon": "mdi:playlist-music",
            "command_topic": self.t("playlist/set"),
            "state_topic": state,
            "value_template": "{{ value_json.playlist }}",
            "options": status.get("playlists") or [],
        }
        p[f"select/{self.node}/processing"] = {
            **self._common("processing", "Processing"),
            "icon": "mdi:tune-vertical",
            "command_topic": self.t("preset/set"),
            "state_topic": state,
            "value_template": "{{ value_json.preset }}",
            "options": status.get("presets") or [],
        }
        p[f"number/{self.node}/volume"] = {
            **self._common("volume", "Output level"),
            "icon": "mdi:volume-high",
            "command_topic": self.t("volume/set"),
            "state_topic": state,
            "value_template": "{{ value_json.volume_db }}",
            "min": out["min_volume_db"],
            "max": out["max_volume_db"],
            "step": 0.5,
            "unit_of_measurement": "dB",
            "mode": "slider",
        }
        p[f"button/{self.node}/skip"] = {
            **self._common("skip", "Skip song"),
            "icon": "mdi:skip-next",
            "command_topic": self.t("skip/press"),
        }
        p[f"button/{self.node}/rescan"] = {
            **self._common("rescan", "Rescan music library"),
            "icon": "mdi:folder-refresh",
            "command_topic": self.t("rescan/press"),
            "entity_category": "config",
        }
        p[f"sensor/{self.node}/now_playing"] = {
            **self._common("now_playing", "Now playing"),
            "icon": "mdi:music",
            "state_topic": state,
            "value_template": "{{ value_json.now_playing }}",
            "json_attributes_topic": state,
            "json_attributes_template": (
                "{{ {'title': value_json.title, 'artist': value_json.artist, 'album': value_json.album,"
                " 'filename': value_json.filename, 'source': value_json.source} | tojson }}"
            ),
        }
        p[f"sensor/{self.node}/source"] = {
            **self._common("source", "Playing from"),
            "icon": "mdi:source-branch",
            "state_topic": state,
            "value_template": "{{ value_json.source }}",
            "entity_category": "diagnostic",
        }
        p[f"binary_sensor/{self.node}/fallback"] = {
            **self._common("fallback", "Playing fallback"),
            "device_class": "problem",
            "state_topic": state,
            "value_template": "{{ 'ON' if value_json.fallback else 'OFF' }}",
            "entity_category": "diagnostic",
        }
        return p

    def publish_discovery(self, status: dict) -> None:
        for path, payload in self.discovery_payloads(status).items():
            self.client.publish(f"{self.prefix}/{path}/config", json.dumps(payload), qos=1, retain=True)
        self.discovered_options = (tuple(status.get("playlists") or []), tuple(status.get("presets") or []))
        log.info("Published Home Assistant discovery (%d playlists)", len(status.get("playlists") or []))

    # --- MQTT callbacks -----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0):
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("Connected to MQTT broker")
        client.subscribe([(self.t(s), 1) for s in ("playlist/set", "preset/set", "volume/set", "skip/press", "rescan/press")])
        client.subscribe(f"{self.prefix}/status", 1)
        client.publish(self.t("availability"), "online", qos=1, retain=True)
        self.engine_online = None
        self.last_published = None
        if self.status:
            self.publish_discovery(self.status)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        topic = msg.topic
        # Run commands off the network thread; rescan can take a while.
        threading.Thread(target=self._handle, args=(topic, payload), daemon=True).start()

    def _handle(self, topic: str, payload: str) -> None:
        try:
            if topic == f"{self.prefix}/status":
                if payload == "online" and self.status:
                    # Home Assistant restarted: resend discovery and state.
                    self.publish_discovery(self.status)
                    self.last_published = None
                return
            log.info("Command %s %r", topic, payload)
            with self.lock:
                if topic == self.t("playlist/set"):
                    self._update(self.engine.set_playlist(payload))
                elif topic == self.t("preset/set"):
                    self._update(self.engine.set_preset(payload))
                elif topic == self.t("volume/set"):
                    self._update(self.engine.set_volume(float(payload)))
                elif topic == self.t("skip/press"):
                    self._update(self.engine.skip())
                elif topic == self.t("rescan/press"):
                    self.rescan()
        except (EngineError, ValueError) as e:
            log.error("Command %s failed: %s", topic, e)
        except Exception:
            log.exception("Command %s failed", topic)

    # --- actions ------------------------------------------------------------

    def rescan(self) -> None:
        """Pick up new files now; restart the engine only if folders changed."""
        wanted = [p.name for p in resolve_playlists(self.cfg)]
        current = (self.status or self.engine.status()).get("playlists") or []
        if wanted != current:
            log.warning("Playlist set changed (%s -> %s); restarting engine. Expect a few seconds of silence.",
                        current, wanted)
            self.engine.restart()
        else:
            self._update(self.engine.reload())

    def emergency_loop(self) -> None:
        """Refresh the local emergency folder every refresh_days (and at once if empty)."""
        music = self.cfg["music"]
        days = float(music["emergency_refresh_days"])
        if days <= 0:
            return
        stamp = self.cfg.state_dir / "emergency.stamp"
        while not self.stop.is_set():
            try:
                age = time.time() - stamp.stat().st_mtime
            except OSError:
                age = float("inf")
            folder = Path(music["emergency_dir"])
            empty = not folder.is_dir() or not any(folder.iterdir())
            if empty or age >= days * 86400:
                try:
                    if emergency.refresh(self.cfg, int(music["emergency_max_mb"])) > 0:
                        stamp.touch()
                except Exception:
                    log.exception("Emergency folder refresh failed")
            # Check again in an hour (retries soon after a failed refresh).
            self.stop.wait(3600)

    # --- state --------------------------------------------------------------

    def _set_engine_online(self, online: bool) -> None:
        if online != self.engine_online:
            self.client.publish(self.t("engine"), "online" if online else "offline", qos=1, retain=True)
            self.engine_online = online
            (log.info if online else log.warning)("Engine %s", "online" if online else "unreachable")

    def _update(self, status: dict) -> None:
        self.status = status
        self._set_engine_online(True)
        options = (tuple(status.get("playlists") or []), tuple(status.get("presets") or []))
        if options != self.discovered_options and self.client.is_connected():
            self.publish_discovery(status)
        state = dict(status)
        state["now_playing"] = now_playing(status)
        if is_fallback(status):
            self.mismatch_since = self.mismatch_since or time.monotonic()
        else:
            self.mismatch_since = None
        state["fallback"] = (self.mismatch_since is not None
                             and time.monotonic() - self.mismatch_since >= FALLBACK_GRACE_SECONDS)
        encoded = json.dumps(state, sort_keys=True)
        if encoded != self.last_published and self.client.is_connected():
            self.client.publish(self.t("state"), encoded, qos=0, retain=True)
            self.last_published = encoded

    def poll_once(self) -> None:
        try:
            status = self.engine.status()
        except EngineError as e:
            log.debug("%s", e)
            self._set_engine_online(False)
            return
        with self.lock:
            self._update(status)

    def run(self) -> None:
        m = self.cfg["mqtt"]
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        self.client.connect_async(m["host"], int(m["port"]), keepalive=30)
        self.client.loop_start()
        threading.Thread(target=self.emergency_loop, name="emergency", daemon=True).start()
        try:
            while not self.stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    log.exception("Status poll failed")
                self.stop.wait(float(m["poll_seconds"]))
        except KeyboardInterrupt:
            pass
        finally:
            log.info("Shutting down")
            try:
                self.client.publish(self.t("availability"), "offline", qos=1, retain=True).wait_for_publish(timeout=3)
            except (RuntimeError, ValueError):
                pass  # not connected; the broker's last-will marks us offline
            self.client.loop_stop()
            self.client.disconnect()


def main(cfg: Config) -> None:
    Bridge(cfg).run()

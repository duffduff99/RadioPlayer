"""Client for the Liquidsoap engine's local HTTP API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import Config


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, cfg: Config, timeout: float = 3.0):
        http = cfg["http"]
        host = "127.0.0.1" if http["bind"] in ("0.0.0.0", "::") else http["bind"]
        self.base = f"http://{host}:{int(http['port'])}"
        self.timeout = timeout

    def _request(self, method: str, path: str, **params) -> dict:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise EngineError(msg) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EngineError(f"engine unreachable at {self.base}: {e}") from None

    def status(self) -> dict:
        return self._request("GET", "/status")

    def skip(self) -> dict:
        return self._request("POST", "/skip")

    def set_playlist(self, name: str) -> dict:
        return self._request("POST", "/playlist", name=name)

    def set_volume(self, db: float) -> dict:
        return self._request("POST", "/volume", db=float(db))

    def set_preset(self, name: str) -> dict:
        return self._request("POST", "/preset", name=name)

    def reload(self) -> dict:
        return self._request("POST", "/reload")

    def restart(self) -> dict:
        return self._request("POST", "/restart")

    def tone(self, on: bool) -> dict:
        return self._request("POST", "/tone", on="1" if on else "0")

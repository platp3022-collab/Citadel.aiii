# -*- coding: utf-8 -*-
"""Тонкий HTTP-клиент на stdlib: json GET/POST, ретраи, вежливый rate limit."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("citadel.dex.http")

UA = "CitadelTrader/0.1 (+https://github.com/platp3022-collab/Citadel.aiii)"


class ApiError(RuntimeError):
    """Ошибка внешнего API после всех повторов."""


class HttpClient:
    def __init__(self, min_interval: float = 0.25, timeout: float = 20.0, retries: int = 3,
                 headers: dict[str, str] | None = None):
        self.min_interval = min_interval        # пауза между запросами, чтобы не ловить 429
        self.timeout = timeout
        self.retries = retries
        self.headers = {"User-Agent": UA, "Accept": "application/json"}
        self.headers.update(headers or {})
        self._last_call = 0.0

    def _wait(self) -> None:
        delta = time.monotonic() - self._last_call
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_call = time.monotonic()

    def get_json(self, url: str, params: dict | None = None) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._request(url, None)

    def post_json(self, url: str, payload: dict) -> dict:
        return self._request(url, json.dumps(payload).encode())

    def _request(self, url: str, data: bytes | None) -> dict:
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
                last = ApiError(f"HTTP {e.code} {url}: {body}")
                if e.code in (429, 500, 502, 503, 504):      # временное — ждём и повторяем
                    time.sleep(2 ** attempt)
                    continue
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = ApiError(f"{type(e).__name__} {url}: {e}")
                time.sleep(2 ** attempt)
        raise last or ApiError(f"не удалось получить {url}")

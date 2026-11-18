"""공용 HTTP 헬퍼 — 정직한 User-Agent(위키미디어 정책), 재시도, 느린 호출 간격."""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

UA = "AutoVideoPipeline/2.0 (educational shorts; https://github.com/kimchi-pancake/auto-video-pipeline)"
_last_call = 0.0


def get(url: str, params: Optional[dict] = None, timeout: int = 25, retries: int = 3, min_gap: float = 0.25) -> requests.Response:
    global _last_call
    err: Exception | None = None
    for attempt in range(retries):
        wait = min_gap - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                err = RuntimeError(f"HTTP {r.status_code}")
                continue
            return r
        except requests.RequestException as e:  # noqa: PERF203
            err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({err})")


def get_json(url: str, params: Optional[dict] = None, **kw) -> Any:
    r = get(url, params, **kw)
    r.raise_for_status()
    return r.json()

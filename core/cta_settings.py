"""
core/cta_settings.py
======================
채널별 구독/좋아요 유도(CTA) 배치 설정. config/cta_settings.json에
{채널명: {"early": bool, "middle": bool, "before_end": bool, "ending": bool}}
형태로 저장합니다. 기본값은 사용자 스펙 권장대로 early만 off.
"""

from __future__ import annotations

import json
from pathlib import Path

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "cta_settings.json"

DEFAULT_SETTINGS = {"early": False, "middle": True, "before_end": True, "ending": True}
POSITIONS = ("early", "middle", "before_end", "ending")


def _load() -> dict:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_settings(channel: str) -> dict:
    data = _load()
    return {**DEFAULT_SETTINGS, **data.get(channel, {})}


def set_setting(channel: str, position: str, enabled: bool) -> dict:
    if position not in POSITIONS:
        raise ValueError(f"알 수 없는 CTA 위치: {position} (가능: {', '.join(POSITIONS)})")
    data = _load()
    settings = {**DEFAULT_SETTINGS, **data.get(channel, {})}
    settings[position] = enabled
    data[channel] = settings
    _save(data)
    return settings

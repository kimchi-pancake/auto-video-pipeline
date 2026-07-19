"""
core/video_registry.py
========================
업로드된 영상을 {video_id, channel, category, title, uploaded_at} 형태로
config/video_registry.json에 append-only로 기록합니다. 나중에 유튜브
조회수/시청 데이터를 수집할 때 "어느 채널의 어떤 소재 카테고리가 잘 되는지"
분석하려면 이 매핑이 있어야 합니다.

analytics_collector.py가 이 레지스트리를 읽어서 각 video_id의 통계를 채워
넣습니다(stats 필드).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REGISTRY_PATH = Path(__file__).parent.parent / "config" / "video_registry.json"


def _load() -> list[dict]:
    if not _REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def record_upload(
    video_id: str,
    channel: str,
    category: Optional[str],
    title: Optional[str],
    is_shorts: bool,
) -> None:
    """업로드 성공 직후 한 건 기록합니다."""
    entries = _load()
    entries.append({
        "video_id": video_id,
        "channel": channel,
        "category": category,
        "title": title,
        "is_shorts": is_shorts,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": None,          # analytics_collector가 나중에 채움
        "stats_collected_at": None,
    })
    _save(entries)


def all_entries() -> list[dict]:
    return _load()


def entries_needing_stats(min_age_hours: int = 24) -> list[dict]:
    """업로드된 지 min_age_hours 이상 지났고 아직 통계를 못 걷은 항목들."""
    now = datetime.now(timezone.utc)
    result = []
    for e in _load():
        if e.get("stats") is not None:
            continue
        try:
            uploaded_at = datetime.fromisoformat(e["uploaded_at"])
        except Exception:
            continue
        age_hours = (now - uploaded_at).total_seconds() / 3600
        if age_hours >= min_age_hours:
            result.append(e)
    return result


def update_stats(video_id: str, stats: dict) -> None:
    entries = _load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = False
    for e in entries:
        if e["video_id"] == video_id:
            e["stats"] = stats
            e["stats_collected_at"] = now
            changed = True
            break
    if changed:
        _save(entries)

"""
core/topic_queue.py
====================
디스코드 봇이 채널별로 예약해두는 주제 큐. config/topic_queue.json에
    {채널명: [{"id": "...", "date": "YYYY-MM-DD"|null, "topic": "..."}, ...]}
형태로 저장합니다. date가 없으면 "다음 실행 때" 소비되고, date가 있으면 그
날짜(놓쳐서 지났어도 다음 실행 때) 소비됩니다.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

_QUEUE_PATH = Path(__file__).parent.parent / "config" / "topic_queue.json"


def _load() -> dict:
    if not _QUEUE_PATH.exists():
        return {}
    try:
        data = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # 예전 스키마({channel: "주제 문자열 하나"})와의 호환
    migrated: dict = {}
    for ch, val in data.items():
        if isinstance(val, str):
            migrated[ch] = [{"id": secrets.token_hex(4), "date": None, "topic": val}]
        elif isinstance(val, list):
            migrated[ch] = val
    return migrated


def _save(data: dict) -> None:
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def push_topic(channel: str, topic: str, date: str | None = None) -> str:
    """채널에 주제를 예약합니다. 예약 항목의 id를 반환합니다."""
    data = _load()
    entries = data.setdefault(channel, [])
    entry_id = secrets.token_hex(4)
    entries.append({"id": entry_id, "date": date, "topic": topic})
    _save(data)
    return entry_id


def pop_topic(channel: str, today: str) -> str | None:
    """오늘(today, "YYYY-MM-DD") 쓸 예약이 있으면 큐에서 꺼내(제거) 반환합니다.
    날짜 지정 예약 중 today 이하(놓친 것 포함)를 우선, 없으면 날짜 미지정
    예약(등록 순) 중 첫 번째를 씁니다. 없으면 None."""
    data = _load()
    entries = data.get(channel, [])
    if not entries:
        return None

    dated = sorted(
        (e for e in entries if e.get("date") and e["date"] <= today),
        key=lambda e: e["date"],
    )
    chosen = dated[0] if dated else next((e for e in entries if not e.get("date")), None)
    if chosen is None:
        return None

    entries.remove(chosen)
    if entries:
        data[channel] = entries
    else:
        data.pop(channel, None)
    _save(data)
    return chosen["topic"]


def list_topics() -> dict:
    """{채널명: [예약 항목, ...]} 전체를 반환합니다."""
    return _load()


def cancel_topic(channel: str, entry_id: str | None = None, date: str | None = None) -> bool:
    """entry_id가 있으면 그 항목만, 없고 date가 있으면 그 날짜 항목, 둘 다
    없으면 날짜 미지정("다음 실행용") 예약 중 가장 먼저 등록된 걸 취소합니다.
    취소했으면 True, 대상이 없으면 False."""
    data = _load()
    entries = data.get(channel, [])
    if not entries:
        return False

    if entry_id:
        target = next((e for e in entries if e["id"] == entry_id), None)
    elif date:
        target = next((e for e in entries if e.get("date") == date), None)
    else:
        target = next((e for e in entries if not e.get("date")), None)

    if target is None:
        return False

    entries.remove(target)
    if entries:
        data[channel] = entries
    else:
        data.pop(channel, None)
    _save(data)
    return True

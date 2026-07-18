"""
core/topic_queue.py
====================
디스코드 봇이 "다음 정기 생성 때 이 주제로 만들어줘"라고 채널별로 예약해두는 큐.
config/topic_queue.json에 {채널명: 주제} 형태로 저장합니다.

평소엔 daily_generate.py가 완전 자동(주제 랜덤 선택)으로 돌지만, 이 큐에 어떤
채널의 예약이 있으면 그날 그 채널의 대본 생성에서만 그 주제를 강제로 씁니다.
한 번 쓰이면 큐에서 빠지고, 다음 날부터는 다시 자동으로 돌아갑니다.
"""

from __future__ import annotations

import json
from pathlib import Path

_QUEUE_PATH = Path(__file__).parent.parent / "config" / "topic_queue.json"


def _load() -> dict:
    if not _QUEUE_PATH.exists():
        return {}
    try:
        return json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_topic(channel_name: str) -> str | None:
    """채널에 예약된 주제가 있으면 큐에서 꺼내(제거하고) 반환합니다. 없으면 None."""
    data = _load()
    topic = data.pop(channel_name, None)
    if topic is not None:
        _save(data)
    return topic


def push_topic(channel_name: str, topic: str) -> None:
    """채널에 주제를 예약합니다. 이미 예약된 게 있으면 덮어씁니다."""
    data = _load()
    data[channel_name] = topic
    _save(data)

"""
core/daily_queue.py
====================
scripts/prepare_daily.py(대본 생성 전용)와 scripts/daily_generate.py(조립
전용)가 서로 다른 GitHub Actions 잡/시각에 실행되면서 story.txt를 주고받기
위한 공유 큐 디렉터리. input/*.txt는 .gitignore로 제외되는 임시 작업
디렉터리라 잡이 끝나면 사라지므로, 잡 경계를 넘겨 전달해야 하는 대본은
반드시 이 경로(git으로 커밋됨)에 둡니다(2026-08-04).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

from utils.file_utils import sanitize_filename

ROOT = Path(__file__).parent.parent
QUEUE_ROOT = ROOT / "queue" / "pending_scripts"


def queue_dir_for_channel(channel_name: str) -> Path:
    d = QUEUE_ROOT / sanitize_filename(channel_name, 100)
    d.mkdir(parents=True, exist_ok=True)
    return d


def pull_queue_into_input_dir(channel_name: str, input_dir: Path) -> List[Path]:
    """queue/pending_scripts/{channel}/*.txt 를 로컬 input_dir로 옮깁니다
    (복사가 아니라 이동 — 큐에서 즉시 비워져야 워크플로우의 커밋 스텝이 "이번에
    가져다 쓴 대본"을 삭제로 인식해서 재처리 방지가 됩니다). 옮겨진 목적지
    경로 목록을 반환합니다."""
    queue_dir = queue_dir_for_channel(channel_name)
    input_dir.mkdir(parents=True, exist_ok=True)
    moved: List[Path] = []
    for f in sorted(queue_dir.glob("*.txt")):
        dest = input_dir / f.name
        shutil.move(str(f), str(dest))
        moved.append(dest)
    return moved

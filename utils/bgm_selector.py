"""
utils/bgm_selector.py
======================
story.txt의 BGM: 에 후보 목록이 여러 개 적혀 있을 때(또는 아예 비어 있을 때),
제목/대사/장면 프롬프트에 등장하는 키워드를 세어 분위기에 가장 잘 맞는
BGM 파일을 그 후보들 중에서 자동으로 고릅니다.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from parser.story_parser import StoryData


def select_bgm(
    story: StoryData,
    moods: Dict[str, List[str]],
    default_bgm: str = "",
    candidates: Optional[List[str]] = None,
) -> str:
    """대본 전체 텍스트에서 무드별 키워드 등장 횟수를 세어 가장 점수가 높은 BGM 파일명을 반환합니다.

    candidates 가 주어지면(= story.txt의 BGM: 에 여러 후보가 적힌 경우) 그 목록 안에서만 고르고,
    없으면 config에 등록된 무드 전체를 후보로 씁니다. 아무 키워드도 매칭되지 않으면
    (candidates 안에 있는) default_bgm 또는 candidates의 첫 항목을 반환합니다.
    """
    if candidates:
        moods = {f: kws for f, kws in moods.items() if f in candidates}
        if default_bgm not in candidates:
            default_bgm = candidates[0]

    text_parts: List[str] = [story.raw_title or ""]
    for scene in story.scenes:
        text_parts.append(scene.prompt or "")
        for dial in scene.dialogues:
            text_parts.append(dial.text)
    full_text = " ".join(text_parts)

    best_file = default_bgm
    best_score = 0
    for filename, keywords in moods.items():
        score = sum(full_text.count(kw) for kw in keywords)
        if score > best_score:
            best_score = score
            best_file = filename

    return best_file

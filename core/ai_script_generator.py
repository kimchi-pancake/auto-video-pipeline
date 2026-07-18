"""
core/ai_script_generator.py
============================
Claude API를 직접 호출해서 대본을 생성하고 story.txt로 저장합니다.
gui/claude_panel.py의 수동(웹뷰에 붙여넣고 복사해오는) 플로우와 완전히 별개의,
API 키 기반 자동 생성 경로입니다.

API 키는 프로젝트 루트의 .env 파일(ANTHROPIC_API_KEY=...)에서 읽습니다.

generate_daily_batch()가 채널당 하루치 물량(기본 롱폼 1개 + 쇼츠 2개)을 한 번에
뽑습니다 — 콤보 호출(롱폼+쇼츠 동시 생성) 1번으로 롱폼 1개 + 쇼츠 1개를 만들고,
나머지 쇼츠 1개는 쇼츠 전용 호출로 채웁니다.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from core.script_prompts import SHORTS_SCRIPT_PROMPT, SPLIT_DELIMITER, combo_script_prompt
from utils.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent / ".env"

# 하루 여러 편씩 대량으로 뽑는 용도라 비용 우선으로 Haiku를 씁니다.
MODEL = "claude-haiku-4-5"

# ProgressCallback(done, total) — generate_daily_batch()가 API 호출 하나 끝날
# 때마다 부릅니다. GUI에서 진행 상태 표시에 씁니다.
ProgressCallback = Callable[[int, int], None]


class ScriptGenerationError(Exception):
    """API 키 누락, 네트워크 오류, 거부 응답 등 생성 실패 시 던집니다."""


def _get_api_key() -> str:
    load_dotenv(_ENV_PATH)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ScriptGenerationError(
            f".env 파일에 API 키가 없습니다.\n{_ENV_PATH}\n"
            "파일을 열어서 ANTHROPIC_API_KEY= 뒤에 콘솔에서 발급받은 키를 붙여넣으세요."
        )
    return key


def _call_claude(prompt: str) -> str:
    """Claude API를 한 번 호출해서 응답 원문을 반환합니다. 생성 함수들의 공통 경로."""
    import anthropic  # 지연 임포트: API 키 미설정 상태에서도 이 모듈 자체는 import 가능하게

    key = _get_api_key()
    client = anthropic.Anthropic(api_key=key)

    try:
        # Haiku 4.5는 adaptive thinking을 지원 안 해서(400 에러) thinking 파라미터
        # 자체를 아예 안 보냅니다 — Opus/Sonnet류로 모델을 바꾸면 다시 켜도 됨.
        with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise ScriptGenerationError("API 키가 유효하지 않습니다. .env 파일의 키를 다시 확인하세요.") from e
    except anthropic.RateLimitError as e:
        raise ScriptGenerationError("요청 한도를 초과했습니다. 잠시 후 다시 시도하세요.") from e
    except anthropic.APIConnectionError as e:
        raise ScriptGenerationError("네트워크 연결에 실패했습니다. 인터넷 연결을 확인하세요.") from e
    except anthropic.APIStatusError as e:
        raise ScriptGenerationError(f"API 오류 (상태코드 {e.status_code}): {e.message}") from e

    if message.stop_reason == "refusal":
        raise ScriptGenerationError("Claude가 이 요청을 안전 정책상 거부했습니다. 다시 시도해보세요.")

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise ScriptGenerationError("빈 응답을 받았습니다. 다시 시도해주세요.")

    usage = message.usage
    logger.info(
        "Claude API 대본 생성 완료 (model=%s, input=%s, output=%s tokens)",
        MODEL, usage.input_tokens, usage.output_tokens,
    )
    return text


def generate_combo_script(custom_topic: str | None = None) -> str:
    """Claude API를 호출해서 롱폼+쇼츠 통합 대본 원문을 반환합니다.
    custom_topic을 주면 주제 풀 대신 그 주제로 강제합니다 (디스코드 봇 등)."""
    logger.info("Claude API 콤보(롱폼+쇼츠) 대본 생성 요청 시작 (topic=%s)", custom_topic or "자동")
    return _call_claude(combo_script_prompt(custom_topic))


def generate_shorts_only() -> str:
    """Claude API를 호출해서 쇼츠 대본 원문 하나만 반환합니다."""
    logger.info("Claude API 쇼츠 단독 대본 생성 요청 시작")
    return _call_claude(SHORTS_SCRIPT_PROMPT)


def save_combo_script(text: str, input_dir: Path) -> list[Path]:
    """콤보 응답을 롱폼/쇼츠로 쪼개서 story.txt로 저장하고 저장된 경로 목록을 반환합니다."""
    input_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved: list[Path] = []

    if SPLIT_DELIMITER in text:
        long_part, _, shorts_part = text.partition(SPLIT_DELIMITER)
        long_part = long_part.strip()
        shorts_part = shorts_part.strip()
        if long_part:
            saved.append(save_story(input_dir, f"claude_{ts}_long.txt", long_part))
        if shorts_part:
            saved.append(save_story(input_dir, f"claude_{ts}_shorts.txt", shorts_part))
    else:
        saved.append(save_story(input_dir, f"claude_{ts}.txt", text))

    if not saved:
        raise ScriptGenerationError("응답에서 저장할 내용을 찾지 못했습니다.")
    return saved


def save_story(input_dir: Path, base_filename: str, content: str) -> Path:
    """파일명이 겹치면 뒤에 번호를 붙여가며 저장합니다. (수동 가져오기와 동일 로직)"""
    dest = input_dir / base_filename
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = input_dir / f"{stem}_{n}{suffix}"
        n += 1
    dest.write_text(content, encoding="utf-8")
    return dest


def generate_and_save(input_dir: Path, custom_topic: str | None = None) -> list[Path]:
    """콤보 1회분 생성부터 저장까지. GUI 워커 스레드에서 이 함수 하나만 부르면 됨."""
    text = generate_combo_script(custom_topic)
    return save_combo_script(text, input_dir)


def generate_daily_batch(
    input_dir: Path,
    long_count: int = 1,
    extra_shorts_count: int = 1,
    progress_cb: Optional[ProgressCallback] = None,
    custom_topic: str | None = None,
) -> list[Path]:
    """
    채널 하루치 물량을 생성합니다. 기본값(long_count=1, extra_shorts_count=1)이면
    콤보 호출 1번(롱폼 1개 + 쇼츠 1개) + 쇼츠 단독 호출 1번 = 롱폼 1개 + 쇼츠 2개
    (하루 3개).
    custom_topic이 있으면 콤보 호출(롱폼+쇼츠 1세트)에만 그 주제를 강제로 씁니다 —
    디스코드로 예약된 주제가 있을 때 그날의 메인 영상에 반영하는 용도입니다.
    실패한 개별 호출은 건너뛰고 계속 진행하며, 하나도 성공 못 하면 예외를 던집니다.
    """
    total = long_count + extra_shorts_count
    done = 0
    saved: list[Path] = []
    errors: list[str] = []

    for i in range(long_count):
        try:
            saved.extend(generate_and_save(input_dir, custom_topic=custom_topic))
        except ScriptGenerationError as e:
            logger.warning("일괄 생성 중 콤보 %d/%d 실패: %s", i + 1, long_count, e)
            errors.append(str(e))
        done += 1
        if progress_cb:
            progress_cb(done, total)

    for i in range(extra_shorts_count):
        try:
            text = generate_shorts_only()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved.append(save_story(input_dir, f"claude_{ts}_shorts.txt", text))
        except ScriptGenerationError as e:
            logger.warning("일괄 생성 중 쇼츠 %d/%d 실패: %s", i + 1, extra_shorts_count, e)
            errors.append(str(e))
        done += 1
        if progress_cb:
            progress_cb(done, total)

    if not saved:
        raise ScriptGenerationError("전부 실패했습니다:\n" + "\n".join(errors))
    if errors:
        logger.warning("일괄 생성 부분 실패 (%d개 성공, %d개 실패)", len(saved), len(errors))
    return saved

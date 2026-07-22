"""
core/ai_script_generator.py
============================
Claude API를 직접 호출해서 대본을 생성하고 story.txt로 저장합니다.
gui/claude_panel.py의 수동(웹뷰에 붙여넣고 복사해오는) 플로우와 완전히 별개의,
API 키 기반 자동 생성 경로입니다.

API 키는 프로젝트 루트의 .env 파일(ANTHROPIC_API_KEY=...)에서 읽습니다.

전체 생성 파이프라인(generate_and_save):
  주제 선정(또는 custom_topic 그대로 사용)
    → 제목 후보 10개 생성 + 자체 평가 → 최고 점수 제목 선택
    → 그 제목을 강제한 채 콤보(롱폼+쇼츠) 대본 생성
    → 자동 검수(후킹/감정/결말 점수) → 기준 미달이면 재생성(최대 2회)
    → story.txt로 저장

generate_daily_batch()가 채널당 하루치 물량(기본 롱폼 1개 + 쇼츠 2개)을 한 번에
뽑습니다 — 위 파이프라인 1번으로 롱폼 1개 + 쇼츠 1개를 만들고, 나머지 쇼츠
1개는 (비용 절감을 위해 제목 최적화 없이) 쇼츠 전용 호출로 채웁니다.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from core.script_prompts import (
    SPLIT_DELIMITER,
    combo_script_prompt,
    shorts_script_prompt,
    extend_long_script_prompt,
    script_score_prompt,
    title_candidates_prompt,
    topic_idea_prompt,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent / ".env"

# 하루 여러 편씩 대량으로 뽑는 용도라 비용 우선으로 Haiku를 씁니다.
MODEL = "claude-haiku-4-5"

# 롱폼 분량이 목표에 못 미칠 때 "처음부터 다시 굴리기(full regen)" 대신 "지금
# 대본을 살린 채 부족한 만큼만 늘려 쓰기(extend)"를 시도할 최대 횟수.
# 실측 로그(2026-07-19~22 daily.yml)상 Haiku 첫 초안은 거의 항상 2600~3800자에
# 그쳐서 목표(4500자/22씬)에 못 미쳤는데, 처음부터 다시 쓰게 하면 (1) 방금 나온
# 괜찮은 내용을 통째로 버리고 (2) 또 같은 짧은 분량이 나와 재생성만 반복됐음.
# 확장은 이미 써둔 3000자쯤을 기반으로 몇 씬만 더 붙이면 되니 목표를 한 번에
# 채울 확률이 훨씬 높고, 쇼츠는 다시 안 받으니 토큰도 덜 듦.
MAX_EXTEND_ATTEMPTS = 2

# hook/ending 점수가 크게 미달일 때만(드물게) 콤보를 통째로 다시 생성. 분량은
# 위 extend로 해결하므로, 이 full regen은 "이야기 자체가 별로인" 경우 전용이라
# 실제로는 거의 안 걸림 — 1번이면 충분.
MAX_SCORE_REGEN_ATTEMPTS = 1
MIN_HOOK_SCORE = 70
MIN_ENDING_SCORE = 60

# 자동 검수(hook/emotion/ending)는 "이야기가 좋은지"만 보고 "분량이 충분한지"는
# 안 봅니다 — 점수는 높은데 씬이 20개/대사가 500자밖에 없는 식으로 8분 목표에
# 한참 못 미치는 롱폼이 그냥 통과해버린 적이 있어서, 분량 자체도 별도로 검사합니다.
# 목표치는 LONG_SCRIPT_PROMPT 요구치(22씬/4500자)에 가깝게 높게 잡되(extend로
# 채우니까 낮출 필요 없음), 확장 결과가 목표에 200~300자 못 미쳐도 한 번 더
# 확장하느라 토큰을 더 쓰지 않도록 "수용 기준"은 살짝 아래(20씬/4000자)에 둠 —
# 4000자면 실제 낭독 약 7분으로, 과거 사고 사례(2100자짜리 99초 영상)와는 확실히
# 격이 다르게 충분한 분량임. 대사 글자수는 [SCENE:...] 영어 묘사를 뺀 실제 낭독
# 텍스트만 셉니다(전체 4200자인데 실제 대사는 2100자뿐이라 99초밖에 안 나온
# 사례가 있어서 이렇게 셈).
MIN_LONG_SCENES = 20
MIN_LONG_DIALOGUE_CHARS = 4000

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
        "Claude API 호출 완료 (model=%s, input=%s, output=%s tokens)",
        MODEL, usage.input_tokens, usage.output_tokens,
    )
    return text


def _parse_json_response(text: str):
    """Claude 응답에서 JSON을 파싱합니다. ```json 코드블록으로 감싸서 오는
    경우가 종종 있어 그것부터 벗겨냅니다."""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1).strip()
    return json.loads(cleaned)


# ─────────────────────────────────────────────
# 1. 주제 선정
# ─────────────────────────────────────────────

def generate_topic_idea(performance_summary: str = "", recent_titles: list[str] | None = None) -> tuple[str, str]:
    """주제 하나(topic)와 카테고리를 골라 (topic, category)로 반환합니다."""
    logger.info("Claude API 주제 선정 요청")
    text = _call_claude(topic_idea_prompt(performance_summary, recent_titles))
    category = ""
    topic = ""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("CATEGORY:"):
            category = line.split(":", 1)[1].strip()
        elif line.upper().startswith("TOPIC:"):
            topic = line.split(":", 1)[1].strip()
    if not topic:
        raise ScriptGenerationError(f"주제 선정 응답을 파싱하지 못했습니다: {text[:200]}")
    return topic, category


# ─────────────────────────────────────────────
# 2. 제목 후보 생성 + 평가
# ─────────────────────────────────────────────

def generate_title_candidates(topic: str, n: int = 10) -> list[dict]:
    """[{"title": ..., "hook_score": int}, ...] 목록을 반환합니다."""
    logger.info("Claude API 제목 후보 %d개 생성 요청", n)
    text = _call_claude(title_candidates_prompt(topic, n))
    try:
        candidates = _parse_json_response(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ScriptGenerationError(f"제목 후보 응답 파싱 실패: {e}\n{text[:300]}") from e
    if not isinstance(candidates, list) or not candidates:
        raise ScriptGenerationError(f"제목 후보 응답 형식이 예상과 다릅니다: {text[:300]}")
    return candidates


def pick_best_title(topic: str, n: int = 10) -> tuple[str, list[dict]]:
    """제목 후보를 생성해서 가장 높은 hook_score를 받은 제목을 고릅니다."""
    candidates = generate_title_candidates(topic, n)
    best = max(candidates, key=lambda c: c.get("hook_score", 0))
    logger.info("제목 선정: \"%s\" (hook_score=%s, 후보 %d개 중)", best.get("title"), best.get("hook_score"), len(candidates))
    return best.get("title", topic), candidates


# ─────────────────────────────────────────────
# 3. 대본 생성 (제목 강제 + CTA 반영)
# ─────────────────────────────────────────────

def generate_combo_script(
    custom_topic: str | None = None,
    forced_title: str | None = None,
    cta_settings: dict | None = None,
) -> str:
    """Claude API를 호출해서 롱폼+쇼츠 통합 대본 원문을 반환합니다."""
    logger.info(
        "Claude API 콤보(롱폼+쇼츠) 대본 생성 요청 (topic=%s, title=%s)",
        custom_topic or "자동", forced_title or "자동",
    )
    return _call_claude(combo_script_prompt(custom_topic, forced_title, cta_settings))


def generate_shorts_only(cta_settings: dict | None = None) -> str:
    """Claude API를 호출해서 쇼츠 단독 대본 원문 하나만 반환합니다."""
    logger.info("Claude API 쇼츠 단독 대본 생성 요청 시작")
    return _call_claude(shorts_script_prompt(cta_settings))


def extend_long_script(current_long: str, reason: str) -> str:
    """분량 미달인 롱폼 대본을 처음부터 다시 쓰지 않고, 지금 내용을 살린 채
    부족한 만큼만 늘려서 다시 받아옵니다(쇼츠는 건드리지 않음)."""
    logger.info("Claude API 롱폼 대본 확장 요청 (사유=%s)", reason)
    return _call_claude(extend_long_script_prompt(current_long, reason))


_RE_THUMBNAIL_LINE = re.compile(
    r"(?im)^(THUMBNAIL_LONG|THUMBNAIL_SHORTS):[ \t]*\r?\n[^\r\n]*"
)


def _force_thumbnail_titles(text: str, title: str) -> str:
    """THUMBNAIL_LONG/THUMBNAIL_SHORTS 첫 줄을 무조건 확정된 title로
    덮어씁니다. 프롬프트로 "정확히 이 제목 그대로 써라" 라고 지시해도
    AI가 그 지시문 문장 자체를 값으로 베껴 쓰는 경우가 있어서, 이미
    확정돼 있는 title을 신뢰하고 후처리로 강제합니다."""
    return _RE_THUMBNAIL_LINE.sub(lambda m: f"{m.group(1)}:\n{title}", text)


# ─────────────────────────────────────────────
# 4. 자동 검수
# ─────────────────────────────────────────────

_RE_DIALOGUE_LINE = re.compile(r"^[^\s:：]+[:：]\s*(.+)$")


def _dialogue_char_count(long_part: str) -> int:
    """실제 낭독되는 대사 글자수만 셉니다 — [SCENE:...] 영어 장면 묘사나
    RESOLUTION:/CATEGORY:/CAST: 같은 섹션 헤더까지 같이 세면 실제 낭독 시간과
    무관하게 부풀려져서, 씬 개수/전체 글자수만으로는 "8분 목표"를 제대로
    검증할 수 없습니다(실제로 씬 25개·전체 4200자인데 대사는 2100자뿐이라
    TTS 낭독시간이 99초밖에 안 나온 사례로 확인됨).

    parser/story_parser.py와 동일한 규칙을 씁니다: 첫 [SCENE 이전 섹션(CAST:
    등)은 세지 않고("나레이터: 나레이터" 같은 CAST 매핑 줄이 대사로 오카운트
    되는 걸 방지), "화자:" 표시 없이 이어지는 줄은 직전 대사의 줄바꿈
    연속으로 보고 같이 셉니다(안 그러면 여러 줄로 줄바꿈된 대사의 뒷부분이
    카운트에서 통째로 빠짐)."""
    total = 0
    in_scene = False
    have_dialogue = False
    for line in long_part.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[SCENE"):
            in_scene = True
            have_dialogue = False
            continue
        if not in_scene:
            continue
        m = _RE_DIALOGUE_LINE.match(line)
        if m:
            total += len(m.group(1))
            have_dialogue = True
        elif have_dialogue:
            total += len(line)
    return total


def _split_long_part(text: str) -> str:
    """콤보 응답에서 롱폼 부분만 잘라냅니다(쇼츠 부분 제외)."""
    return text.split(SPLIT_DELIMITER, 1)[0] if SPLIT_DELIMITER in text else text


def _split_combo(text: str) -> tuple[str, str]:
    """콤보 응답을 (롱폼, 쇼츠꼬리) 로 나눕니다. 쇼츠꼬리는 구분선 이후 전체를
    구분선 포함해서 그대로 담고 있어서, 롱폼만 확장한 뒤 다시 이어붙이면 원래
    콤보 형식이 그대로 복원됩니다. 구분선이 없으면 쇼츠꼬리는 빈 문자열."""
    if SPLIT_DELIMITER in text:
        long_part, _, shorts_rest = text.partition(SPLIT_DELIMITER)
        return long_part.rstrip(), SPLIT_DELIMITER + shorts_rest
    return text, ""


def _stitch_combo(long_part: str, shorts_tail: str) -> str:
    """확장된 롱폼과 원래 쇼츠꼬리를 다시 콤보 형식으로 붙입니다."""
    if not shorts_tail:
        return long_part
    return f"{long_part.rstrip()}\n\n{shorts_tail}"


def _long_form_length_ok(text: str) -> tuple[bool, str]:
    """콤보 응답의 롱폼 부분이 최소 분량(씬 개수/실제 대사 글자수)을 채웠는지
    확인합니다. 부족하면 (False, 이유)를 반환 — score_script의 이야기 품질
    점수와는 별개로 "너무 짧게 써서 8분 목표를 못 채우는" 케이스를 잡아내기
    위한 검사입니다."""
    long_part = _split_long_part(text)
    scene_count = long_part.count("[SCENE:")
    dialogue_chars = _dialogue_char_count(long_part)
    if scene_count < MIN_LONG_SCENES:
        return False, f"씬 {scene_count}개 (최소 {MIN_LONG_SCENES}개 필요)"
    if dialogue_chars < MIN_LONG_DIALOGUE_CHARS:
        return False, f"대사 {dialogue_chars}자 (최소 {MIN_LONG_DIALOGUE_CHARS}자 필요)"
    return True, ""


def score_script(script_text: str) -> dict:
    """{"hook": int, "emotion": int, "ending": int, "dropout_risk": str, "cta_excess": bool}."""
    logger.info("Claude API 대본 자동 검수 요청")
    text = _call_claude(script_score_prompt(script_text))
    try:
        scores = _parse_json_response(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("검수 응답 파싱 실패, 검수 없이 통과 처리: %s", e)
        return {}
    logger.info(
        "검수 결과: hook=%s emotion=%s ending=%s dropout_risk=%s cta_excess=%s",
        scores.get("hook"), scores.get("emotion"), scores.get("ending"),
        scores.get("dropout_risk"), scores.get("cta_excess"),
    )
    return scores


# ─────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 전체 오케스트레이션
# ─────────────────────────────────────────────

def generate_optimized_script(
    custom_topic: str | None = None,
    channel: str | None = None,
) -> tuple[str, dict]:
    """주제 선정(필요시) → 제목 후보/평가 → 대본 생성 → 자동 검수(미달시 재생성)
    까지 전부 처리하고 (대본 원문, 메타데이터)를 반환합니다."""
    from core.performance_analysis import summarize_for_prompt, recent_titles as _recent_titles
    from core.cta_settings import get_settings as get_cta_settings

    topic = custom_topic
    category_hint = None
    if not topic:
        perf_summary = summarize_for_prompt(channel)
        recent = _recent_titles(channel)
        topic, category_hint = generate_topic_idea(perf_summary, recent)

    best_title, candidates = pick_best_title(topic)
    cta = get_cta_settings(channel) if channel else None

    # 1) 콤보 생성 → 롱폼이 짧으면 "처음부터 다시"가 아니라 "지금 걸 늘려서"
    #    목표 분량을 채웁니다(extend). 쇼츠 부분은 건드리지 않고 롱폼만 확장.
    text, long_part, length_ok, length_reason, extends = _extend_to_length(
        generate_combo_script(custom_topic=topic, forced_title=best_title, cta_settings=cta),
        best_title,
    )
    # 2) 분량이 확보된 롱폼만 채점(쇼츠 제외 = 입력 토큰 절약). 분량 미달로
    #    끝난 경우엔 어차피 최선을 다한 결과라 굳이 채점하지 않습니다.
    scores = score_script(long_part) if length_ok else {}

    # 3) hook/ending 점수가 크게 미달인 경우에만(드물게) 콤보를 통째로 다시
    #    생성. 이 경로도 재생성 후 다시 확장까지 태워 분량을 보장합니다.
    score_regens = 0
    while (
        scores
        and (scores.get("hook", 100) < MIN_HOOK_SCORE or scores.get("ending", 100) < MIN_ENDING_SCORE)
        and score_regens < MAX_SCORE_REGEN_ATTEMPTS
    ):
        logger.warning(
            "검수 점수 미달(hook=%s, ending=%s) — 재생성 %d/%d",
            scores.get("hook"), scores.get("ending"), score_regens + 1, MAX_SCORE_REGEN_ATTEMPTS,
        )
        text, long_part, length_ok, length_reason, ext = _extend_to_length(
            generate_combo_script(custom_topic=topic, forced_title=best_title, cta_settings=cta),
            best_title,
        )
        extends += ext
        scores = score_script(long_part) if length_ok else {}
        score_regens += 1

    meta = {
        "topic": topic,
        "category_hint": category_hint,
        "title": best_title,
        "title_candidates": candidates,
        "scores": scores,
        "extends": extends,
        "score_regens": score_regens,
    }
    return text, meta


def _extend_to_length(text: str, best_title: str) -> tuple[str, str, bool, str, int]:
    """콤보 응답을 받아 제목을 강제 교정하고, 롱폼이 분량 미달이면 목표를
    채울 때까지(또는 MAX_EXTEND_ATTEMPTS 도달까지) 롱폼만 확장한 뒤,
    (재조립된 콤보 텍스트, 롱폼 부분, 분량통과여부, 미달사유, 확장횟수)를 반환합니다."""
    text = _force_thumbnail_titles(text, best_title)
    long_part, shorts_tail = _split_combo(text)
    length_ok, reason = _long_form_length_ok(long_part)
    extends = 0
    while not length_ok and extends < MAX_EXTEND_ATTEMPTS:
        logger.warning("롱폼 분량 미달(%s) — 확장 %d/%d", reason, extends + 1, MAX_EXTEND_ATTEMPTS)
        long_part = _force_thumbnail_titles(extend_long_script(long_part, reason), best_title)
        length_ok, reason = _long_form_length_ok(long_part)
        extends += 1
    return _stitch_combo(long_part, shorts_tail), long_part, length_ok, reason, extends


def generate_and_save(
    input_dir: Path,
    custom_topic: str | None = None,
    channel: str | None = None,
    meta_out: Optional[dict] = None,
) -> list[Path]:
    """전체 파이프라인(주제→제목→대본→검수) 1회분 생성부터 저장까지.
    meta_out 딕셔너리를 넘기면 topic/title/scores 등 상세 정보를 채워 넣습니다."""
    text, meta = generate_optimized_script(custom_topic=custom_topic, channel=channel)
    if meta_out is not None:
        meta_out.update(meta)
    return save_combo_script(text, input_dir)


def generate_daily_batch(
    input_dir: Path,
    long_count: int = 1,
    extra_shorts_count: int = 1,
    progress_cb: Optional[ProgressCallback] = None,
    custom_topic: str | None = None,
    channel: str | None = None,
    meta_out: Optional[list[dict]] = None,
) -> list[Path]:
    """
    채널 하루치 물량을 생성합니다. 기본값(long_count=1, extra_shorts_count=1)이면
    전체 파이프라인(주제→제목→대본→검수) 1번(롱폼 1개 + 쇼츠 1개) + 쇼츠 단독
    호출 1번(비용 절감을 위해 제목 최적화 생략) = 롱폼 1개 + 쇼츠 2개 (하루 3개).
    custom_topic이 있으면 메인 콤보에만 그 주제를 강제로 씁니다 — 디스코드로
    예약된 주제가 있을 때 그날의 메인 영상에 반영하는 용도입니다.
    meta_out 리스트를 넘기면 각 콤보 생성의 메타데이터(topic/title/scores)를
    append합니다.
    실패한 개별 호출은 건너뛰고 계속 진행하며, 하나도 성공 못 하면 예외를 던집니다.
    """
    from core.cta_settings import get_settings as get_cta_settings

    total = long_count + extra_shorts_count
    done = 0
    saved: list[Path] = []
    errors: list[str] = []

    for i in range(long_count):
        try:
            meta: dict = {}
            saved.extend(generate_and_save(input_dir, custom_topic=custom_topic, channel=channel, meta_out=meta))
            if meta_out is not None:
                meta_out.append(meta)
        except ScriptGenerationError as e:
            logger.warning("일괄 생성 중 콤보 %d/%d 실패: %s", i + 1, long_count, e)
            errors.append(str(e))
        done += 1
        if progress_cb:
            progress_cb(done, total)

    cta = get_cta_settings(channel) if channel else None
    for i in range(extra_shorts_count):
        try:
            text = generate_shorts_only(cta_settings=cta)
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

"""
v2/llm.py
==========
기존 무료 LLM 체인(core.ai_script_generator._call_claude → OpenRouter→NVIDIA 직접)을 재사용하되
JSON 응답을 안전하게 뽑아내고, 형식이 깨지면 한 번 더 고쳐 쓰게 한다.
유료 모델은 호출하지 않는다(체인 정책은 기존 코드 그대로).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """서비스 문제(모델 장애·타임아웃·한도) — 나중에 다시 시도하면 될 수 있다."""


class LLMValidationError(LLMError):
    """모델은 응답했지만 우리 검증(근거·형식)을 반복해서 통과하지 못함 — 콘텐츠 품질 문제로 취급한다."""


def _extract_json(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e > s:
            chunk = text[s:e + 1]
            try:
                return json.loads(chunk)
            except Exception:
                # 흔한 오류: 마지막 쉼표
                fixed = re.sub(r",\s*([}\]])", r"\1", chunk)
                try:
                    return json.loads(fixed)
                except Exception:
                    continue
    raise ValueError("JSON을 찾지 못했습니다")


def call_text(prompt: str, max_tokens: int = 16000) -> str:
    from core.ai_script_generator import _call_claude, ScriptGenerationError
    try:
        return _call_claude(prompt, max_tokens=max_tokens)
    except ScriptGenerationError as e:
        raise LLMError(str(e)) from e


def call_json(prompt: str, validate: Optional[Callable[[Any], None]] = None, max_tokens: int = 16000, repairs: int = 1) -> Any:
    """JSON으로 응답받는다. 파싱/검증 실패 시 오류 내용을 알려주고 repairs 번까지 다시 쓰게 한다."""
    last = ""
    for attempt in range(repairs + 1):
        p = prompt if attempt == 0 else (
            prompt + f"\n\n[이전 응답의 문제 — 반드시 고쳐라] {last}\n"
                     "같은 문제를 반복하지 말고, 설명 없이 올바른 JSON만 다시 출력해라."
        )
        raw = call_text(p, max_tokens=max_tokens)
        try:
            data = _extract_json(raw)
            if validate:
                validate(data)
            return data
        except Exception as e:  # noqa: BLE001
            last = str(e)[:300]
            logger.warning("LLM JSON 응답 문제(%d/%d): %s", attempt + 1, repairs + 1, last)
    raise LLMValidationError(f"JSON 응답을 얻지 못했습니다: {last}")

"""
scripts/generate_topics.py
============================
디스코드 슬래시 커맨드(/영상 주제생성)에서 넘어온 채널+개수(+카테고리)로,
과거 성과 데이터를 참고해서 새 주제 여러 개를 만들어 topic_queue에 예약(날짜
미지정 = 다음 실행부터 순서대로 소비)해두는 헤드리스 스크립트.

실제 대본/영상을 만들지는 않습니다 — 그냥 "다음에 쓸 주제 아이디어"를
미리 채워두는 용도입니다.

수동 실행: python scripts/generate_topics.py --channel 웃짬 --count 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.notify import notify_discord
from core.ai_script_generator import ScriptGenerationError, _call_claude, _parse_json_response
from core.script_prompts import topic_batch_prompt
from core.performance_analysis import summarize_for_prompt, recent_titles
from core.topic_queue import push_topic


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--category", default="", help="비우면 성과 좋은 카테고리 위주로 자동 선택")
    args = parser.parse_args()

    setup_logging(log_dir=ROOT / "logs", level="DEBUG")
    logger = get_logger(__name__)

    perf = summarize_for_prompt(args.channel)
    recent = recent_titles(args.channel)
    prompt = topic_batch_prompt(args.count, args.category or None, perf, recent)

    try:
        text = _call_claude(prompt)
        topics = _parse_json_response(text)
    except (ScriptGenerationError, ValueError) as e:
        logger.error("generate_topics: 주제 생성 실패 — %s", e)
        notify_discord(f"🔴 [{args.channel}] 주제 생성 실패 — {e}")
        return 1

    if not isinstance(topics, list) or not topics:
        logger.error("generate_topics: 응답 형식이 예상과 다름: %s", text[:300])
        notify_discord(f"🔴 [{args.channel}] 주제 생성 응답을 파싱하지 못함")
        return 1

    saved = 0
    lines = []
    for item in topics:
        topic = (item.get("topic") or "").strip()
        category = (item.get("category") or "").strip()
        if not topic:
            continue
        push_topic(args.channel, topic)
        saved += 1
        lines.append(f"- [{category or '?'}] {topic}")

    logger.info("generate_topics: '%s' 주제 %d개 예약 완료", args.channel, saved)
    notify_discord(
        f"🧠 [{args.channel}] 새 주제 {saved}개 생성/예약 완료 (다음 실행부터 순서대로 사용됨)\n"
        + "\n".join(lines[:20])
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
scripts/prepare_daily.py
=========================
"대본 생성" 전용 헤드리스 스크립트 — 설정에 등록된 모든 채널에 대해 하루치
대본(채널당 롱폼 1개 + 쇼츠 2개)을 Claude API로 생성해서
queue/pending_scripts/{채널}/ 에 저장합니다.

대본이 저장되는 순간(core/ai_script_generator.py의 save_story) Cloudflare
Worker에 AI 씬 이미지 생성을 비동기로 요청해두므로, 이 스크립트는 대본
생성만 하고 영상 조립(TTS/이미지/합성/업로드)은 전혀 하지 않습니다 —
그래야 Worker가 이미지를 다 그릴 때까지 기다리는 시간 없이 빨리 끝납니다.

실제 영상 조립은 scripts/daily_generate.py가 담당하며, Worker가 이미지
생성을 끝내면 assemble_daily.yml 워크플로우를 직접 트리거해서 그 스크립트를
실행시킵니다(tools/discord_worker/index.js).

수동 실행: python scripts/prepare_daily.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify, notify_discord
from core.ai_script_generator import ScriptGenerationError, generate_daily_batch
from core.daily_queue import queue_dir_for_channel
from core.topic_queue import pop_topic

_STATE_PATH = ROOT / "config" / "daily_generate_state.json"


def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate_scripts(cfg, channels, logger) -> tuple[int, int]:
    """모든 채널에 하루치 대본을 생성해 queue/pending_scripts/에 저장합니다.
    채널별로 오늘 이미 생성했으면 건너뜁니다. (성공 채널 수, 저장된 파일
    총수)를 반환."""
    today = date.today().isoformat()
    state = _load_state()

    logger.info("prepare_daily: %d개 채널 대상으로 하루치 대본 생성 시작", len(channels))
    ok_channels = 0
    saved_total = 0
    for chan in channels:
        name = chan.get("name") or "(이름없음)"

        if state.get(name) == today:
            logger.info("prepare_daily: '%s' 오늘 이미 대본 생성함 — 건너뜀", name)
            ok_channels += 1
            continue

        queue_dir = queue_dir_for_channel(name)
        topic = pop_topic(name, today)
        if topic:
            logger.info("prepare_daily: '%s' 디스코드로 예약된 주제 사용 — %s", name, topic)
        try:
            meta_list: list[dict] = []
            saved = generate_daily_batch(queue_dir, custom_topic=topic, channel=name, meta_out=meta_list)
            logger.info("prepare_daily: '%s' 대본 생성 완료 — %d개 저장", name, len(saved))
            for meta in meta_list:
                logger.info(
                    "prepare_daily: '%s' 대본 — 제목 \"%s\" · 확장 %s회 · 분량기준 %s",
                    name, meta.get("title", "?"), meta.get("extends", 0),
                    "충족" if meta.get("length_ok") else "미달",
                )
            ok_channels += 1
            saved_total += len(saved)
            state[name] = today
            _save_state(state)
        except ScriptGenerationError as e:
            logger.error("prepare_daily: '%s' 대본 생성 전체 실패 — %s", name, e)
    logger.info("prepare_daily: 대본 생성 종료 (%d/%d 채널 성공)", ok_channels, len(channels))
    return ok_channels, saved_total


def main() -> int:
    cfg = get_config()
    setup_logging(
        log_dir=ROOT / cfg.get("paths.logs_dir", "logs"),
        level=cfg.get("logging.level", "DEBUG"),
        max_bytes=cfg.get("logging.max_file_size", 10 * 1024 * 1024),
        backup_count=cfg.get("logging.backup_count", 5),
    )
    logger = get_logger(__name__)

    channels = list(cfg.get("youtube.channels", []) or [])
    if not channels:
        logger.warning("prepare_daily: 등록된 채널이 없습니다 (설정 > 채널 관리에서 추가하세요).")
        return 1

    channel_names = ", ".join(c.get("name", "?") for c in channels)
    notify("대본 생성 시작", f"{len(channels)}개 채널 — AI 이미지 생성을 미리 요청해둡니다.")
    notify_discord(f"📝 대본 생성 시작 — {len(channels)}개 채널({channel_names}), 완료되면 AI 이미지 준비 후 영상 조립이 자동으로 이어집니다.")

    ok, saved = _generate_scripts(cfg, channels, logger)

    summary = f"대본 {saved}개 생성 ({ok}/{len(channels)}개 채널)"
    logger.info("prepare_daily: 종료 — %s", summary)
    notify("대본 생성 완료", summary)
    notify_discord(("✅" if ok > 0 else "🔴") + f" {summary} — AI 이미지 준비되는 대로 영상 조립 시작됨")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

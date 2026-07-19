"""
scripts/generate_topic.py
==========================
디스코드 슬래시 커맨드(/영상)에서 넘어온 "채널 + 주제"로 대본 1편(롱폼+쇼츠)을
그 자리에서 만들어서 바로 영상 제작 + 유튜브 업로드까지 진행하는 온디맨드 스크립트.

daily_generate.py(매일 정해진 시각에 채널당 3개씩)와 별개로, 사용자가 원할 때
원하는 주제로 즉석에서 한 편 뽑는 용도입니다.

수동 실행: python scripts/generate_topic.py --channel 웃짬 --topic "며느리가 숨긴 유산"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify_discord
from core.ai_script_generator import ScriptGenerationError, generate_and_save
from core.batch_runner import BatchRunner
from core.video_registry import record_upload


def _resolve_input_dir(input_dir: str, cfg) -> Path:
    rel = input_dir or cfg.get("paths.input_dir", "input")
    path = ROOT / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True, help="config.json의 youtube.channels[].name과 일치해야 함")
    parser.add_argument("--topic", required=True, help="대본 주제 (자유 텍스트)")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(
        log_dir=ROOT / cfg.get("paths.logs_dir", "logs"),
        level=cfg.get("logging.level", "DEBUG"),
        max_bytes=cfg.get("logging.max_file_size", 10 * 1024 * 1024),
        backup_count=cfg.get("logging.backup_count", 5),
    )
    logger = get_logger(__name__)

    channels = {c.get("name"): c for c in (cfg.get("youtube.channels", []) or [])}
    chan = channels.get(args.channel)
    if not chan:
        msg = f"'{args.channel}' 채널을 찾을 수 없음 (등록된 채널: {', '.join(channels) or '없음'})"
        logger.error("generate_topic: %s", msg)
        notify_discord(f"🔴 온디맨드 생성 실패 — {msg}")
        return 1

    notify_discord(f"🎯 [{args.channel}] 주제 지정 생성 시작 — \"{args.topic}\"")

    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)
    try:
        saved = generate_and_save(input_dir, custom_topic=args.topic)
    except ScriptGenerationError as e:
        logger.error("generate_topic: 대본 생성 실패 — %s", e)
        notify_discord(f"🔴 [{args.channel}] 대본 생성 실패 — {e}")
        return 1

    logger.info("generate_topic: '%s' 대본 %d개 저장 — 영상 제작+업로드 시작", args.channel, len(saved))

    privacy = cfg.get("youtube.default_privacy", "private")
    runner = BatchRunner(
        cfg, input_dir,
        youtube_credentials_file=chan.get("credentials_file", ""),
        archive_subdir=args.channel,
    )
    try:
        results = runner.run(
            count=len(saved),
            upload_to_youtube=True,
            youtube_privacy=privacy,
            schedule_days_ahead=0,
            also_make_shorts=False,
            stagger_days=0,
        )
    except Exception as e:
        logger.exception("generate_topic: 배치 처리 중 예외 발생")
        notify_discord(f"🔴 [{args.channel}] 영상 제작 중 오류 — {e}")
        return 1

    for r in results:
        if r.success and r.youtube_video_id:
            title = Path(r.video_path).parent.name
            is_shorts = bool(r.thumbnail_shorts_path) and not r.thumbnail_long_path
            record_upload(r.youtube_video_id, args.channel, r.category, r.title or title, is_shorts)
            notify_discord(f"✅ [{args.channel}] 업로드 완료 — {title}\nhttps://youtu.be/{r.youtube_video_id}")
        elif r.success:
            notify_discord(f"⚠️ [{args.channel}] 영상은 만들어졌지만 업로드 실패 — {r.youtube_error or '알 수 없는 오류'}")
        else:
            notify_discord(f"🔴 [{args.channel}] 영상 생성 실패 — {r.error or '알 수 없는 오류'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

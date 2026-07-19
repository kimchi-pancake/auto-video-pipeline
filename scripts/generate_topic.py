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
import unicodedata
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify_discord
from core.ai_script_generator import ScriptGenerationError, generate_and_save
from core.batch_runner import BatchRunner
from core.video_registry import record_upload
from utils.soft_approval import is_rejected, publish_video, sleep_for_review


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

    # 셸/워크플로우 입력 전달 과정에서 한글이 정규화(NFC/NFD)가 다르게 들어오면
    # 눈에는 똑같아 보여도 문자열 비교가 실패합니다 — 양쪽 다 NFC로 맞춰서 비교.
    def _nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    channels = {_nfc(c.get("name", "")): c for c in (cfg.get("youtube.channels", []) or [])}
    chan = channels.get(_nfc(args.channel))
    if not chan:
        msg = f"'{args.channel}' 채널을 찾을 수 없음 (등록된 채널: {', '.join(channels) or '없음'})"
        logger.error("generate_topic: %s", msg)
        notify_discord(f"🔴 온디맨드 생성 실패 — {msg}")
        return 1

    notify_discord(f"🎯 [{args.channel}] 주제 지정 생성 시작 — \"{args.topic}\"")

    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)
    try:
        meta: dict = {}
        saved = generate_and_save(input_dir, custom_topic=args.topic, channel=args.channel, meta_out=meta)
    except ScriptGenerationError as e:
        logger.error("generate_topic: 대본 생성 실패 — %s", e)
        notify_discord(f"🔴 [{args.channel}] 대본 생성 실패 — {e}")
        return 1

    scores = meta.get("scores") or {}
    if scores:
        notify_discord(
            f"📝 [{args.channel}] 대본 준비됨 — \"{meta.get('title')}\"\n"
            f"후킹 {scores.get('hook', '?')} · 감정 {scores.get('emotion', '?')} · 결말 {scores.get('ending', '?')}"
        )
    logger.info("generate_topic: '%s' 대본 %d개 저장 — 영상 제작+업로드 시작", args.channel, len(saved))

    default_privacy = cfg.get("youtube.default_privacy", "private")
    upload_privacy = "private" if default_privacy == "public" else default_privacy

    def _on_file_done(story_path: str, r) -> None:
        # 파일 하나(보통 롱폼+쇼츠 중 하나) 끝날 때마다 바로 알림 — 배치
        # 전체가 끝날 때까지 기다렸다가 한꺼번에 보내지 않도록 함
        # (daily_generate.py에서 겪은 것과 같은 알림 지연 버그 방지).
        if r.success and r.youtube_video_id:
            title = Path(r.video_path).parent.name
            is_shorts = bool(r.thumbnail_shorts_path) and not r.thumbnail_long_path
            record_upload(r.youtube_video_id, args.channel, r.category, r.title or title, is_shorts)
            kind = "쇼츠" if is_shorts else "롱폼"
            if default_privacy == "public":
                notify_discord(
                    f"👀 [{args.channel}] {kind} 미리보기 — {r.title or title}\n"
                    f"https://youtu.be/{r.youtube_video_id}\n"
                    f"5분 안에 `/영상 거절 {r.youtube_video_id}` 안 하면 자동으로 공개됨."
                )
                sleep_for_review()
                if is_rejected(r.youtube_video_id):
                    notify_discord(f"🚫 [{args.channel}] 거절됨 — 비공개로 유지: {title}")
                elif publish_video(r.youtube_video_id, chan.get("credentials_file", "")):
                    notify_discord(f"✅ [{args.channel}] {kind} 공개 전환 완료 — {title}\nhttps://youtu.be/{r.youtube_video_id}")
                else:
                    notify_discord(f"⚠️ [{args.channel}] 공개 전환 실패 — 유튜브 스튜디오에서 직접 바꿔야 함: {title}")
            else:
                notify_discord(f"✅ [{args.channel}] 업로드 완료(비공개) — {title}\nhttps://youtu.be/{r.youtube_video_id}")
        elif r.success:
            notify_discord(f"⚠️ [{args.channel}] 영상은 만들어졌지만 업로드 실패 — {r.youtube_error or '알 수 없는 오류'}")
        else:
            notify_discord(f"🔴 [{args.channel}] 영상 생성 실패 — {r.error or '알 수 없는 오류'}")

    runner = BatchRunner(
        cfg, input_dir,
        youtube_credentials_file=chan.get("credentials_file", ""),
        archive_subdir=args.channel,
        discord_status=True,
        on_file_done=_on_file_done,
    )
    try:
        runner.run(
            count=len(saved),
            upload_to_youtube=True,
            youtube_privacy=upload_privacy,
            schedule_days_ahead=0,
            also_make_shorts=False,
            stagger_days=0,
        )
    except Exception as e:
        logger.exception("generate_topic: 배치 처리 중 예외 발생")
        notify_discord(f"🔴 [{args.channel}] 영상 제작 중 오류 — {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

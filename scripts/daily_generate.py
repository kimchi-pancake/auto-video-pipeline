"""
scripts/daily_generate.py
==========================
채널별 대기열(큐)을 실제 영상으로 만들고 YouTube에 업로드까지 진행하는
"조립" 단계 전용 스크립트입니다.

2026-08-04 이전에는 "대본 생성 + 영상 조립"이 이 스크립트 하나에서 한 번에
돌았는데, AI 이미지를 Cloudflare Worker(Pollinations)로 미리 그려두는
구조로 바꾸면서 두 단계로 쪼갰습니다:
  1. scripts/prepare_daily.py — 대본만 생성해서 queue/pending_scripts/에
     커밋해두고, 그 자리에서 Worker에 씬 이미지 생성을 요청함(대기 없음).
  2. 이 스크립트(daily_generate.py) — queue/pending_scripts/에 쌓인 대본을
     로컬 input_dir로 가져와 실제 영상(TTS+이미지+자막+합성)을 만들고 업로드.
     이미지는 이미 Worker가 미리 그려뒀을 확률이 높아 대기 시간이 거의 없음.

Worker가 이미지 생성을 다 끝내면 이 스크립트를 담은 assemble_daily.yml
워크플로우를 직접 트리거합니다(tools/discord_worker/index.js). 혹시 그
트리거가 실패해도 놓치지 않도록, Worker의 별도 cron이 몇 시간 뒤 안전망으로
한 번 더 트리거합니다 — 그때도 대기열이 비어 있으면 이 스크립트는 아무것도
안 하고 조용히 끝납니다.

수동 실행: python scripts/daily_generate.py
"""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify, notify_discord, notify_discord_create_thread
from core.batch_runner import BatchRunner
from core.daily_queue import pull_queue_into_input_dir
from core.video_registry import record_upload
from utils.soft_approval import publish_video, schedule_publish

_KST = pytz.timezone("Asia/Seoul")


def _extract_title(video_path: str | None) -> str:
    """output/20260718_205639_아들이 버린 엄마, 진짜 사연/... 형태의 경로에서
    타임스탬프를 뗀 제목만 뽑아냅니다."""
    if not video_path:
        return "(제목 미상)"
    folder = Path(video_path).parent.name
    m = re.match(r"^\d{8}_\d{6}_(.+)$", folder)
    return m.group(1) if m else folder


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


def _resolve_input_dir(input_dir: str, cfg) -> Path:
    """gui/main_window.py의 _resolve_input_dir와 동일한 로직 (GUI 의존성 없이 재구현)."""
    rel = input_dir or cfg.get("paths.input_dir", "input")
    path = ROOT / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def _finalize_publish(name: str, chan: dict, video_id: str, title: str, kind: str, publish_at: datetime, thread_id: str | None = None) -> None:
    """업로드 직후 곧바로 예약공개(또는 목표 시각이 이미 지났으면 즉시 공개)를
    확정합니다. 예전에는 review_lock 시각까지 대기(/영상 거절 여부 확인)한
    뒤에야 이 처리를 했는데, 그 대기가 매번 최소 3시간40분씩 걸려서 GitHub
    Actions 과금의 실제 원인이었습니다(2026-08-01 확인). 소프트 승인/거절
    기능 자체를 제거하고 생성 직후 바로 확정하는 방식으로 바꿨습니다."""
    now = datetime.now(_KST)
    if publish_at > now:
        if schedule_publish(video_id, chan.get("credentials_file", ""), publish_at):
            notify_discord(
                f"📅 [{name}] {kind} 예약 확정 — {publish_at.strftime('%H:%M')}에 자동 공개 예정: {title}\n"
                f"https://youtu.be/{video_id}",
                thread_id=thread_id,
            )
        else:
            notify_discord(f"⚠️ [{name}] 예약 확정 실패 — 유튜브 스튜디오에서 직접 공개로 바꿔야 함: {title}", thread_id=thread_id)
    else:
        # 목표 공개 시각도 이미 지났음(생성이 아주 오래 걸린 경우) — 예약을
        # 걸어봤자 과거 시각이라 API가 거부하거나 무의미하므로 바로 공개.
        if publish_video(video_id, chan.get("credentials_file", "")):
            notify_discord(
                f"✅ [{name}] {kind} 공개 완료(목표 시각을 넘겨 즉시 공개됨) — {title}\n"
                f"https://youtu.be/{video_id}",
                thread_id=thread_id,
            )
        else:
            notify_discord(f"⚠️ [{name}] 공개 전환 실패 — 유튜브 스튜디오에서 직접 공개로 바꿔야 함: {title}", thread_id=thread_id)


def _handle_result(name: str, chan: dict, cfg, default_privacy: str, story_path: str, result, counters: dict, logger, thread_id: str | None = None) -> None:
    """파일 하나 처리가 끝날 때마다(배치 전체가 아니라) 바로 불려서, 그
    영상에 대한 디스코드 알림을 즉시 보냅니다 — 채널 대기열 전체(롱폼+쇼츠2)가
    다 끝날 때까지 기다렸다가 한꺼번에 보내면, 롱폼 하나 렌더링에만 1시간
    가까이 걸리는 상황에서 사용자가 한참 동안 아무 알림도 못 받게 됩니다."""
    title = _extract_title(result.video_path)
    # 썸네일은 롱폼/쇼츠 여부와 무관하게 core/pipeline.py가 항상 둘 다
    # 만들어서 (thumbnail_long_path, thumbnail_shorts_path) 둘 다 채워짐 —
    # 그걸로 종류를 구분하려던 예전 코드는 늘 "롱폼"으로만 나오는 버그였음.
    # 대신 입력 파일명 접미사(claude_..._shorts.txt / _long.txt)로 구분.
    is_shorts = "_shorts" in Path(story_path).stem
    kind = "쇼츠" if is_shorts else "롱폼"

    # r.success는 "영상 파일 자체가 만들어졌는지"만 뜻함 (core/pipeline.py 설계상
    # 업로드 실패해도 영상 생성은 성공으로 침). 실제 유튜브 업로드 여부는
    # youtube_video_id가 채워졌는지로 따로 확인해야 함 — 안 그러면 업로드가
    # 조용히 실패해도 "성공"으로 보고돼서 놓치게 됨 (실제로 한 번 겪은 버그).
    if result.success and result.youtube_video_id:
        counters["uploaded"] += 1
        record_upload(result.youtube_video_id, name, result.category, result.title or title, is_shorts)
        if default_privacy == "public":
            publish_hour = cfg.get("youtube.schedule_hour", 20)
            publish_minute = cfg.get("youtube.schedule_minute", 0)
            now = datetime.now(_KST)
            publish_at = now.replace(hour=publish_hour, minute=publish_minute, second=0, microsecond=0)
            if publish_at <= now:
                publish_at += timedelta(days=1)
            _finalize_publish(name, chan, result.youtube_video_id, result.title or title, kind, publish_at, thread_id)
        else:
            notify_discord(
                f"✅ [{name}] {kind} 업로드 완료(비공개) — {title}\n"
                f"https://youtu.be/{result.youtube_video_id}",
                thread_id=thread_id,
            )
    elif result.success and not result.youtube_video_id:
        counters["made_but_not_uploaded"] += 1
        logger.error(
            "daily_generate: '%s' 영상 생성됨 — 업로드 실패: %s (%s)",
            name, title, result.youtube_error,
        )
        notify_discord(
            f"⚠️ [{name}] {kind} 영상은 만들어졌지만 업로드 실패 — {title}\n"
            f"사유: {result.youtube_error or '알 수 없는 오류'}",
            thread_id=thread_id,
        )
    else:
        counters["fail"] += 1
        notify_discord(f"🔴 [{name}] {kind} 영상 생성 자체가 실패 — {title}\n사유: {result.error or '알 수 없는 오류'}", thread_id=thread_id)


def _preflight_youtube_auth(cfg, chan, logger) -> tuple[bool, str]:
    """렌더링을 시작하기 전에 채널의 유튜브 자격증명이 유효한지 확인합니다.
    (성공 여부, 실패 사유)를 반환. 무거운 작업(build 이후)은 하지 않고
    토큰 리프레시까지만 검증합니다."""
    from youtube.youtube_uploader import YouTubeUploader

    yt_cfg = cfg.section("youtube")
    cred_file = chan.get("credentials_file", "")
    if cred_file:
        yt_cfg["credentials_file"] = cred_file
    try:
        uploader = YouTubeUploader(yt_cfg)
        if uploader.authenticate():
            return True, ""
        return False, "자격증명을 불러오지 못했습니다(파일 없음/형식 오류)."
    except Exception as e:
        return False, str(e)


def _process_channel(cfg, chan, logger) -> tuple[int, int]:
    """채널 하나의 대기열을 처리합니다. (성공 개수, 실패 개수)를 반환."""
    name = chan.get("name") or "(이름없음)"
    default_privacy = cfg.get("youtube.default_privacy", "private")
    # 공개가 목표인 채널이어도 일단 비공개로 올린 뒤, _handle_result가 곧바로
    # _finalize_publish로 예약공개(publishAt)를 확정합니다.
    upload_privacy = "private" if default_privacy == "public" else default_privacy
    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)

    counters = {"uploaded": 0, "made_but_not_uploaded": 0, "fail": 0}

    def _on_file_done(story_path: str, result, tid) -> None:
        _handle_result(name, chan, cfg, default_privacy, story_path, result, counters, logger, thread_id=tid)

    # queue/pending_scripts/{채널}/(scripts/prepare_daily.py가 미리 커밋해둔
    # 대본)를 로컬 input_dir로 가져옵니다 — 이 스크립트가 조립 전용으로
    # 쪼개지면서 생긴 단계입니다(2026-08-04).
    pulled = pull_queue_into_input_dir(name, input_dir)
    if pulled:
        logger.info("daily_generate: '%s' 큐에서 대본 %d개 가져옴", name, len(pulled))

    # 대기 파일 조회는 스레드/BatchRunner 없이도 되니, 처리할 게 있을 때만
    # 채널별 디스코드 스레드를 만듭니다(빈 채널까지 매번 스레드가 생기지 않게).
    input_paths = sorted(input_dir.glob("*.txt"), key=lambda p: p.stat().st_ctime)
    if not input_paths:
        logger.info("daily_generate: '%s' 처리할 대기 파일 없음", name)
        return 0, 0

    # 사전 인증 점검: 유튜브 자격증명이 죽어 있으면(리프레시 실패/클라이언트 삭제)
    # 어차피 모든 업로드가 실패합니다. 그런데 업로드는 각 영상을 1시간 가까이
    # 렌더링한 "뒤"에야 시도되므로, 죽은 자격증명으로 채널당 3편을 통째로
    # 렌더링하고 나서야 전부 실패하는 낭비(런너 시간·토큰)가 발생합니다 —
    # 렌더 시작 전에 인증을 한 번 확인해서, 죽어 있으면 채널을 통째로 건너뜁니다.
    ok, auth_err = _preflight_youtube_auth(cfg, chan, logger)
    if not ok:
        logger.error("daily_generate: '%s' 유튜브 인증 실패 — 채널 건너뜀: %s", name, auth_err)
        notify_discord(
            f"🔴 [{name}] 유튜브 인증이 만료돼 업로드를 못 합니다 — 재인증 필요.\n{auth_err}\n"
            f"({len(input_paths)}편은 렌더링하지 않고 대기열에 그대로 둡니다.)"
        )
        return 0, len(input_paths)

    today_str = date.today().isoformat()
    thread_id = notify_discord_create_thread(f"{name} {today_str}")
    if thread_id:
        logger.info("daily_generate: '%s' 디스코드 스레드 생성 — %s", name, thread_id)
    else:
        logger.warning("daily_generate: '%s' 디스코드 스레드 생성 실패 — 메인 채널로 대체", name)

    runner = BatchRunner(
        cfg,
        input_dir,
        youtube_credentials_file=chan.get("credentials_file", ""),
        archive_subdir=name,
        discord_status=True,
        discord_thread_id=thread_id,
        on_file_done=lambda story_path, result: _on_file_done(story_path, result, thread_id),
    )
    pending = runner.get_pending_files()

    logger.info("daily_generate: '%s' 영상 생성+업로드 시작 (%d개)", name, len(pending))
    try:
        runner.run(
            count=len(pending),
            upload_to_youtube=True,
            youtube_privacy=upload_privacy,
            schedule_days_ahead=0,
            also_make_shorts=False,
            stagger_days=0,
        )
    except Exception as e:
        logger.exception("daily_generate: '%s' 배치 처리 중 예외 발생", name)
        notify_discord(f"🔴 [{name}] 배치 처리 중 예외 발생: {e}", thread_id=thread_id)
        return counters["uploaded"], counters["made_but_not_uploaded"] + counters["fail"]

    total_fail = counters["made_but_not_uploaded"] + counters["fail"]
    logger.info("daily_generate: '%s' 처리 완료 — 업로드 성공 %d개, 실패 %d개", name, counters["uploaded"], total_fail)
    return counters["uploaded"], total_fail


def _process_queue(cfg, channels, logger) -> tuple[int, int]:
    """각 채널의 대기열(오늘 새로 만든 것 + 기존에 밀려있던 것 전부)을 영상으로
    만들고 업로드합니다. GUI의 병렬 실행("이 채널 시작" 여러 개 동시 실행)과
    동일하게, 채널별로 동시에 처리합니다. (성공 영상 수, 실패 영상 수)를 반환."""
    total_success = 0
    total_fail = 0

    with ThreadPoolExecutor(max_workers=len(channels) or 1) as pool:
        futures = {pool.submit(_process_channel, cfg, chan, logger): chan for chan in channels}
        for future in as_completed(futures):
            chan = futures[future]
            try:
                ok, fail = future.result()
            except Exception:
                logger.exception("daily_generate: '%s' 처리 스레드 오류", chan.get("name", ""))
                continue
            total_success += ok
            total_fail += fail

    return total_success, total_fail


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
        logger.warning("daily_generate: 등록된 채널이 없습니다 (설정 > 채널 관리에서 추가하세요).")
        return 1

    start = time.time()

    up_ok, up_fail = _process_queue(cfg, channels, logger)

    elapsed_str = _format_elapsed(time.time() - start)

    if up_ok == 0 and up_fail == 0:
        # 큐가 비어 있어서 조용히 끝남 — Worker의 안전망 cron이 실제로 처리할
        # 게 없는데도 매번 부를 수 있어서, 이 경우는 알림 없이 조용히 종료함.
        logger.info("daily_generate: 처리할 대기열이 없어 종료 (%s 소요)", elapsed_str)
        return 0

    logger.info(
        "daily_generate: 전체 종료 (영상 성공 %d개/실패 %d개, %s 소요)",
        up_ok, up_fail, elapsed_str,
    )
    summary = f"영상 {up_ok}개 업로드 성공" + (f" · {up_fail}개 실패" if up_fail else "") + f" · {elapsed_str} 소요"
    notify("자동화 완료", summary)
    notify_discord(("✅" if up_fail == 0 else "⚠️") + f" 자동화 완료 — {summary}")
    return 0 if up_ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

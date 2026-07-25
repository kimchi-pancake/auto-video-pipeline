"""
scripts/daily_generate.py
==========================
GUI 없이 돌아가는 헤드리스 스크립트. 설정에 등록된 모든 채널에 대해:

  1. 하루치 대본(채널당 롱폼 1개 + 쇼츠 2개)을 Claude API로 생성해서 대기열에 저장
  2. 대기열에 쌓인 모든 파일을 실제로 영상으로 만들고 YouTube에 업로드까지 진행

Windows 작업 스케줄러가 매일 이 스크립트를 실행합니다 — 이 한 번의 실행으로
"대본 작성 → 영상 생성 → 업로드"가 전부 끝납니다.

수동 실행: python scripts/daily_generate.py
"""

from __future__ import annotations

import json
import re
import sys
import threading
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
from core.ai_script_generator import ScriptGenerationError, generate_daily_batch
from core.batch_runner import BatchRunner
from core.topic_queue import pop_topic
from core.video_registry import record_upload
from utils.soft_approval import is_rejected, publish_video, schedule_publish, sleep_until

# 모듈 레벨 로거. _lock_in_schedule는 threading.Thread의 target으로 직접
# 호출되는(main()의 지역 logger를 인자로 못 넘기는) 함수라 따로 필요합니다.
_module_logger = get_logger(__name__)

# 채널별 "오늘 이미 대본 생성했음" 기록. 같은 날 이 스크립트를 두 번 돌려도
# (예: 스케줄 실행 후 수동 테스트) 대본이 중복으로 쌓이지 않도록 막습니다.
_STATE_PATH = ROOT / "config" / "daily_generate_state.json"

_KST = pytz.timezone("Asia/Seoul")


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


def _generate_scripts(cfg, channels, logger) -> tuple[int, int]:
    """모든 채널에 하루치 대본을 생성합니다. 채널별로 오늘 이미 생성했으면
    건너뜁니다. (성공 채널 수, 저장된 파일 총수)를 반환."""
    today = date.today().isoformat()
    state = _load_state()

    logger.info("daily_generate: %d개 채널 대상으로 하루치 대본 생성 시작", len(channels))
    ok_channels = 0
    saved_total = 0
    for chan in channels:
        name = chan.get("name") or "(이름없음)"

        if state.get(name) == today:
            logger.info("daily_generate: '%s' 오늘 이미 대본 생성함 — 건너뜀", name)
            ok_channels += 1
            continue

        input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)
        topic = pop_topic(name, today)
        if topic:
            logger.info("daily_generate: '%s' 디스코드로 예약된 주제 사용 — %s", name, topic)
        try:
            meta_list: list[dict] = []
            saved = generate_daily_batch(input_dir, custom_topic=topic, channel=name, meta_out=meta_list)
            logger.info("daily_generate: '%s' 대본 생성 완료 — %d개 저장", name, len(saved))
            for meta in meta_list:
                logger.info(
                    "daily_generate: '%s' 대본 — 제목 \"%s\" · 확장 %s회 · 분량기준 %s",
                    name, meta.get("title", "?"), meta.get("extends", 0),
                    "충족" if meta.get("length_ok") else "미달",
                )
            ok_channels += 1
            saved_total += len(saved)
            state[name] = today
            _save_state(state)
        except ScriptGenerationError as e:
            logger.error("daily_generate: '%s' 대본 생성 전체 실패 — %s", name, e)
    logger.info("daily_generate: 대본 생성 종료 (%d/%d 채널 성공)", ok_channels, len(channels))
    return ok_channels, saved_total


def _lock_in_schedule(name: str, chan: dict, video_id: str, title: str, kind: str, lock_at: datetime, publish_at: datetime, thread_id: str | None = None) -> None:
    """lock_at까지 기다렸다가(그동안 /영상 거절 가능), 거절 안 됐으면 그때서야
    유튜브에 publish_at 예약공개를 겁니다. 생성이 끝나자마자 바로 예약해버리면
    거절 창이 사실상 없는 셈이라, "예약을 거는 행위" 자체를 lock_at까지 미룹니다.

    lock_at/publish_at은 호출 시점에 "오늘 이미 지났으면" 절대 하루씩 미루지
    않습니다(과거에 이 로직이 다음날로 미루다가, GitHub Actions 잡의
    350분 타임아웃보다 오래 자야 해서 예약이 영영 안 걸린 채로 죽는 사고가
    있었습니다) — 대신 lock_at은 "이미 지났으면 지금 바로", publish_at도
    "이미 지났으면 예약 대신 즉시 공개"로 처리합니다.

    lock_at 자체가 CI 타임아웃 예산을 넘을 만큼 멀면(예: 이른 시각에 수동
    트리거해서 review_lock까지 몇 시간씩 남은 경우) 그 시각까지 다 기다리지
    않고 예산 한도에서 앞당겨 lock-in합니다 — 거절 창은 줄어들지만, 예약
    자체가 통째로 유실되는 것보다는 훨씬 낫습니다."""
    deadline = _JOB_STARTED_AT + timedelta(minutes=_JOB_LOCK_IN_BUDGET_MINUTES)
    safe_lock_at = min(lock_at, deadline)
    if safe_lock_at < lock_at:
        _module_logger.warning(
            "daily_generate: '%s' review_lock(%s)까지 다 기다리면 CI 타임아웃을 넘길 것 "
            "같아 %s로 앞당겨 lock-in합니다(거절 창 축소)",
            name, lock_at.isoformat(), safe_lock_at.isoformat(),
        )
    sleep_until(safe_lock_at)
    if is_rejected(video_id):
        notify_discord(f"🚫 [{name}] 거절됨 — 비공개로 유지: {title}", thread_id=thread_id)
        return

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


# lock-in 대기 스레드들. main()이 끝나기 전에 전부 join해서, lock_at까지
# 기다리는 도중 프로세스가 먼저 끝나 daemon 스레드가 죽는(=예약이 영영 안
# 걸리는) 일을 막습니다.
_pending_lock_threads: list = []
_pending_lock_lock = threading.Lock()

# GitHub Actions job의 timeout-minutes(daily.yml, 350분)을 넘기면 프로세스가
# 강제종료되면서 그 시점에 sleep_until(lock_at)로 대기 중이던 스레드가 통째로
# 죽어 스케줄링이 영영 안 걸립니다 — 2026-07-25 사고: 오전에 수동 트리거해서
# review_lock까지 대기시간이 거의 10시간이 됐고, 5시간50분 타임아웃에 걸려
# 강제종료되면서 그날 업로드된 영상 4개가 전부 예약 안 걸린 채 비공개로
# 방치됨. "거절 창을 최대한 보장"하는 것보다 "예약이 아예 안 걸리는 것"이
# 훨씬 나쁘므로, 대기시간이 안전 예산을 넘으면 거절 창을 줄여서라도(0분까지
# 줄어들 수 있음) 반드시 lock-in을 완료시킵니다.
_JOB_STARTED_AT = datetime.now(pytz.utc)
_JOB_LOCK_IN_BUDGET_MINUTES = 300  # daily.yml timeout-minutes(350)보다 50분 여유


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
            lock_hour = cfg.get("youtube.review_lock_hour", 19)
            lock_minute = cfg.get("youtube.review_lock_minute", 40)
            publish_hour = cfg.get("youtube.schedule_hour", 20)
            publish_minute = cfg.get("youtube.schedule_minute", 0)
            now = datetime.now(_KST)
            lock_at = now.replace(hour=lock_hour, minute=lock_minute, second=0, microsecond=0)
            publish_at = now.replace(hour=publish_hour, minute=publish_minute, second=0, microsecond=0)
            if publish_at <= lock_at:
                publish_at += timedelta(days=1)
            # 생성이 오래 걸려서 이미 잠금 시각이 지났으면, 하루씩 미루지 않고
            # 지금 바로 처리(=대기 없이 곧장 거절 여부 확인 후 예약/공개)합니다.
            if lock_at <= now:
                lock_at = now

            t = threading.Thread(
                target=_lock_in_schedule,
                args=(name, chan, result.youtube_video_id, result.title or title, kind, lock_at, publish_at, thread_id),
                daemon=True,
            )
            t.start()
            with _pending_lock_lock:
                _pending_lock_threads.append(t)
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
    # 공개가 목표인 채널이어도 일단 비공개로만 올립니다 — 예약(publishAt)을
    # 언제 걸지는 _handle_result의 lock-in 스레드가 review_lock 시각까지
    # 기다렸다가 결정합니다(그 전에 /영상 거절 가능).
    upload_privacy = "private" if default_privacy == "public" else default_privacy
    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)

    counters = {"uploaded": 0, "made_but_not_uploaded": 0, "fail": 0}

    def _on_file_done(story_path: str, result, tid) -> None:
        _handle_result(name, chan, cfg, default_privacy, story_path, result, counters, logger, thread_id=tid)

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
    channel_names = ", ".join(c.get("name", "?") for c in channels)
    notify("자동화 시작", f"{len(channels)}개 채널 — 대본 생성 후 영상 제작·업로드까지 진행합니다.")
    notify_discord(f"🎬 자동화 시작 — {len(channels)}개 채널({channel_names}) 대본 생성 후 영상 제작·업로드 진행")

    gen_ok, gen_saved = _generate_scripts(cfg, channels, logger)
    logger.info("daily_generate: 대본 생성 완료 — %d/%d개 채널, 총 %d개 저장", gen_ok, len(channels), gen_saved)

    up_ok, up_fail = _process_queue(cfg, channels, logger)

    # 채널 처리는 다 끝났어도, review_lock 시각까지 기다리는 lock-in 스레드가
    # 아직 돌고 있을 수 있음 — 안 기다리고 프로세스가 끝나면 daemon 스레드가
    # 죽어서 예약이 영영 안 걸립니다.
    with _pending_lock_lock:
        threads = list(_pending_lock_threads)
    for t in threads:
        t.join()

    elapsed_str = _format_elapsed(time.time() - start)

    logger.info(
        "daily_generate: 전체 종료 (대본 %d/%d 채널, 영상 성공 %d개/실패 %d개, %s 소요)",
        gen_ok, len(channels), up_ok, up_fail, elapsed_str,
    )
    summary = (
        f"대본 {gen_saved}개 생성 · 영상 {up_ok}개 업로드 성공"
        + (f" · {up_fail}개 실패" if up_fail else "")
        + f" · {elapsed_str} 소요"
    )
    notify("자동화 완료", summary)
    notify_discord(("✅" if up_fail == 0 else "⚠️") + f" 자동화 완료 — {summary}")
    return 0 if (gen_ok > 0 or up_ok > 0) else 1


if __name__ == "__main__":
    sys.exit(main())

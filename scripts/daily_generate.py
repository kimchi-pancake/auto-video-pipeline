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
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify, notify_discord
from core.ai_script_generator import ScriptGenerationError, generate_daily_batch
from core.batch_runner import BatchRunner
from core.topic_queue import pop_topic
from core.video_registry import record_upload
from utils.soft_approval import is_rejected, publish_video, sleep_for_review

# 채널별 "오늘 이미 대본 생성했음" 기록. 같은 날 이 스크립트를 두 번 돌려도
# (예: 스케줄 실행 후 수동 테스트) 대본이 중복으로 쌓이지 않도록 막습니다.
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
            notify_discord(f"🎯 [{name}] 오늘은 예약된 주제로 생성함 — \"{topic}\"")
        try:
            meta_list: list[dict] = []
            saved = generate_daily_batch(input_dir, custom_topic=topic, channel=name, meta_out=meta_list)
            logger.info("daily_generate: '%s' 대본 생성 완료 — %d개 저장", name, len(saved))
            for meta in meta_list:
                scores = meta.get("scores") or {}
                score_str = (
                    f"후킹 {scores.get('hook', '?')} · 감정 {scores.get('emotion', '?')} · 결말 {scores.get('ending', '?')}"
                    if scores else "검수 점수 없음"
                )
                notify_discord(f"📝 [{name}] 대본 준비됨 — \"{meta.get('title')}\"\n{score_str}")
            ok_channels += 1
            saved_total += len(saved)
            state[name] = today
            _save_state(state)
        except ScriptGenerationError as e:
            logger.error("daily_generate: '%s' 대본 생성 전체 실패 — %s", name, e)
    logger.info("daily_generate: 대본 생성 종료 (%d/%d 채널 성공)", ok_channels, len(channels))
    return ok_channels, saved_total


def _hold_then_publish(name: str, chan: dict, video_id: str, title: str, kind: str) -> None:
    """소프트 승인: 미리보기+점수를 올리고 REVIEW_WINDOW 동안 기다렸다가,
    /영상 거절이 없었으면 공개로 전환합니다. 실패 시에도 파이프라인 전체는
    멈추지 않습니다(다음 영상 처리는 계속 진행)."""
    notify_discord(
        f"👀 [{name}] {kind} 미리보기 — {title}\n"
        f"https://youtu.be/{video_id}\n"
        f"5분 안에 `/영상 거절 {video_id}` 안 하면 자동으로 공개됨."
    )
    sleep_for_review()
    if is_rejected(video_id):
        notify_discord(f"🚫 [{name}] 거절됨 — 비공개로 유지: {title}")
        return
    if publish_video(video_id, chan.get("credentials_file", "")):
        notify_discord(f"✅ [{name}] {kind} 공개 전환 완료 — {title}\nhttps://youtu.be/{video_id}")
    else:
        notify_discord(f"⚠️ [{name}] 공개 전환 실패 — 유튜브 스튜디오에서 직접 공개로 바꿔야 함: {title}")


# 채널별로 스레드에서 띄우는 "5분 승인 대기 → 공개 전환" 백그라운드 작업들.
# main()이 끝나기 전에 전부 join해서, 공개 전환이 실제로 끝나기 전에 프로세스가
# 종료돼 daemon 스레드가 통째로 죽는(=영상이 영원히 비공개로 남는) 일을 막습니다.
_pending_publish_threads: list = []
_pending_publish_lock = threading.Lock()


def _handle_result(name: str, chan: dict, default_privacy: str, result, counters: dict, logger) -> None:
    """파일 하나 처리가 끝날 때마다(배치 전체가 아니라) 바로 불려서, 그
    영상에 대한 디스코드 알림을 즉시 보냅니다 — 채널 대기열 전체(롱폼+쇼츠2)가
    다 끝날 때까지 기다렸다가 한꺼번에 보내면, 롱폼 하나 렌더링에만 1시간
    가까이 걸리는 상황에서 사용자가 한참 동안 아무 알림도 못 받게 됩니다."""
    title = _extract_title(result.video_path)
    is_shorts = bool(result.thumbnail_shorts_path) and not result.thumbnail_long_path
    kind = "쇼츠" if is_shorts else "롱폼"

    # r.success는 "영상 파일 자체가 만들어졌는지"만 뜻함 (core/pipeline.py 설계상
    # 업로드 실패해도 영상 생성은 성공으로 침). 실제 유튜브 업로드 여부는
    # youtube_video_id가 채워졌는지로 따로 확인해야 함 — 안 그러면 업로드가
    # 조용히 실패해도 "성공"으로 보고돼서 놓치게 됨 (실제로 한 번 겪은 버그).
    if result.success and result.youtube_video_id:
        counters["uploaded"] += 1
        record_upload(result.youtube_video_id, name, result.category, result.title or title, is_shorts)
        if default_privacy == "public":
            # 5분 승인 대기(sleep_for_review)를 여기서 그냥 기다리면 배치 루프가
            # 막혀서 다음 영상 생성이 5분씩 밀립니다 — 별도 스레드로 돌려서
            # 승인 대기 중에도 다음 파일 처리가 바로 이어지게 함. daemon=True라
            # 메인 프로세스가 먼저 끝나면 죽어버리므로, main()에서 종료 전에
            # pending_threads를 전부 join해서 승인/공개 전환이 실제로 끝나게 함.
            t = threading.Thread(
                target=_hold_then_publish,
                args=(name, chan, result.youtube_video_id, result.title or title, kind),
                daemon=True,
            )
            t.start()
            with _pending_publish_lock:
                _pending_publish_threads.append(t)
        else:
            notify_discord(
                f"✅ [{name}] {kind} 업로드 완료(비공개) — {title}\n"
                f"https://youtu.be/{result.youtube_video_id}"
            )
    elif result.success and not result.youtube_video_id:
        counters["made_but_not_uploaded"] += 1
        logger.error(
            "daily_generate: '%s' 영상 생성됨 — 업로드 실패: %s (%s)",
            name, title, result.youtube_error,
        )
        notify_discord(
            f"⚠️ [{name}] {kind} 영상은 만들어졌지만 업로드 실패 — {title}\n"
            f"사유: {result.youtube_error or '알 수 없는 오류'}"
        )
    else:
        counters["fail"] += 1
        notify_discord(f"🔴 [{name}] {kind} 영상 생성 자체가 실패 — {title}\n사유: {result.error or '알 수 없는 오류'}")


def _process_channel(cfg, chan, logger) -> tuple[int, int]:
    """채널 하나의 대기열을 처리합니다. (성공 개수, 실패 개수)를 반환."""
    name = chan.get("name") or "(이름없음)"
    default_privacy = cfg.get("youtube.default_privacy", "private")
    # 공개가 목표인 채널이면 일단 비공개로 올려서 소프트 승인 창을 거치고,
    # 원래 설정이 private인 채널이면 그대로 private로 둡니다(승인 절차 불필요).
    upload_privacy = "private" if default_privacy == "public" else default_privacy
    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)

    counters = {"uploaded": 0, "made_but_not_uploaded": 0, "fail": 0}

    def _on_file_done(story_path: str, result) -> None:
        _handle_result(name, chan, default_privacy, result, counters, logger)


    runner = BatchRunner(
        cfg,
        input_dir,
        youtube_credentials_file=chan.get("credentials_file", ""),
        archive_subdir=name,
        discord_status=True,
        on_file_done=_on_file_done,
    )
    pending = runner.get_pending_files()
    if not pending:
        logger.info("daily_generate: '%s' 처리할 대기 파일 없음", name)
        return 0, 0

    logger.info("daily_generate: '%s' 영상 생성+업로드 시작 (%d개)", name, len(pending))
    notify_discord(f"▶️ [{name}] 영상 생성+업로드 시작 — {len(pending)}개 대기 중")
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
        notify_discord(f"🔴 [{name}] 배치 처리 중 예외 발생: {e}")
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

    if gen_saved:
        notify_discord(f"📝 대본 생성 완료 — {gen_ok}/{len(channels)}개 채널, 총 {gen_saved}개 저장")

    up_ok, up_fail = _process_queue(cfg, channels, logger)

    # 채널 처리는 다 끝났어도, 소프트 승인(5분 대기 후 공개 전환) 스레드가 아직
    # 돌고 있을 수 있음 — 이걸 안 기다리고 프로세스가 끝나면 daemon 스레드가
    # 그대로 죽어서 영상이 영원히 비공개로 남습니다.
    with _pending_publish_lock:
        threads = list(_pending_publish_threads)
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

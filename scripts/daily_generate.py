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
import sys
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
        try:
            saved = generate_daily_batch(input_dir)
            logger.info("daily_generate: '%s' 대본 생성 완료 — %d개 저장", name, len(saved))
            ok_channels += 1
            saved_total += len(saved)
            state[name] = today
            _save_state(state)
        except ScriptGenerationError as e:
            logger.error("daily_generate: '%s' 대본 생성 전체 실패 — %s", name, e)
    logger.info("daily_generate: 대본 생성 종료 (%d/%d 채널 성공)", ok_channels, len(channels))
    return ok_channels, saved_total


def _process_channel(cfg, chan, logger) -> tuple[int, int]:
    """채널 하나의 대기열을 처리합니다. (성공 개수, 실패 개수)를 반환."""
    name = chan.get("name") or "(이름없음)"
    privacy = cfg.get("youtube.default_privacy", "private")
    input_dir = _resolve_input_dir(chan.get("input_dir", ""), cfg)
    runner = BatchRunner(
        cfg,
        input_dir,
        youtube_credentials_file=chan.get("credentials_file", ""),
        archive_subdir=name,
    )
    pending = runner.get_pending_files()
    if not pending:
        logger.info("daily_generate: '%s' 처리할 대기 파일 없음", name)
        return 0, 0

    logger.info("daily_generate: '%s' 영상 생성+업로드 시작 (%d개)", name, len(pending))
    try:
        results = runner.run(
            count=len(pending),
            upload_to_youtube=True,
            youtube_privacy=privacy,
            schedule_days_ahead=0,
            also_make_shorts=False,
        )
    except Exception:
        logger.exception("daily_generate: '%s' 배치 처리 중 예외 발생", name)
        return 0, 0

    # r.success는 "영상 파일 자체가 만들어졌는지"만 뜻함 (core/pipeline.py 설계상
    # 업로드 실패해도 영상 생성은 성공으로 침). 실제 유튜브 업로드 여부는
    # youtube_video_id가 채워졌는지로 따로 확인해야 함 — 안 그러면 업로드가
    # 조용히 실패해도 "성공"으로 보고돼서 놓치게 됨 (실제로 한 번 겪은 버그).
    uploaded = sum(1 for r in results if r.success and r.youtube_video_id)
    made_but_not_uploaded = sum(1 for r in results if r.success and not r.youtube_video_id)
    fail = len(results) - uploaded - made_but_not_uploaded
    if made_but_not_uploaded:
        logger.error(
            "daily_generate: '%s' 영상 %d개는 생성됐지만 유튜브 업로드 실패함 (인증 문제 가능성)",
            name, made_but_not_uploaded,
        )
        for r in results:
            if r.success and not r.youtube_video_id:
                notify_discord(f"⚠️ '{name}' 채널 영상 생성됐지만 업로드 실패: {r.youtube_error or '알 수 없는 오류'}")
    logger.info("daily_generate: '%s' 처리 완료 — 업로드 성공 %d개, 실패 %d개", name, uploaded, fail + made_but_not_uploaded)
    return uploaded, fail + made_but_not_uploaded


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

    notify("자동화 시작", f"{len(channels)}개 채널 — 대본 생성 후 영상 제작·업로드까지 진행합니다.")
    notify_discord(f"🎬 자동화 시작 — {len(channels)}개 채널 대본 생성 후 영상 제작·업로드 진행")

    gen_ok, gen_saved = _generate_scripts(cfg, channels, logger)
    up_ok, up_fail = _process_queue(cfg, channels, logger)

    logger.info(
        "daily_generate: 전체 종료 (대본 %d/%d 채널, 영상 성공 %d개/실패 %d개)",
        gen_ok, len(channels), up_ok, up_fail,
    )
    summary = (
        f"대본 {gen_saved}개 생성 · 영상 {up_ok}개 업로드 성공"
        + (f" · {up_fail}개 실패" if up_fail else "")
    )
    notify("자동화 완료", summary)
    notify_discord(("✅" if up_fail == 0 else "⚠️") + f" 자동화 완료 — {summary}")
    return 0 if (gen_ok > 0 or up_ok > 0) else 1


if __name__ == "__main__":
    sys.exit(main())

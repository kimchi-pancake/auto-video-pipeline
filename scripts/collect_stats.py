"""
scripts/collect_stats.py
==========================
config/video_registry.json에 기록된 영상들의 조회수/좋아요/댓글 수를
YouTube Data API로 걷어서 다시 registry에 채워 넣는 헤드리스 스크립트.
매일 한 번 정도 돌리면 됩니다 (collect_stats.yml 워크플로우가 자동 실행).

수동 실행: python scripts/collect_stats.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify_discord
from core.analytics_collector import collect_stats


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
        logger.warning("collect_stats: 등록된 채널이 없습니다.")
        return 1

    ok, fail = collect_stats(channels)
    if ok or fail:
        notify_discord(f"📈 조회수 데이터 수집 — 성공 {ok}건, 실패 {fail}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())

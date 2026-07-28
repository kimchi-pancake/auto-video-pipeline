"""
scripts/weekly_analysis.py
============================
채널별로 YouTube Analytics API에서 최근 30일 조회수/시청 지속률을 걷어서
카테고리별로 집계하고 config/category_performance.json에 저장하는 헤드리스
스크립트. core/performance_analysis.py의 summarize_for_prompt()가 이 파일을
읽어서 다음 주제 생성부터 자동으로 반영합니다 — 이 스크립트 자체가 뭘
"바꾸지는" 않고, 매일 도는 주제 생성이 참고할 판단 재료만 주 1회 갱신합니다.

주 1회 실행 (weekly_analytics.yml).
수동 실행: python scripts/weekly_analysis.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.notify import notify_discord
from core.youtube_analytics import fetch_channel_video_metrics
from core.performance_analysis import category_retention_stats

_OUT_PATH = ROOT / "config" / "category_performance.json"


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
        logger.warning("weekly_analysis: 등록된 채널이 없습니다.")
        return 1

    result: dict = {}
    report_lines = ["📊 **주간 소재 성과 분석** (최근 30일, 시청 지속률 기준)"]

    for chan in channels:
        name = chan.get("name") or "(이름없음)"
        creds_file = chan.get("credentials_file", "")
        try:
            metrics = fetch_channel_video_metrics(creds_file, days_back=30)
        except Exception as e:
            logger.exception("weekly_analysis: '%s' 지표 수집 실패", name)
            report_lines.append(f"⚠️ [{name}] 지표 수집 실패 — {e}")
            continue

        ranking = category_retention_stats(name, metrics)
        result[name] = {
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "categories": ranking,
        }

        if not ranking:
            report_lines.append(f"[{name}] 분석할 데이터 없음(신규 채널이거나 최근 조회수 없음)")
            continue

        top = ranking[:3]
        bottom = ranking[-3:] if len(ranking) > 3 else []
        report_lines.append(f"**[{name}]**")
        report_lines.append(
            "👍 " + ", ".join(
                f"{s['category']}({s['avg_retention']:.0f}%, {int(s['avg_views'])}회)" for s in top
            )
        )
        if bottom and bottom != top:
            report_lines.append(
                "👎 " + ", ".join(f"{s['category']}({s['avg_retention']:.0f}%)" for s in bottom)
            )
        logger.info(
            "weekly_analysis: '%s' 카테고리 %d개 집계 (1위: %s %.0f%%)",
            name, len(ranking), ranking[0]["category"], ranking[0]["avg_retention"],
        )

    _OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("weekly_analysis: %s 저장 완료", _OUT_PATH)

    notify_discord("\n".join(report_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

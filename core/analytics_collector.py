"""
core/analytics_collector.py
=============================
video_registry.json에 기록된 영상들의 조회수/좋아요/댓글 수를 YouTube Data
API v3(videos.list part=statistics)로 걷어와 다시 registry에 채워 넣습니다.

주의: 시청 지속시간·시청률 같은 리텐션 지표는 YouTube Analytics API
(yt-analytics.readonly 스코프)가 따로 필요합니다 — 지금 저장된 채널
자격증명에는 그 스코프가 없어서 여기서는 조회수/좋아요/댓글 수만 걷습니다.
나중에 리텐션 분석까지 하려면 두 채널 다 그 스코프를 추가해서 재인증해야
합니다.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from utils.logger import get_logger
from core.video_registry import entries_needing_stats, update_stats

logger = get_logger(__name__)


def _build_client(credentials_file: str):
    creds = Credentials.from_authorized_user_file(credentials_file)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def collect_stats(channels: list[dict], min_age_hours: int = 24) -> tuple[int, int]:
    """등록된 채널들의 credentials_file을 이용해, 업로드된 지 min_age_hours가
    지났고 아직 통계가 없는 영상들의 조회수/좋아요/댓글 수를 걷어 registry에
    채웁니다. (수집 성공 개수, 실패 개수)를 반환합니다."""
    pending = entries_needing_stats(min_age_hours=min_age_hours)
    if not pending:
        logger.info("analytics_collector: 수집할 영상 없음")
        return 0, 0

    creds_by_channel = {c.get("name"): c.get("credentials_file", "") for c in channels}

    ok = 0
    fail = 0
    # 채널별로 묶어서 클라이언트를 한 번만 만들고, 최대 50개씩 배치 조회
    by_channel: dict[str, list[dict]] = {}
    for e in pending:
        by_channel.setdefault(e["channel"], []).append(e)

    for channel, entries in by_channel.items():
        creds_file = creds_by_channel.get(channel)
        if not creds_file:
            logger.warning("analytics_collector: '%s' 채널 자격증명을 못 찾음 — 건너뜀", channel)
            fail += len(entries)
            continue
        try:
            yt = _build_client(creds_file)
        except Exception as e:
            logger.error("analytics_collector: '%s' 인증 실패 — %s", channel, e)
            fail += len(entries)
            continue

        for i in range(0, len(entries), 50):
            batch = entries[i:i + 50]
            ids = [e["video_id"] for e in batch]
            try:
                resp = yt.videos().list(part="statistics", id=",".join(ids)).execute()
            except Exception as e:
                logger.error("analytics_collector: '%s' 통계 조회 실패 — %s", channel, e)
                fail += len(batch)
                continue

            stats_by_id = {item["id"]: item.get("statistics", {}) for item in resp.get("items", [])}
            for e in batch:
                stats = stats_by_id.get(e["video_id"])
                if stats is None:
                    # 삭제됐거나 비공개 전환 등 — 다음 수집 때 다시 시도
                    fail += 1
                    continue
                update_stats(e["video_id"], {
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                })
                ok += 1

    logger.info("analytics_collector: 수집 완료 (성공 %d개, 실패 %d개)", ok, fail)
    return ok, fail

"""
core/youtube_analytics.py
===========================
YouTube Analytics API(v2)로 조회수/시청 지속률(retention) 같은, YouTube Data
API(core/analytics_collector.py)로는 못 얻는 지표를 가져옵니다.

주의: 노출수/노출 클릭률(CTR)은 유튜브 스튜디오 UI 전용 지표라 공개
Analytics API에 없습니다(2026-07 확인) — averageViewPercentage(시청
지속률)가 "이야기가 끝까지 붙잡아두는지"를 보여주는 가장 가까운 대체
지표라 이걸 씁니다.

이 API를 쓰려면 채널 자격증명에 yt-analytics.readonly 스코프가 있어야
합니다(youtube/youtube_uploader.py의 SCOPES). 기존에 그 스코프 없이
인증된 자격증명 파일은 tools/reauthorize_youtube.py로 재인증해야
반영됩니다.
"""

from __future__ import annotations

from datetime import date, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils.logger import get_logger

logger = get_logger(__name__)


def _build_client(credentials_file: str):
    creds = Credentials.from_authorized_user_file(credentials_file)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtubeAnalytics", "v2", credentials=creds)


def fetch_channel_video_metrics(credentials_file: str, days_back: int = 30) -> dict[str, dict]:
    """최근 days_back일 동안 조회수가 1 이상 발생한 모든 영상의
    {video_id: {"views", "avg_view_duration", "avg_view_percentage"}}를
    한 번의 쿼리로 가져옵니다. 그 기간에 조회수가 없던 영상은 결과에
    아예 안 나타납니다(신규 영상은 다음 주 분석부터 반영됨)."""
    end = date.today()
    start = end - timedelta(days=days_back)

    yt = _build_client(credentials_file)
    resp = yt.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,averageViewDuration,averageViewPercentage",
        dimensions="video",
        sort="-views",
        maxResults=200,  # 이 프로젝트 규모(하루 2~4개)면 30일치로는 절대 안 넘음
    ).execute()

    result: dict[str, dict] = {}
    for row in resp.get("rows") or []:
        video_id, views, avg_dur, avg_pct = row
        result[video_id] = {
            "views": int(views),
            "avg_view_duration": float(avg_dur),
            "avg_view_percentage": float(avg_pct),
        }

    logger.info(
        "youtube_analytics: %s~%s 기간 %d개 영상 지표 수집",
        start.isoformat(), end.isoformat(), len(result),
    )
    return result

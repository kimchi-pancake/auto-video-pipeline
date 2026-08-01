"""
utils/soft_approval.py
========================
유튜브 공개/예약공개 전환 헬퍼.

2026-08-01: "소프트 승인"(디스코드 /영상 거절로 일정 시간 안에 취소 가능한 대기
창) 기능을 제거했습니다 — daily.yml에서 이 대기(review_lock까지 매번 최소
3시간40분)가 GitHub Actions 과금의 실제 원인으로 드러나서, 대기 없이 생성
직후 바로 예약/공개하는 방식으로 바꿨습니다.
"""

from __future__ import annotations

from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils.logger import get_logger

logger = get_logger(__name__)


def publish_video(video_id: str, credentials_file: str) -> bool:
    """videos.update로 privacyStatus를 public으로 바꿉니다."""
    try:
        creds = Credentials.from_authorized_user_file(credentials_file)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        yt = build("youtube", "v3", credentials=creds)
        yt.videos().update(
            part="status", body={"id": video_id, "status": {"privacyStatus": "public"}}
        ).execute()
        return True
    except Exception as e:
        logger.error("영상 공개 전환 실패 (video_id=%s): %s", video_id, e)
        return False


def schedule_publish(video_id: str, credentials_file: str, publish_at: datetime) -> bool:
    """videos.update로 예약공개(publishAt)를 겁니다 — 이 시점부터는 유튜브가
    스스로 publish_at 정각에 공개로 바꾸므로, 우리 쪽에서 그 시각까지 또
    깨어있을 필요가 없습니다."""
    try:
        creds = Credentials.from_authorized_user_file(credentials_file)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        yt = build("youtube", "v3", credentials=creds)
        yt.videos().update(
            part="status",
            body={
                "id": video_id,
                "status": {"privacyStatus": "private", "publishAt": publish_at.isoformat()},
            },
        ).execute()
        return True
    except Exception as e:
        logger.error("예약공개 설정 실패 (video_id=%s): %s", video_id, e)
        return False

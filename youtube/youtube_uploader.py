"""
youtube/youtube_uploader.py
===========================
YouTube Data API v3를 사용한 동영상 업로드 모듈.
OAuth2 인증, 예약 업로드, 썸네일 설정을 지원합니다.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from utils.logger import get_logger

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (ConnectionError, TimeoutError)


class YouTubeUploader:
    """
    사용 예:
        uploader = YouTubeUploader(config["youtube"])
        video_id = uploader.upload(
            video_path="output/video.mp4",
            title="제목",
            description="설명",
            tags=["태그1"],
            thumbnail_path="output/thumbnail_long.jpg",
            scheduled_at=datetime(2025, 1, 1, 18, 0, tzinfo=KST),
        )
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._secrets_file = config.get("client_secrets_file", "config/client_secrets.json")
        self._creds_file = config.get("credentials_file", "config/youtube_credentials.json")
        self._chunk_size = config.get("chunk_size", 10 * 1024 * 1024)
        self._retry_count = config.get("retry_count", 3)
        self._retry_delay = config.get("retry_delay", 10)
        self._default_category = config.get("default_category", "22")
        self._default_privacy = config.get("default_privacy", "private")
        self._default_language = config.get("default_language", "ko")
        self._tz_name = config.get("schedule_timezone", "Asia/Seoul")
        self._service = None

    # ─────────────────────────────────────────
    # 인증
    # ─────────────────────────────────────────

    def authenticate(self) -> bool:
        """OAuth2 인증을 수행합니다. 자격증명을 캐시합니다."""
        creds = None
        creds_path = Path(self._creds_file)

        if creds_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(creds_path), SCOPES)
            except Exception as e:
                logger.warning("Failed to load cached credentials: %s", e)

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("YouTube credentials refreshed.")
            except Exception as e:
                logger.warning("Credentials refresh failed: %s", e)
                creds = None

        if not creds or not creds.valid:
            secrets = Path(self._secrets_file)
            if not secrets.exists():
                logger.error(
                    "client_secrets.json not found: %s\n"
                    "Download from Google Cloud Console → APIs & Services → Credentials",
                    secrets,
                )
                return False
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0)

        # 저장
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("youtube", "v3", credentials=creds)
        logger.info("YouTube authentication successful.")
        return True

    def is_authenticated(self) -> bool:
        return self._service is not None

    # ─────────────────────────────────────────
    # 업로드
    # ─────────────────────────────────────────

    def upload(
        self,
        video_path: str | Path,
        title: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        privacy: str = "private",
        thumbnail_path: Optional[str | Path] = None,
        scheduled_at: Optional[datetime] = None,
        language: Optional[str] = None,
    ) -> Optional[str]:
        """
        동영상을 업로드하고 video_id를 반환합니다. 실패 시 None.

        Args:
            scheduled_at: 예약 공개 시각 (timezone-aware datetime). 
                          None이면 즉시 업로드.
            privacy: "private" | "public" | "unlisted"
        """
        if not self.is_authenticated():
            if not self.authenticate():
                return None

        video_path = Path(video_path)
        if not video_path.exists():
            logger.error("Video file not found: %s", video_path)
            return None

        # 제목 길이 제한
        max_title = self._cfg.get("max_title_length", 100)
        title = title[:max_title]

        # 예약 설정
        status_body: dict = {"privacyStatus": privacy}
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None:
                tz = pytz.timezone(self._tz_name)
                scheduled_at = tz.localize(scheduled_at)
            # 예약이면 privacyStatus는 반드시 "private" (API 요구사항)
            status_body = {
                "privacyStatus": "private",
                "publishAt": scheduled_at.isoformat(),
            }

        body = {
            "snippet": {
                "title": title,
                "description": description[:self._cfg.get("max_description_length", 5000)],
                "tags": tags or self._cfg.get("default_tags", []),
                "categoryId": category or self._default_category,
                "defaultLanguage": language or self._default_language,
                "defaultAudioLanguage": language or self._default_language,
            },
            "status": status_body,
        }

        media = MediaFileUpload(
            str(video_path),
            chunksize=self._chunk_size,
            resumable=True,
            mimetype="video/mp4",
        )

        logger.info("Uploading: %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)

        video_id = self._resumable_upload(body, media)
        if not video_id:
            return None

        logger.info("Upload complete. video_id=%s", video_id)

        # 썸네일 설정
        if thumbnail_path:
            self._set_thumbnail(video_id, str(thumbnail_path))

        return video_id

    # ─────────────────────────────────────────
    # 내부 구현
    # ─────────────────────────────────────────

    def _resumable_upload(self, body: dict, media: MediaFileUpload) -> Optional[str]:
        request = self._service.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media,
        )

        response = None
        for attempt in range(1, self._retry_count + 1):
            try:
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        pct = int(status.progress() * 100)
                        logger.debug("Upload progress: %d%%", pct)
                return response.get("id")
            except HttpError as e:
                if e.resp.status in RETRIABLE_STATUS_CODES:
                    logger.warning("Retriable HTTP error %d (attempt %d)", e.resp.status, attempt)
                else:
                    logger.error("Non-retriable HTTP error: %s", e)
                    return None
            except RETRIABLE_EXCEPTIONS as e:
                logger.warning("Retriable exception (attempt %d): %s", attempt, e)

            if attempt < self._retry_count:
                sleep = self._retry_delay * attempt
                logger.info("Retrying in %ds...", sleep)
                time.sleep(sleep)

        logger.error("Upload failed after %d attempts.", self._retry_count)
        return None

    def _set_thumbnail(self, video_id: str, thumbnail_path: str) -> bool:
        if not Path(thumbnail_path).exists():
            logger.warning("Thumbnail not found: %s", thumbnail_path)
            return False
        try:
            media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            self._service.thumbnails().set(
                videoId=video_id,
                media_body=media,
            ).execute()
            logger.info("Thumbnail set for video %s", video_id)
            return True
        except HttpError as e:
            logger.error("Set thumbnail failed: %s", e)
            return False

    # ─────────────────────────────────────────
    # 예약 시각 계산
    # ─────────────────────────────────────────

    def make_schedule_datetime(
        self,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        days_ahead: int = 0,
    ) -> datetime:
        """
        오늘 기준으로 days_ahead 일 뒤 hour:minute 의 예약 시각을 반환합니다.
        """
        from datetime import timedelta
        tz = pytz.timezone(self._tz_name)
        h = hour if hour is not None else self._cfg.get("schedule_hour", 18)
        m = minute if minute is not None else self._cfg.get("schedule_minute", 0)
        now = datetime.now(tz)
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        target += timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(days=1)
        return target

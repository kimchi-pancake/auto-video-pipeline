"""
utils/soft_approval.py
========================
"소프트 승인" 흐름 — 영상을 일단 비공개로 올려두고, REVIEW_WINDOW_SECONDS
동안 디스코드에서 /영상 거절이 안 오면 자동으로 공개 전환합니다.

기본값은 "거절 안 하면 바로 올리는" 쪽입니다 — 조회 실패 등 예외 상황에서도
전체 파이프라인이 멈추지 않도록 항상 "거절 안 됨"으로 간주(fail-open)합니다.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils.logger import get_logger

logger = get_logger(__name__)

REVIEW_WINDOW_SECONDS = 300  # 5분


def sleep_for_review(seconds: int = REVIEW_WINDOW_SECONDS) -> None:
    logger.info("소프트 승인 대기 중 (%d초)...", seconds)
    time.sleep(seconds)


def is_rejected(video_id: str) -> bool:
    """config/rejections.json을 GitHub API로 직접 조회합니다. 로컬 git 체크아웃은
    워크플로우 시작 시점 스냅샷이라 대기 중에 들어온 거절을 못 보므로, 항상
    GitHub에서 최신 상태를 다시 읽어야 합니다. 조회 실패 시 "거절 안 됨"으로
    간주합니다(자동 공개 우선)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/contents/config/rejections.json",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            return video_id in (content.get("rejected") or [])
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning("거절 여부 조회 실패 (HTTP %s), 거절 안 된 것으로 간주", e.code)
        return False
    except Exception as e:
        logger.warning("거절 여부 조회 실패: %s, 거절 안 된 것으로 간주", e)
        return False


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

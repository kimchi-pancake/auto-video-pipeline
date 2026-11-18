"""
v2/publish.py
==============
업로드와 예약공개. 원칙:
  - QC를 통과한 job만 올린다. 통과 못 하면 올리지 않고 NEEDS_REVIEW.
  - 같은 job은 두 번 올리지 않는다: job에 video_id가 있으면 재사용, 응답이 유실됐을 수 있으니 올리기 전에
    채널 업로드 목록(playlistItems, 1유닛)에서 같은 제목을 먼저 찾는다.
  - 업로드는 항상 private. publish_mode=="scheduled" 일 때만 publishAt(20:00 KST)을 건다. 그 외(review)는 비공개로 두고
    사람 승인(approve)을 기다린다. 예약 시각이 이미 지났으면 즉시 공개하지 않고 다음 날 같은 시각으로 미룬다.
  - 처리 완료(processed)를 확인한 뒤에야 예약을 건다.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.config_manager import get_config
from utils.logger import get_logger
from v2.channels import ChannelProfile, load_v2_config
from v2.jobstore import JobStore
from youtube.youtube_uploader import YouTubeUploader

logger = get_logger(__name__)
KST = pytz.timezone("Asia/Seoul")


def _service(creds_file: str):
    creds = Credentials.from_authorized_user_file(creds_file)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def next_slot(now: Optional[datetime] = None) -> datetime:
    cfg = load_v2_config()
    now = now or datetime.now(KST)
    slot = now.replace(hour=cfg["publish_hour_kst"], minute=cfg["publish_minute_kst"], second=0, microsecond=0)
    # 업로드·처리에 시간이 걸리므로 최소 15분 뒤여야 예약으로 인정. 지났으면 다음 날(즉시 공개 금지).
    if slot <= now + timedelta(minutes=15):
        slot += timedelta(days=1)
    return slot


def find_existing(yt, title: str) -> Optional[str]:
    """채널 업로드 재생목록의 최근 영상 중 같은 제목이 있으면 video_id (응답 유실 후 재업로드 방지)."""
    try:
        ch = yt.channels().list(part="contentDetails", mine=True).execute()
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=30).execute().get("items", [])
        for it in items:
            if it["snippet"]["title"].strip() == title.strip():
                return it["snippet"]["resourceId"]["videoId"]
    except HttpError as e:
        logger.warning("기존 업로드 조회 실패(계속 진행): %s", e)
    return None


def wait_processed(yt, video_id: str, timeout_s: int = 600) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        r = yt.videos().list(part="status,processingDetails", id=video_id).execute()
        it = (r.get("items") or [None])[0]
        if it:
            st = it["status"].get("uploadStatus")
            ps = (it.get("processingDetails") or {}).get("processingStatus")
            if st in ("rejected", "failed", "deleted"):
                raise RuntimeError(f"업로드 거부/실패: {st} {it['status'].get('rejectionReason') or it['status'].get('failureReason')}")
            if st == "processed" or ps == "succeeded":
                return True
        time.sleep(15)
    return False


def build_description(job: Dict) -> str:
    sc = job["script"]
    lines = [sc.get("title", ""), ""]
    # 한 줄 요약: 첫 장면 + 마지막 장면
    lines.append(sc["scenes"][-1]["narration"])
    lines += ["", "[출처]"]
    seen = set()
    for c in job.get("claims") or []:
        key = c.get("source_url")
        if key and key not in seen:
            seen.add(key)
            lines.append(f"- {c.get('source_title')} ({c.get('source_url')}) · 확인일 {c.get('checked_at')}")
    for a in job.get("assets") or []:
        src = a.get("meta", {}).get("source")
        if src and src["url"] not in seen:
            seen.add(src["url"])
            lines.append(f"- {src['title']} ({src['url']}) · 확인일 {src['fetched_at']}")
    photos = [a["meta"]["photo"] for a in (job.get("assets") or []) if a.get("meta", {}).get("photo")]
    if photos:
        lines += ["", "[사진 출처]"]
        for p in photos:
            lines.append(f"- {p.get('artist') or '작성자 미상'}, {p.get('license')}, {p.get('page')}")
    lines += ["", "이 영상은 위 자료를 바탕으로 AI 도구(대본 초안·합성 음성)를 사용해 제작되었습니다."]
    return "\n".join(lines)[:4900]


def upload_job(store: JobStore, job: Dict, profile: ChannelProfile) -> Dict:
    cfg_v2 = load_v2_config()
    if job["status"] not in ("READY_TO_UPLOAD", "UPLOADING", "RETRY_WAIT"):
        return job
    store.claim(job)
    store.transition(job, "UPLOADING", "업로드 시작")
    try:
        yt = _service(profile.credentials_file)
        title = f"{job['script']['title']} #Shorts"
        vid = job["youtube"].get("video_id") or find_existing(yt, title)
        if vid:
            logger.info("이미 업로드된 영상 재사용: %s", vid)
        else:
            ycfg = dict(get_config().section("youtube"))
            ycfg["credentials_file"] = profile.credentials_file
            up = YouTubeUploader(ycfg)
            if not up.authenticate():
                return store.fail(job, "upload", "auth", "YouTube 인증 실패")
            tags = ["Shorts", job["topic"].get("category", "")] + (ycfg.get("shorts_tags") or [])
            vid = up.upload(video_path=job["files"]["video"], title=title, description=build_description(job),
                            tags=[t for t in tags if t][:15], privacy="private",
                            thumbnail_path=job["files"].get("thumbnail"))
            if not vid:
                return store.fail(job, "upload", "service_transient", "업로드 결과 없음")
        job["youtube"]["video_id"] = vid
        job["youtube"]["privacy"] = "private"
        store.save(job)
        if not wait_processed(yt, vid):
            return store.fail(job, "upload", "service_transient", "처리 완료 대기 시간 초과")

        if cfg_v2["publish_mode"] == "scheduled":
            slot = next_slot()
            yt.videos().update(part="status", body={"id": vid, "status": {"privacyStatus": "private", "publishAt": slot.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                                                           "selfDeclaredMadeForKids": False}}).execute()
            job["youtube"]["scheduled_at"] = slot.isoformat()
            store.transition(job, "SCHEDULED", f"예약공개 {slot:%m-%d %H:%M}")
        else:
            store.transition(job, "NEEDS_REVIEW", "비공개 업로드 완료 — 사람 승인 대기(publish_mode=review)")
        store.release(job)
        return job
    except HttpError as e:
        status = getattr(e.resp, "status", 0)
        cls = "rate_limit" if status in (403, 429) and "quota" in str(e).lower() else ("auth" if status in (401,) else "service_transient")
        return store.fail(job, "upload", cls, str(e)[:300])
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        cls = "auth" if "자격증명" in msg or "invalid_grant" in msg else "service_transient"
        return store.fail(job, "upload", cls, msg[:300])


def approve(store: JobStore, job_id: str, profile: ChannelProfile) -> Dict:
    """review 모드에서 사람이 승인하면 처리 완료를 확인하고 다음 슬롯(20:00 KST)에 예약공개를 건다."""
    job = store.get(job_id)
    vid = job["youtube"].get("video_id")
    if not vid:
        raise RuntimeError("업로드된 영상이 없습니다")
    yt = _service(profile.credentials_file)
    slot = next_slot()
    yt.videos().update(part="status", body={"id": vid, "status": {"privacyStatus": "private", "publishAt": slot.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                                                   "selfDeclaredMadeForKids": False}}).execute()
    job["youtube"]["scheduled_at"] = slot.isoformat()
    return store.transition(job, "SCHEDULED", f"승인 → 예약공개 {slot:%m-%d %H:%M}")

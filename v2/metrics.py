"""
v2/metrics.py
==============
Performance Intelligence — 수집부. 업로드(예약공개) 후 1h·6h·24h·72h·7d·28d 시점에 성과 스냅샷을 남긴다.

공식 API가 실제로 주는 값만 기록한다:
  YouTube Data API   videos.list(statistics)               → views, likes, comments  (즉시)
  YouTube Analytics  reports.query(dimensions=video)       → views, engagedViews, estimatedMinutesWatched,
                       averageViewDuration, averageViewPercentage, likes, comments, shares, subscribersGained
                       (보통 24~48시간 지연 — 아직 없으면 null로 두고 다음 수집에서 보완)
Shorts '시청 선택률(Stayed to watch)', 피드 노출, 구간별 유지율은 공개 API 문서에 없어서 자동 수집하지 않는다
(YouTube Studio CSV를 data/manual/ 에 넣으면 import_studio_csv 가 읽는다).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils.logger import get_logger
from v2 import DATA
from v2.channels import ChannelProfile, load_v2_config
from v2.jobstore import JobStore, _iso, _now, _parse

logger = get_logger(__name__)
METRICS_DIR = DATA / "metrics"
ANALYTICS_METRICS = "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained"


def _creds(path: str) -> Credentials:
    c = Credentials.from_authorized_user_file(path)
    if c.expired and c.refresh_token:
        c.refresh(Request())
    return c


def load_snapshots(video_id: str) -> List[Dict]:
    p = METRICS_DIR / f"{video_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append(video_id: str, snap: Dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    with (METRICS_DIR / f"{video_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def published_at(job: Dict) -> Optional[datetime]:
    sa = job["youtube"].get("scheduled_at")
    if sa:
        return datetime.fromisoformat(sa).astimezone(timezone.utc)
    return None


def fetch_video_stats(creds: Credentials, video_id: str, since: datetime) -> Dict:
    yt = build("youtube", "v3", credentials=creds)
    r = yt.videos().list(part="statistics,status", id=video_id).execute()
    it = (r.get("items") or [None])[0]
    if not it:
        return {"missing": True}
    st = it.get("statistics", {})
    out = {"privacy": it["status"].get("privacyStatus"), "views": int(st.get("viewCount", 0)),
           "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)), "analytics": None}
    try:
        ya = build("youtubeAnalytics", "v2", credentials=creds)
        start = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = None
        for metrics in (ANALYTICS_METRICS, ANALYTICS_METRICS.replace("engagedViews,", "")):
            try:
                rep = ya.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics=metrics,
                                         dimensions="video", filters=f"video=={video_id}").execute()
                rows = (rep.get("rows") or [None])[0]
                names = [h["name"] for h in rep.get("columnHeaders", [])]
                if rows:
                    out["analytics"] = dict(zip(names, rows[:len(names)]))
                    out["analytics"].pop("video", None)
                break
            except Exception as e:  # noqa: BLE001 — engagedViews 미지원 계정 등
                logger.info("Analytics 조회 변형 실패(%s): %s", "engagedViews 포함" if "engagedViews" in metrics else "제외", str(e)[:100])
    except Exception as e:  # noqa: BLE001
        logger.info("Analytics API 사용 불가: %s", str(e)[:100])
    return out


def collect(store: JobStore, profile: ChannelProfile) -> List[Dict]:
    """도래한 시점의 스냅샷을 기록하고, 공개가 확인된 SCHEDULED 영상을 PUBLISHED로 바꾼다."""
    cfg = load_v2_config()
    taken: List[Dict] = []
    if not profile.credentials_file:
        return taken
    creds = _creds(profile.credentials_file)
    for job in store.list(profile.name, states=["SCHEDULED", "PUBLISHED"]):
        vid = job["youtube"].get("video_id")
        pub = published_at(job)
        if not vid or not pub:
            continue
        have = {s["age_h"] for s in load_snapshots(vid)}
        due = [a for a in cfg["metrics_ages_hours"] if a not in have and _now() >= pub + timedelta(hours=a)]
        if not due:
            continue
        stats = fetch_video_stats(creds, vid, pub)
        if stats.get("missing"):
            logger.warning("영상 %s 조회 불가(삭제/비공개?)", vid)
            continue
        if job["status"] == "SCHEDULED" and stats.get("privacy") == "public":
            store.transition(job, "PUBLISHED", "공개 확인")
        for age in due:
            snap = {"age_h": age, "taken_at": _iso(_now()), "job_id": job["id"], **stats}
            _append(vid, snap)
            taken.append(snap)
    return taken


def channel_daily(profile: ChannelProfile, days: int = 35) -> Dict[str, Dict]:
    """채널 전체 일간 지표(조회수·engagedViews·시청시간·구독 증가) — 7일/28일 합계의 근거."""
    if not profile.credentials_file:
        return {}
    ya = build("youtubeAnalytics", "v2", credentials=_creds(profile.credentials_file))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    metrics_try = ["views,engagedViews,estimatedMinutesWatched,subscribersGained", "views,estimatedMinutesWatched,subscribersGained"]
    for m in metrics_try:
        try:
            rep = ya.reports().query(ids="channel==MINE", startDate=str(start), endDate=str(end), metrics=m, dimensions="day", sort="day").execute()
            names = [h["name"] for h in rep["columnHeaders"]]
            out = {r[0]: dict(zip(names[1:], r[1:])) for r in rep.get("rows", [])}
            p = DATA / "channel_daily"
            p.mkdir(parents=True, exist_ok=True)
            path = p / f"{profile.key.split(':')[0]}.json"
            merged = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            merged.update(out)
            path.write_text(json.dumps(merged, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            return merged
        except Exception as e:  # noqa: BLE001
            logger.info("채널 일간 Analytics 실패(%s): %s", m, str(e)[:100])
    return {}


def import_studio_csv(path: Path) -> int:
    """YouTube Studio에서 내려받은 CSV(영상별 성과)를 data/manual/ 로 읽어 스냅샷으로 합친다. 컬럼명은 한/영 모두 시도."""
    n = 0
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vid = row.get("콘텐츠") or row.get("Content") or row.get("video_id") or ""
            if not vid:
                continue
            snap = {"age_h": None, "taken_at": _iso(_now()), "source": "studio_csv", "manual": {k: v for k, v in row.items()}}
            _append(vid, snap)
            n += 1
    return n

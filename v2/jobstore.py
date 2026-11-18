"""
v2/jobstore.py
===============
영상 작업(Job)의 영속 상태 관리.

저장소: data/jobs/{job_id}.json (영상 1개 = 파일 1개 → 서로 다른 작업끼리 git 충돌이 없다).
삭제 API는 일부러 두지 않는다. 같은 인터페이스로 Cloudflare D1 백엔드를 붙일 수 있게
Store 메서드(create/get/save/transition/claim/release/list)만 외부에 노출한다.

상태 머신
  NEW → RESEARCHING → VERIFIED → SCRIPT_READY → ASSETS_READY → RENDERING → QUALITY_CHECK
      → READY_TO_UPLOAD → UPLOADING → SCHEDULED → PUBLISHED
  어디서든 → RETRY_WAIT(서비스 장애, 자동 재시도) / NEEDS_REVIEW(품질·정책 문제, 사람 확인) / FAILED(영구 실패)
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from v2 import DATA

STATES = [
    "NEW", "RESEARCHING", "VERIFIED", "SCRIPT_READY", "ASSETS_READY", "RENDERING",
    "QUALITY_CHECK", "READY_TO_UPLOAD", "UPLOADING", "SCHEDULED", "PUBLISHED",
    "RETRY_WAIT", "NEEDS_REVIEW", "FAILED",
]
_ORDER = {s: i for i, s in enumerate(STATES[:11])}
TERMINAL = {"PUBLISHED", "FAILED"}

# 장애 분류 — 재시도 가능 여부와 대기시간(분)을 결정한다.
ERROR_CLASSES = {
    "service_transient": {"retry": True, "wait_min": 30},   # 5xx, 타임아웃, 모델 장애
    "rate_limit": {"retry": True, "wait_min": 120},          # 429, 쿼터
    "auth": {"retry": False, "wait_min": 0},                 # OAuth 만료 등 → 사람이 재인증
    "content_quality": {"retry": False, "wait_min": 0},      # 검증 실패, QC 실패 → NEEDS_REVIEW
    "bug": {"retry": True, "wait_min": 60},
}
MAX_ATTEMPTS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def slugify_channel(name: str) -> str:
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"ch{h}"


class TransitionError(RuntimeError):
    pass


class LeaseError(RuntimeError):
    pass


class JobStore:
    def __init__(self, root: Optional[Path] = None):
        self.dir = Path(root) if root else DATA / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── 기본 입출력 ──────────────────────────────────────
    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def exists(self, job_id: str) -> bool:
        return self._path(job_id).exists()

    def get(self, job_id: str) -> Dict[str, Any]:
        return json.loads(self._path(job_id).read_text(encoding="utf-8"))

    def save(self, job: Dict[str, Any]) -> None:
        job["updated_at"] = _iso(_now())
        tmp = self._path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path(job["id"]))

    def list(self, channel: Optional[str] = None, states: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        out = []
        want = set(states) if states else None
        for p in sorted(self.dir.glob("*.json")):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if channel and j.get("channel") != channel:
                continue
            if want and j.get("status") not in want:
                continue
            out.append(j)
        return out

    # ── 생성 ─────────────────────────────────────────────
    def create(self, channel: str, topic: Dict[str, Any], day: Optional[str] = None, extra: Optional[dict] = None) -> Dict[str, Any]:
        day = day or datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        seq = 1 + sum(1 for j in self.list(channel) if j["id"].split("-")[1] == day)
        key = hashlib.sha1(f"{channel}:{topic.get('title','')}:{time.time()}".encode()).hexdigest()[:5]
        job_id = f"{slugify_channel(channel)}-{day}-{seq:02d}-{key}"
        job = {
            "id": job_id,
            "channel": channel,
            "status": "NEW",
            "created_at": _iso(_now()),
            "topic": topic,
            "history": [{"status": "NEW", "at": _iso(_now()), "note": "created"}],
            "attempts": {},
            "error": None,
            "lease": None,
            "retry_after": None,
            "hook": None,
            "script": None,
            "claims": [],
            "assets": None,
            "files": {},
            "qc": None,
            "youtube": {"video_id": None, "scheduled_at": None, "privacy": None},
            "experiment": None,
        }
        if extra:
            job.update(extra)
        self.save(job)
        return job

    # ── 상태 전이 ─────────────────────────────────────────
    def transition(self, job: Dict[str, Any], new: str, note: str = "", **fields: Any) -> Dict[str, Any]:
        old = job["status"]
        if new not in STATES:
            raise TransitionError(f"unknown state {new}")
        if old in TERMINAL and new != old:
            raise TransitionError(f"{old} is terminal")
        if new in _ORDER and old in _ORDER and _ORDER[new] < _ORDER[old]:
            # 재시도 때 이전 단계로 되돌아가는 것은 허용하되 history에 남긴다.
            note = (note + " [rewind]").strip()
        job["status"] = new
        job["history"].append({"status": new, "at": _iso(_now()), "note": note})
        job.update(fields)
        self.save(job)
        return job

    def fail(self, job: Dict[str, Any], stage: str, error_class: str, message: str) -> Dict[str, Any]:
        """장애를 분류해서 RETRY_WAIT / NEEDS_REVIEW / FAILED 중 하나로 보낸다. 데이터는 지우지 않는다."""
        spec = ERROR_CLASSES.get(error_class, ERROR_CLASSES["bug"])
        n = job["attempts"].get(stage, 0) + 1
        job["attempts"][stage] = n
        job["error"] = {"stage": stage, "class": error_class, "message": message[:600], "at": _iso(_now())}
        job["lease"] = None
        if error_class == "content_quality":
            return self.transition(job, "NEEDS_REVIEW", f"{stage}: {message[:120]}")
        if error_class == "auth":
            return self.transition(job, "NEEDS_REVIEW", f"{stage}: 인증 문제 — {message[:100]}")
        if spec["retry"] and n < MAX_ATTEMPTS:
            wait = spec["wait_min"] * n
            return self.transition(job, "RETRY_WAIT", f"{stage} 실패({error_class}) {n}/{MAX_ATTEMPTS}",
                                   retry_after=_iso(_now() + timedelta(minutes=wait)))
        return self.transition(job, "FAILED", f"{stage}: {error_class} 반복 실패 — {message[:100]}")

    def due_for_retry(self, job: Dict[str, Any]) -> bool:
        if job["status"] != "RETRY_WAIT":
            return False
        ra = job.get("retry_after")
        return (not ra) or _parse(ra) <= _now()

    # ── 작업 잠금(lease) ─────────────────────────────────
    def claim(self, job: Dict[str, Any], owner: Optional[str] = None, ttl_min: int = 90) -> Dict[str, Any]:
        owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        lease = job.get("lease")
        if lease and _parse(lease["until"]) > _now() and lease["owner"] != owner:
            raise LeaseError(f"{job['id']} is leased by {lease['owner']} until {lease['until']}")
        job["lease"] = {"owner": owner, "until": _iso(_now() + timedelta(minutes=ttl_min))}
        self.save(job)
        return job

    def release(self, job: Dict[str, Any]) -> None:
        job["lease"] = None
        self.save(job)

    # ── 조회 헬퍼 ─────────────────────────────────────────
    def recent_titles(self, channel: str, limit: int = 60) -> List[str]:
        jobs = sorted(self.list(channel), key=lambda j: j["created_at"], reverse=True)
        titles = []
        for j in jobs[:limit]:
            t = (j.get("hook") or {}).get("title") or (j.get("topic") or {}).get("title")
            if t:
                titles.append(t)
        return titles

    def runnable(self, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        """다음 실행에서 이어서 처리할 작업(중간 상태 + 재시도 시각이 된 RETRY_WAIT)."""
        out = []
        for j in self.list(channel):
            s = j["status"]
            lease = j.get("lease")
            if lease and _parse(lease["until"]) > _now():
                continue
            if s in ("SCRIPT_READY", "ASSETS_READY", "RENDERING", "QUALITY_CHECK", "READY_TO_UPLOAD", "UPLOADING", "VERIFIED", "RESEARCHING", "NEW"):
                out.append(j)
            elif s == "RETRY_WAIT" and self.due_for_retry(j):
                out.append(j)
        return out

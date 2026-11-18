"""
v2/run.py — 단계 실행기.  python -m v2.run <plan|produce|publish|measure|all|status|approve JOB_ID> [--channel 도개] [--store DIR]

plan     오늘 만들 영상 기획(소재→자료→훅→대본·주장 검증). 이어서 해야 할 job이 있으면 먼저 이어서 진행.
produce  SCRIPT_READY job을 영상으로(자료 확보→TTS→자막→합성→썸네일→품질 검사).
publish  품질 검사를 통과한 job을 비공개 업로드하고, publish_mode가 scheduled면 20:00 KST 예약공개.
measure  성과 스냅샷 수집, 분석(insights.json), 실험 평가, 디스코드 요약.
모든 단계는 job 파일에 상태를 남기고 삭제하지 않는다. 서비스 장애는 RETRY_WAIT로 남아 다음 실행에서 이어진다.
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytz

from utils.logger import get_logger, setup_logging
from utils.notify import notify_discord
from v2 import DATA, ROOT, experiments, intelligence, metrics, plan, publish, qc, render
from v2.channels import ChannelProfile, active_profiles, load_v2_config
from v2.jobstore import JobStore, LeaseError

logger = get_logger("v2.run")
KST = pytz.timezone("Asia/Seoul")
PLAN_STAGES = {"research", "hook", "script", "plan"}
PRODUCE_STAGES = {"assets", "render", "qc"}
PUBLISH_STAGES = {"upload"}


def _profile_for(profiles: List[ChannelProfile], job: Dict) -> Optional[ChannelProfile]:
    return next((p for p in profiles if p.name == job["channel"]), None)


def _stage_of_retry(job: Dict) -> str:
    return (job.get("error") or {}).get("stage", "plan")


# ── plan ─────────────────────────────────────────────────

def stage_plan(store: JobStore, profiles: List[ChannelProfile], only: Optional[str] = None) -> None:
    cfg = load_v2_config()
    today = datetime.now(KST).strftime("%Y%m%d")
    for prof in profiles:
        if only and prof.name != only:
            continue
        # 1) 이어서 할 기획 job
        for job in store.runnable(prof.name):
            if job["status"] in ("NEW", "RESEARCHING", "VERIFIED") or (job["status"] == "RETRY_WAIT" and _stage_of_retry(job) in PLAN_STAGES):
                logger.info("기획 이어서: %s (%s)", job["id"], job["status"])
                try:
                    plan.advance(store, job, prof, (job.get("experiment") or {}) and {"experiment": job["experiment"]})
                except LeaseError as e:
                    logger.info("건너뜀: %s", e)
        # 2) 오늘 목표 편수 채우기 (오늘 만든 job 수 기준 — 실패한 job도 센다, 무한 재기획 방지)
        target = int(cfg["posts_per_day"].get(prof.name, 1))
        made = sum(1 for j in store.list(prof.name) if j["id"].split("-")[1] == today)
        need = max(target - made, 0)
        for _ in range(need):
            rng = random.Random()
            tmp_id = f"pending-{random.randint(0, 10**6)}"
            hints = experiments.assign(prof.name, tmp_id)
            job = plan.plan_one(prof, store, hints=hints, rng=rng)
            if job:
                # 임시 배정 키를 실제 job id로 교체
                d = experiments._load()
                if tmp_id in d["assignments"]:
                    d["assignments"][job["id"]] = d["assignments"].pop(tmp_id)
                    experiments._save(d)
                _notify_planned(job)


def _notify_planned(job: Dict) -> None:
    if job["status"] == "SCRIPT_READY":
        sc = job["script"]
        notify_discord(f"📝 [{job['channel']}] 대본 준비 — {sc['title']} · {sc['eval']['est_seconds']}초 · 장면 {sc['eval']['scenes']}개 · 근거 {len(job['claims'])}건 검증")
    else:
        notify_discord(f"⚠️ [{job['channel']}] 기획 보류({job['status']}) — {job['topic']['title']}: {(job.get('error') or {}).get('message','')[:120]}")


# ── produce ──────────────────────────────────────────────

def produce_job(store: JobStore, job: Dict, prof: ChannelProfile, workroot: Path) -> Dict:
    stage = "assets"
    try:
        store.claim(job)
        work = workroot / job["id"]
        work.mkdir(parents=True, exist_ok=True)
        assets = render.resolve_assets(job, prof, work)
        store.transition(job, "ASSETS_READY", f"장면 자료 {len(assets)}개 확보",
                         assets=[{"kind": a["kind"], "meta": a["meta"]} for a in assets])
        stage = "render"
        store.transition(job, "RENDERING", "합성 시작")
        files = render.render(job, prof, work, assets)
        stage = "qc"
        store.transition(job, "QUALITY_CHECK", "합성 완료", files=files)
        recent = store.recent_titles(prof.name, 80)
        recent = [t for t in recent if t != job["script"]["title"]]
        checks = qc.check_job(job, recent) + qc.check_video(files["video"], files["ass"])
        summary = qc.summarize(checks)
        job["qc"] = summary
        if not summary["passed"]:
            return store.fail(job, "qc", "content_quality", f"품질 검사 실패: {', '.join(summary['failed'])}")
        store.transition(job, "READY_TO_UPLOAD", "품질 검사 통과", qc=summary)
        store.release(job)
        return job
    except Exception as e:  # noqa: BLE001
        from v2.visuals.resolve import ContentMismatch
        cls = "content_quality" if isinstance(e, ContentMismatch) else plan.classify_error(e)
        logger.exception("제작 단계 실패(%s): %s", stage, e)
        return store.fail(job, stage, cls, f"{type(e).__name__}: {e}")


def stage_produce(store: JobStore, profiles: List[ChannelProfile], workroot: Path, only: Optional[str] = None) -> List[Dict]:
    out = []
    for job in store.runnable(None):
        prof = _profile_for(profiles, job)
        if not prof or (only and prof.name != only):
            continue
        s = job["status"]
        due = s in ("SCRIPT_READY", "ASSETS_READY", "RENDERING", "QUALITY_CHECK") or (s == "RETRY_WAIT" and _stage_of_retry(job) in PRODUCE_STAGES)
        if not due:
            continue
        if not job.get("script"):
            continue
        logger.info("제작: %s (%s)", job["id"], s)
        try:
            out.append(produce_job(store, job, prof, workroot))
        except LeaseError as e:
            logger.info("건너뜀: %s", e)
    return out


# ── publish ──────────────────────────────────────────────

def stage_publish(store: JobStore, profiles: List[ChannelProfile], only: Optional[str] = None) -> List[Dict]:
    out = []
    for job in store.runnable(None):
        prof = _profile_for(profiles, job)
        if not prof or (only and prof.name != only):
            continue
        s = job["status"]
        due = s in ("READY_TO_UPLOAD", "UPLOADING") or (s == "RETRY_WAIT" and _stage_of_retry(job) in PUBLISH_STAGES)
        if not due or not job.get("files", {}).get("video") or not Path(job["files"]["video"]).exists():
            if due:
                # 렌더 산출물이 사라졌다면(다른 러너) 다시 합성하도록 되돌린다.
                store.transition(job, "SCRIPT_READY", "산출물 없음 — 재합성", )
            continue
        res = publish.upload_job(store, job, prof)
        out.append(res)
        _notify_uploaded(res)
    return out


def _notify_uploaded(job: Dict) -> None:
    vid = job["youtube"].get("video_id")
    url = f"https://youtu.be/{vid}" if vid else ""
    if job["status"] == "SCHEDULED":
        notify_discord(f"📅 [{job['channel']}] 예약공개 {job['youtube']['scheduled_at'][5:16]} — {job['script']['title']}\n{url}")
    elif job["status"] == "NEEDS_REVIEW" and vid:
        notify_discord(f"👀 [{job['channel']}] 비공개 업로드 완료 — 승인 대기: {job['script']['title']}\n{url}\n승인: GitHub Actions 'V2 Approve' 에 job id `{job['id']}` 입력")
    elif job["status"] in ("RETRY_WAIT", "FAILED", "NEEDS_REVIEW"):
        notify_discord(f"🔴 [{job['channel']}] 업로드 {job['status']} — {(job.get('error') or {}).get('message','')[:140]}")


# ── measure ──────────────────────────────────────────────

def stage_measure(store: JobStore, profiles: List[ChannelProfile]) -> None:
    for prof in profiles:
        try:
            snaps = metrics.collect(store, prof)
            metrics.channel_daily(prof)
            logger.info("%s: 스냅샷 %d개 기록", prof.name, len(snaps))
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 성과 수집 실패: %s", prof.name, e)
    ins = intelligence.refresh(store, [p.name for p in profiles])
    for prof in profiles:
        res = experiments.evaluate(store, prof.name)
        recs = intelligence.recommendations(ins.get(prof.name, {}))
        logger.info("%s 실험: %s / 제안: %s", prof.name, res["note"], recs[:1])


def status(store: JobStore) -> None:
    rows = sorted(store.list(), key=lambda j: j["created_at"])
    for j in rows[-30:]:
        title = (j.get("script") or {}).get("title") or j["topic"]["title"]
        err = f" ⚠ {j['error']['stage']}/{j['error']['class']}" if j.get("error") and j["status"] in ("RETRY_WAIT", "NEEDS_REVIEW", "FAILED") else ""
        print(f"{j['id']:<28} {j['status']:<16} {j['channel']:<4} {title[:30]}{err}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["plan", "produce", "publish", "assemble", "measure", "all", "status", "approve"])
    ap.add_argument("job_id", nargs="?")
    ap.add_argument("--channel")
    ap.add_argument("--store")
    ap.add_argument("--work", help="렌더 작업 폴더(기본: 임시)")
    a = ap.parse_args(argv)

    setup_logging(log_dir=ROOT / "logs", level="INFO")
    store = JobStore(Path(a.store)) if a.store else JobStore()
    profiles = active_profiles()
    work = Path(a.work) if a.work else Path(tempfile.gettempdir()) / "v2_work"
    work.mkdir(parents=True, exist_ok=True)

    if a.stage == "status":
        status(store)
        return 0
    if a.stage == "approve":
        job = store.get(a.job_id)
        prof = _profile_for(profiles, job)
        j = publish.approve(store, a.job_id, prof)
        notify_discord(f"✅ [{j['channel']}] 승인 → 예약공개 {j['youtube']['scheduled_at'][5:16]} — {j['script']['title']}")
        return 0
    if a.stage in ("plan", "all"):
        stage_plan(store, profiles, a.channel)
    if a.stage in ("produce", "assemble", "all"):
        stage_produce(store, profiles, work, a.channel)
    if a.stage in ("publish", "assemble", "all"):
        stage_publish(store, profiles, a.channel)
    if a.stage in ("measure", "all"):
        stage_measure(store, profiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())

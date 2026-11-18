"""
v2/plan.py
===========
기획 단계: 소재 선정 → 자료 수집 → 훅 후보 → 대본(검증 포함). 각 단계가 끝날 때마다 job을 저장하므로
중간에 죽어도 이어서 진행한다(이미 끝난 단계는 다시 하지 않는다).

NEW → RESEARCHING(자료 확보) → VERIFIED(훅 확정) → SCRIPT_READY(대본·주장 검증 완료)
"""
from __future__ import annotations

import random
import re
from typing import Dict, List, Optional

from utils.logger import get_logger
from v2 import hooks, research, script as script_mod, topics
from v2.channels import ChannelProfile, load_v2_config
from v2.jobstore import JobStore
from v2.llm import LLMError, LLMValidationError

logger = get_logger(__name__)


def classify_error(e: Exception) -> str:
    msg = str(e)
    if isinstance(e, LLMValidationError):
        return "content_quality"
    if isinstance(e, LLMError):
        if re.search(r"429|한도|rate", msg, re.I):
            return "rate_limit"
        return "service_transient"
    if isinstance(e, (ConnectionError, TimeoutError)) or re.search(r"timed out|Max retries|GET failed|HTTP 5", msg):
        return "service_transient"
    return "bug"


def _legacy_titles(channel: str) -> List[str]:
    try:
        from core.performance_analysis import recent_titles
        return recent_titles(channel, 40)
    except Exception:
        return []


def pick_topics(profile: ChannelProfile, store: JobStore, rng: Optional[random.Random] = None, limit: int = 3):
    """점수순 후보를 차례로 조사해서, 자료 근거가 충분한 후보 {candidate, pack}를 하나씩 내보낸다."""
    recent = store.recent_titles(profile.name) + _legacy_titles(profile.name)
    ctx, cands = topics.discover(profile, recent, rng)
    ctx["category_counts"] = topics.category_usage(store.list(profile.name))
    ranked = topics.rank(profile, cands, ctx, top=8, rng=rng)
    cfg = load_v2_config()
    n = 0
    for cand in ranked:
        logger.info("소재 후보 '%s' [%s] 점수 %.3f — %s", cand["title"], cand["source"], cand["score"], cand["explain"])
        try:
            pack = research.build_pack(cand, profile)
        except Exception as e:  # noqa: BLE001
            logger.warning("자료 수집 실패(%s): %s", cand["title"], e)
            continue
        photos_needed = 0 if profile.kind == "world_econ" else cfg["min_free_photos"]
        if not pack["ok"] or len(pack["photos"]) < photos_needed:
            logger.info("근거 부족으로 제외: %s (본문 %d자, 사진 %d장)", cand["title"],
                        sum(len(s["text"]) for s in pack["sources"]), len(pack["photos"]))
            continue
        cand["evidence"] = pack["evidence"]
        cand["rescored"] = topics.score(cand, ctx)
        yield {"candidate": cand, "pack": pack}
        n += 1
        if n >= limit:
            return


def advance(store: JobStore, job: Dict, profile: ChannelProfile, hints: Optional[Dict] = None) -> Dict:
    """job을 SCRIPT_READY까지 진행시킨다. 실패하면 장애 분류에 따라 RETRY_WAIT/NEEDS_REVIEW로 보낸다."""
    stage = "plan"
    try:
        store.claim(job)
        if job["status"] in ("NEW", "RETRY_WAIT") and not job.get("pack"):
            stage = "research"
            pack = research.build_pack(job["topic"], profile)
            if not pack["ok"]:
                return store.fail(job, stage, "content_quality", "근거 자료 부족")
            store.transition(job, "RESEARCHING", "자료 수집 완료", pack=pack)
        pack = job["pack"]
        if not job.get("hook"):
            stage = "hook"
            recent_hooks = [(j.get("hook") or {}).get("chosen", {}).get("first_line", "") for j in store.list(profile.name)]
            recent_types = [(j.get("hook") or {}).get("chosen", {}).get("type", "") for j in sorted(store.list(profile.name), key=lambda j: j["created_at"], reverse=True)]
            h = hooks.generate(job["topic"], pack, profile, [x for x in recent_hooks if x], [x for x in recent_types if x],
                               prefer_type=(hints or {}).get("hook_pref"))
            if h["chosen"]["eval"]["components"]["grounded"] == 0.0:
                logger.warning("선택된 훅이 근거 확인 안 됨 — 그래도 다음 후보 중 근거 있는 것 우선")
                grounded = [c for c in h["candidates"] if c["eval"]["components"]["grounded"] == 1.0]
                if grounded:
                    h["chosen"] = grounded[0]
            store.transition(job, "VERIFIED", "훅 확정", hook=h)
        if not job.get("script"):
            stage = "script"
            sc = script_mod.write(job["topic"], pack, job["hook"]["chosen"], profile, hints)
            job["topic"]["category"] = sc["category"]
            unverified = [c for c in sc["claims_verified"] if not c["verified"]]
            if unverified:
                return store.fail(job, stage, "content_quality", f"검증 실패 주장 {len(unverified)}개")
            store.transition(job, "SCRIPT_READY", "대본·주장 검증 완료", script=sc, claims=sc["claims_verified"])
        store.release(job)
        return job
    except Exception as e:  # noqa: BLE001
        cls = classify_error(e)
        logger.exception("기획 단계 실패(%s): %s", stage, e)
        return store.fail(job, stage, cls, f"{type(e).__name__}: {e}")


def plan_one(profile: ChannelProfile, store: JobStore, hints: Optional[Dict] = None, rng: Optional[random.Random] = None) -> Optional[Dict]:
    """소재 하나를 SCRIPT_READY까지 만든다. 한 소재가 근거·형식 검증을 못 넘으면(NEEDS_REVIEW) 다음 후보로 넘어간다.
    서비스 장애(RETRY_WAIT)면 같은 job을 다음 실행에서 이어서 하도록 두고 멈춘다."""
    last = None
    for picked in pick_topics(profile, store, rng):
        job = store.create(profile.name, picked["candidate"], extra={"experiment": (hints or {}).get("experiment")})
        store.transition(job, "RESEARCHING", "소재 선정·자료 수집", pack=picked["pack"])
        last = advance(store, job, profile, hints)
        if last["status"] == "SCRIPT_READY":
            return last
        if last["status"] == "RETRY_WAIT":
            return last
        logger.warning("%s: '%s' 소재는 검증을 통과하지 못해(%s) 다음 후보로 넘어갑니다", profile.name, picked["candidate"]["title"], last["status"])
    if last is None:
        logger.warning("%s: 오늘 쓸 만한 소재를 못 찾음", profile.name)
    return last

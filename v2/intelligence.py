"""
v2/intelligence.py
===================
성과 분석 → 다음 제작에 반영할 근거 생성.

원칙
- 같은 채널 영상끼리, 같은 경과 시간(기본 72시간) 스냅샷으로만 비교한다.
- 표본 수를 항상 같이 낸다: n<4 "표본 부족", 4~7 "참고", ≥8 "비교적 충분". 상관관계를 인과로 말하지 않는다.
- 영상 하나가 우연히 크게 터진 경우를 구분하려고 평균이 아니라 중앙값을 쓴다.
- 결과는 data/insights.json 에 저장하고, topics.py 의 category_prior 가 읽어서 소재 선택에 약하게 반영한다.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from v2 import DATA
from v2.jobstore import JobStore
from v2.metrics import load_snapshots

BASE_AGE_H = 72


def confidence(n: int) -> str:
    return "표본 부족" if n < 4 else ("참고 수준" if n < 8 else "비교적 충분")


def _snap_at(snaps: List[Dict], age_h: int) -> Optional[Dict]:
    s = [x for x in snaps if x.get("age_h") == age_h]
    return s[-1] if s else None


def length_bucket(seconds: float) -> str:
    return "≤25초" if seconds <= 25 else ("26~40초" if seconds <= 40 else ">40초")


def video_rows(store: JobStore, channel: str) -> List[Dict]:
    rows = []
    for job in store.list(channel, states=["SCHEDULED", "PUBLISHED"]):
        vid = job["youtube"].get("video_id")
        if not vid:
            continue
        snaps = load_snapshots(vid)
        base = _snap_at(snaps, BASE_AGE_H)
        if not base:
            continue
        sc = job.get("script") or {}
        ev = sc.get("eval") or {}
        mix = ev.get("visual_mix") or {}
        n_sc = max(sum(mix.values()), 1)
        an = base.get("analytics") or {}
        pub = job["youtube"].get("scheduled_at", "")
        rows.append({
            "job_id": job["id"], "video_id": vid, "title": sc.get("title"), "category": (job.get("topic") or {}).get("category"),
            "hook_type": ((job.get("hook") or {}).get("chosen") or {}).get("type"),
            "length": length_bucket(ev.get("est_seconds") or 0), "est_seconds": ev.get("est_seconds"),
            "publish_hour": int(pub[11:13]) if len(pub) >= 13 else None,
            "chart_share": round((mix.get("chart", 0) + mix.get("compare", 0) + mix.get("stat", 0)) / n_sc, 2),
            "topic_source": (job.get("topic") or {}).get("source"),
            "experiment": job.get("experiment"),
            "views": base["views"], "engaged": an.get("engagedViews"), "avg_pct": an.get("averageViewPercentage"),
            "avg_dur": an.get("averageViewDuration"), "likes": base["likes"],
        })
    return rows


def _group(rows: List[Dict], key: str) -> Dict[str, Dict]:
    med = statistics.median([r["views"] for r in rows]) if rows else 0
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            groups[str(r[key])].append(r)
    out = {}
    for g, rs in groups.items():
        views = [r["views"] for r in rs]
        pct = [r["avg_pct"] for r in rs if r.get("avg_pct") is not None]
        out[g] = {"n": len(rs), "median_views": statistics.median(views),
                  "relative": round(statistics.median(views) / med, 2) if med else 0.5,
                  "median_avg_view_pct": round(statistics.median(pct), 1) if pct else None,
                  "confidence": confidence(len(rs))}
    return out


def analyze(store: JobStore, channel: str) -> Dict:
    rows = video_rows(store, channel)
    n = len(rows)
    result: Dict = {"n_videos": n, "base_age_h": BASE_AGE_H, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "confidence": confidence(n)}
    if n == 0:
        result["note"] = f"{BASE_AGE_H}시간 경과 스냅샷이 있는 영상이 아직 없음"
        return result
    result["median_views"] = statistics.median([r["views"] for r in rows])
    for key, name in (("category", "categories"), ("hook_type", "hook_types"), ("length", "lengths"),
                      ("publish_hour", "publish_hours"), ("topic_source", "topic_sources")):
        result[name] = _group(rows, key)
    result["chart_share_corr"] = _chart_vs_views(rows)
    med = result["median_views"]
    winners = [r for r in rows if med and r["views"] >= 2 * med and n >= 5]
    result["winners"] = [{"job_id": r["job_id"], "title": r["title"], "category": r["category"], "views": r["views"]} for r in winners[:3]]
    return result


def _chart_vs_views(rows: List[Dict]) -> Optional[Dict]:
    if len(rows) < 6:
        return None
    hi = [r["views"] for r in rows if r["chart_share"] >= 0.35]
    lo = [r["views"] for r in rows if r["chart_share"] < 0.35]
    if len(hi) < 3 or len(lo) < 3:
        return None
    return {"high_chart_share_median": statistics.median(hi), "low_chart_share_median": statistics.median(lo), "n_high": len(hi), "n_low": len(lo)}


def refresh(store: JobStore, channels: List[str]) -> Dict:
    path = DATA / "insights.json"
    insights = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for ch in channels:
        insights[ch] = analyze(store, ch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(insights, ensure_ascii=False, indent=1), encoding="utf-8")
    return insights


def recommendations(ch_insight: Dict) -> List[str]:
    """데이터로 뒷받침되는 제안만. 표본이 적으면 그 사실을 앞에 쓰고 '실험 권장' 수준으로 말한다."""
    out: List[str] = []
    n = ch_insight.get("n_videos", 0)
    if n == 0:
        return ["아직 72시간이 지난 영상이 없어 성과 기반 제안을 할 수 없습니다. 탐색 위주로 소재를 고르고 있습니다."]
    cats = ch_insight.get("categories", {})
    ranked = sorted(cats.items(), key=lambda kv: kv[1]["relative"], reverse=True)
    if len(ranked) >= 2:
        top, bot = ranked[0], ranked[-1]
        out.append(f"{top[0]} 소재가 채널 중앙값 대비 {top[1]['relative']}배(표본 {top[1]['n']}편, {top[1]['confidence']})로 상대적으로 높았습니다. "
                   f"{bot[0]}은 {bot[1]['relative']}배(표본 {bot[1]['n']}편)입니다. 상관관계일 수 있어 {top[0]} 소재를 추가로 실험하는 것을 권장합니다.")
    ln = ch_insight.get("lengths", {})
    if len(ln) >= 2:
        best = max(ln.items(), key=lambda kv: kv[1]["relative"])
        out.append(f"길이별로는 {best[0]} 영상이 상대 {best[1]['relative']}배(표본 {best[1]['n']}편, {best[1]['confidence']})였습니다. 단정하기에는 표본이 적습니다.")
    cs = ch_insight.get("chart_share_corr")
    if cs:
        out.append(f"차트·숫자 장면 비중이 높은 영상의 조회수 중앙값 {cs['high_chart_share_median']:.0f} vs 낮은 영상 {cs['low_chart_share_median']:.0f} "
                   f"(각 {cs['n_high']}편/{cs['n_low']}편).")
    return out or ["비교할 그룹이 아직 충분하지 않습니다."]

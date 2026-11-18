"""
v2/experiments.py
==================
안전한 콘텐츠 실험. 한 채널에서 한 번에 변수 하나만 바꾸고, 각 팔(arm)에 영상이 고르게 배정되도록 한다.
같은 영상을 제목만 바꿔 다시 올리지 않는다 — 서로 다른 소재의 영상 사이에서 변수만 바꾼다.
결과는 표본 수와 함께 기록하고, 팔당 min_n편 미만이면 결론내지 않는다(유튜브가 공식 A/B 기능을 주지 않으므로
소재 차이가 섞여 있다는 한계를 결과에 명시한다).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional

from v2 import DATA
from v2.intelligence import video_rows

PATH = DATA / "experiments.json"
MIN_N = 6

# 순서대로 하나씩 돌린다. variable은 script.write 의 hints 로 전달된다.
CATALOG = [
    {"variable": "length_bucket", "arms": ["short", "medium"], "desc": "대본 길이(짧게 vs 중간)"},
    {"variable": "hook_pref", "arms": ["result_first", "surprising_fact"], "desc": "훅 유형 선호"},
    {"variable": "visual_style", "arms": ["photo_heavy", "graphic_heavy"], "desc": "실제 사진 중심 vs 그래픽·카드 중심"},
]


def _load() -> Dict:
    if PATH.exists():
        return json.loads(PATH.read_text(encoding="utf-8"))
    return {"active": {}, "assignments": {}, "history": []}


def _save(d: Dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def current(channel: str) -> Dict:
    d = _load()
    if channel not in d["active"]:
        idx = sum(1 for h in d["history"] if h["channel"] == channel) % len(CATALOG)
        spec = CATALOG[idx]
        d["active"][channel] = {"id": f"{channel}-{spec['variable']}-{datetime.now(timezone.utc):%Y%m%d}", "variable": spec["variable"],
                                "arms": spec["arms"], "desc": spec["desc"], "min_n": MIN_N,
                                "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        _save(d)
    return d["active"][channel]


def assign(channel: str, job_id: str) -> Dict:
    """가장 적게 배정된 팔을 고른다(균형 배정). 반환값은 script hints 에 합쳐 쓴다."""
    d = _load()
    exp = current(channel)
    d = _load()
    counts = {a: 0 for a in exp["arms"]}
    for jid, a in d["assignments"].items():
        if a["exp_id"] == exp["id"]:
            counts[a["arm"]] = counts.get(a["arm"], 0) + 1
    arm = min(exp["arms"], key=lambda a: (counts[a], exp["arms"].index(a)))
    d["assignments"][job_id] = {"exp_id": exp["id"], "arm": arm}
    _save(d)
    hints = {"experiment": {"id": exp["id"], "variable": exp["variable"], "arm": arm}}
    if exp["variable"] == "length_bucket":
        hints["length_bucket"] = arm
    elif exp["variable"] == "hook_pref":
        hints["hook_pref"] = arm
    elif exp["variable"] == "visual_style":
        hints["visual_style"] = arm
    return hints


def evaluate(store, channel: str) -> Dict:
    """현재 실험의 팔별 성과(72시간 조회수 중앙값). 팔당 MIN_N 미만이면 결론 보류."""
    exp = current(channel)
    d = _load()
    arm_of = {jid: a["arm"] for jid, a in d["assignments"].items() if a["exp_id"] == exp["id"]}
    rows = [r for r in video_rows(store, channel) if r["job_id"] in arm_of]
    by_arm: Dict[str, List[int]] = {a: [] for a in exp["arms"]}
    for r in rows:
        by_arm[arm_of[r["job_id"]]].append(r["views"])
    summary = {a: {"n": len(v), "median_views": statistics.median(v) if v else None} for a, v in by_arm.items()}
    enough = all(s["n"] >= exp["min_n"] for s in summary.values())
    res = {"experiment": exp, "arms": summary, "concluded": enough,
           "note": ("팔당 표본이 충분해 비교 가능(그래도 소재 차이가 섞여 있어 인과 단정은 금지)" if enough
                    else f"팔당 {exp['min_n']}편 미만 — 결론 보류")}
    if enough:
        d["history"].append({"channel": channel, "id": exp["id"], "result": summary, "at": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
        d["active"].pop(channel, None)
        _save(d)
    return res

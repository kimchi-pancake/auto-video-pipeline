"""
core/performance_analysis.py
==============================
video_registry.json 통계를 소재 카테고리별로 집계합니다. "!분석" 성격의
데이터 + 주제 자동 생성 프롬프트에 참고자료로 넣을 요약을 만듭니다.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from core.video_registry import all_entries

# scripts/weekly_analysis.py가 YouTube Analytics API(시청 지속률)로 매주 갱신.
# 이 파일이 있으면 summarize_for_prompt()가 조회수 대신 이걸 우선 씁니다.
_CATEGORY_PERF_PATH = Path(__file__).parent.parent / "config" / "category_performance.json"


def _nfc(s: str) -> str:
    """채널명 비교용 정규화 — 서로 다른 곳(디스코드 Worker, GitHub Actions
    입력, config.json)에서 온 같은 한글 문자열이라도 유니코드 정규화 형태가
    다르면 == 비교가 조용히 실패할 수 있습니다."""
    return unicodedata.normalize("NFC", s)


def category_retention_stats(channel: str, video_metrics: dict[str, dict]) -> dict[str, list[dict]]:
    """video_registry의 카테고리 매핑과 YouTube Analytics 지표(video_metrics:
    {video_id: {"views","avg_view_duration","avg_view_percentage"}})를
    조인해서 {"long": [...], "shorts": [...]} 형태로, 각각 카테고리별
    {category, count, avg_views, avg_retention}을 시청 지속률(%) 내림차순
    으로 반환합니다.

    롱폼/쇼츠를 반드시 분리합니다 — 쇼츠는 자동 반복재생 때문에
    averageViewPercentage가 100%를 넘는 경우가 흔한 반면(실측: 이 채널
    쇼츠의 절반 가까이가 100%+, 롱폼은 전부 100% 미만) 롱폼은 구조적으로
    100%를 못 넘습니다 — 척도 자체가 다른 둘을 섞어서 평균 내면(쇼츠
    165% + 롱폼 5% → 85%처럼) 롱폼 성과가 나쁜 카테고리가 "지속률 좋은
    카테고리"로 왜곡됩니다. 그 기간에 조회수가 없던 영상(=video_metrics에
    없는 video_id)은 제외됩니다."""
    entries = all_entries()
    channel = _nfc(channel)
    by_kind_cat: dict[str, dict[str, list[dict]]] = {"long": {}, "shorts": {}}
    for e in entries:
        if _nfc(e.get("channel", "")) != channel:
            continue
        m = video_metrics.get(e["video_id"])
        if not m:
            continue
        kind = "shorts" if e.get("is_shorts") else "long"
        cat = e.get("category") or "미분류"
        by_kind_cat[kind].setdefault(cat, []).append(m)

    result: dict[str, list[dict]] = {}
    for kind, by_cat in by_kind_cat.items():
        rows = []
        for cat, ms in by_cat.items():
            n = len(ms)
            rows.append({
                "category": cat,
                "count": n,
                "avg_views": sum(m["views"] for m in ms) / n,
                "avg_retention": sum(m["avg_view_percentage"] for m in ms) / n,
            })
        rows.sort(key=lambda r: r["avg_retention"], reverse=True)
        result[kind] = rows
    return result


def _load_retention_ranking(channel: str) -> dict[str, list[dict]] | None:
    if not _CATEGORY_PERF_PATH.exists():
        return None
    try:
        data = json.loads(_CATEGORY_PERF_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get(channel) or data.get(_nfc(channel))
    if not entry:
        return None
    long_rows = entry.get("long") or []
    shorts_rows = entry.get("shorts") or []
    if not long_rows and not shorts_rows:
        return None
    return {"long": long_rows, "shorts": shorts_rows}


def category_stats(channel: str | None = None) -> list[dict]:
    """카테고리별 {category, count, avg_views, avg_likes, avg_comments}를
    평균 조회수 내림차순으로 반환합니다. 통계가 아직 없는 영상은 제외합니다."""
    entries = all_entries()
    channel = _nfc(channel) if channel else channel
    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        if channel and _nfc(e.get("channel", "")) != channel:
            continue
        stats = e.get("stats")
        if not stats:
            continue
        cat = e.get("category") or "미분류"
        by_cat.setdefault(cat, []).append(stats)

    result = []
    for cat, stats_list in by_cat.items():
        n = len(stats_list)
        result.append({
            "category": cat,
            "count": n,
            "avg_views": sum(s.get("views", 0) for s in stats_list) / n,
            "avg_likes": sum(s.get("likes", 0) for s in stats_list) / n,
            "avg_comments": sum(s.get("comments", 0) for s in stats_list) / n,
        })
    result.sort(key=lambda r: r["avg_views"], reverse=True)
    return result


def summarize_for_prompt(channel: str | None = None, top_n: int = 3) -> str:
    """주제 생성 프롬프트에 참고자료로 붙일 짧은 텍스트. 데이터 없으면 빈 문자열.

    scripts/weekly_analysis.py가 만든 시청 지속률 기반 순위(config/
    category_performance.json)가 있으면 그걸 우선 씁니다 — "끝까지
    보게 되는 소재"가 "일단 클릭은 되는 소재"(조회수)보다 다음 소재
    고르는 기준으로 더 직접적입니다. 아직 주간 분석이 안 돌았으면
    기존처럼 조회수 기준으로 폴백합니다."""
    retention_ranking = _load_retention_ranking(channel) if channel else None
    if retention_ranking:
        lines = ["[참고: 최근 소재별 시청 지속률 — 끝까지 보게 되는 소재를 우선 고려해라]"]
        # 쇼츠는 반복재생 때문에 지속률이 100%를 넘을 수 있어 롱폼과 척도가
        # 다르므로(core.performance_analysis.category_retention_stats 참고)
        # 절대 합쳐서 보여주지 않고 항상 구분해서 보여줍니다.
        for kind, label in (("long", "롱폼"), ("shorts", "쇼츠")):
            rows = retention_ranking.get(kind) or []
            if not rows:
                continue
            good = rows[:top_n]
            bad = rows[-top_n:] if len(rows) > top_n else []
            lines.append(
                f"{label} 지속률 높은 소재: "
                + ", ".join(
                    f"{s['category']}(평균 시청률 {s['avg_retention']:.0f}%, 조회수 {int(s['avg_views'])})"
                    for s in good
                )
            )
            if bad and bad != good:
                lines.append(
                    f"{label} 지속률 낮은 소재(초반 이탈 많음): "
                    + ", ".join(f"{s['category']}(평균 시청률 {s['avg_retention']:.0f}%)" for s in bad)
                )
        if len(lines) > 1:
            return "\n".join(lines)

    stats = category_stats(channel)
    if not stats:
        return ""
    good = stats[:top_n]
    bad = stats[-top_n:] if len(stats) > top_n else []
    lines = ["[참고: 최근 소재별 성과 — 잘 되는 소재를 우선 고려해라]"]
    lines.append(
        "성과 좋은 소재: "
        + ", ".join(f"{s['category']}(평균 조회수 {int(s['avg_views'])})" for s in good)
    )
    if bad and bad != good:
        lines.append(
            "성과 낮은 소재: "
            + ", ".join(f"{s['category']}(평균 조회수 {int(s['avg_views'])})" for s in bad)
        )
    return "\n".join(lines)


def recent_titles(channel: str | None = None, limit: int = 30) -> list[str]:
    entries = all_entries()
    if channel:
        channel = _nfc(channel)
        entries = [e for e in entries if _nfc(e.get("channel", "")) == channel]
    return [e["title"] for e in entries[-limit:] if e.get("title")]

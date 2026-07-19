"""
core/performance_analysis.py
==============================
video_registry.json 통계를 소재 카테고리별로 집계합니다. "!분석" 성격의
데이터 + 주제 자동 생성 프롬프트에 참고자료로 넣을 요약을 만듭니다.
"""

from __future__ import annotations

from core.video_registry import all_entries


def category_stats(channel: str | None = None) -> list[dict]:
    """카테고리별 {category, count, avg_views, avg_likes, avg_comments}를
    평균 조회수 내림차순으로 반환합니다. 통계가 아직 없는 영상은 제외합니다."""
    entries = all_entries()
    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        if channel and e.get("channel") != channel:
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
    """주제 생성 프롬프트에 참고자료로 붙일 짧은 텍스트. 데이터 없으면 빈 문자열."""
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
        entries = [e for e in entries if e.get("channel") == channel]
    return [e["title"] for e in entries[-limit:] if e.get("title")]

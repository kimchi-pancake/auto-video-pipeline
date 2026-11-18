"""
v2/sources/trends.py
=====================
공개적으로 합법 이용 가능한 관심도 신호.
  1) 위키미디어 Pageviews API — 한국어 위키백과 일간 조회수 상위 문서(공식 API, 키 불필요)
  2) Google Trends 공개 RSS (대한민국 일간 인기 검색어) — 실패하면 조용히 건너뛴다
수집 실패는 "수집 못 함"으로 기록하고, 확보한 척 하지 않는다.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from v2.sources.http import get, get_json

_JUNK = re.compile(r"^(특수|분류|위키백과|파일|틀|포털|도움말|사용자|대문|Special|Main_Page|Wikipedia)")


def wikipedia_top(lang: str = "ko", days_back: int = 2, limit: int = 60) -> List[Dict]:
    day = datetime.now(timezone.utc) - timedelta(days=days_back)
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{lang}.wikipedia/all-access/{day:%Y/%m/%d}"
    try:
        items = get_json(url)["items"][0]["articles"]
    except Exception:
        return []
    out = []
    for it in items:
        a = it["article"].replace("_", " ")
        if _JUNK.match(a) or ":" in a:
            continue
        out.append({"title": a, "views": it["views"], "rank": it["rank"], "source": "wikipedia_top", "date": f"{day:%Y-%m-%d}"})
        if len(out) >= limit:
            break
    return out


def google_trends_kr(limit: int = 25) -> List[Dict]:
    try:
        r = get("https://trends.google.com/trending/rss", {"geo": "KR"}, retries=2)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    for item in root.iter("item"):
        t = (item.findtext("title") or "").strip()
        traffic = ""
        for child in item:
            if child.tag.endswith("approx_traffic"):
                traffic = (child.text or "").strip()
        if t:
            out.append({"title": t, "traffic": traffic, "source": "google_trends_kr"})
        if len(out) >= limit:
            break
    return out

"""
v2/sources/wiki.py
===================
위키백과 본문 + 위키미디어 커먼즈 이미지(라이선스 확인된 것만).

반환하는 모든 사실 자료에는 출처 URL·제목·확인일을 붙인다(사실 검증 엔진이 그대로 저장).
이미지는 커먼즈 extmetadata의 라이선스를 허용 목록과 대조하고, 아니면 버린다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from v2.sources.http import get_json

_SKIP_FILE = re.compile(
    r"(logo|icon|flag of|coat of arms|symbol|commons-|wiki|button|edit-|question|ambox|pictogram|disambig|"
    r"stub|portal|increase|decrease|steady|\.svg$|\.gif$|\.ogg$|\.ogv$|\.webm$|\.pdf$|\.tif)",
    re.IGNORECASE,
)
_OK_EXT = (".jpg", ".jpeg", ".png", ".webp")

# 허용: 퍼블릭 도메인, CC0, CC BY, CC BY-SA, 공공누리 1유형(KOGL Type 1)
_LICENSE_OK = re.compile(
    r"^(public domain|pd[-\s]|cc0|cc[-\s]?by(-sa)?[\s-]?[0-9.]*|attribution|kogl.*(type\s*)?1|공공누리)", re.IGNORECASE
)
_LICENSE_BAD = re.compile(r"(\bnc\b|-nc|\bnd\b|-nd|fair use|non-?free|all rights reserved|copyrighted)", re.IGNORECASE)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def license_ok(short_name: str) -> bool:
    s = (short_name or "").strip()
    if not s or _LICENSE_BAD.search(s):
        return False
    return bool(_LICENSE_OK.search(s))


def _api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def fetch_article(title: str, lang: str = "ko") -> Optional[Dict]:
    """본문(plain text) 전체를 가져온다. 없는 제목이면 검색으로 한 번 더 시도한다."""
    for attempt_title in (title, None):
        t = attempt_title
        if t is None:
            hit = search_title(title, lang)
            if not hit:
                return None
            t = hit
        data = get_json(_api(lang), {
            "action": "query", "prop": "extracts|info", "explaintext": 1, "exsectionformat": "plain",
            "redirects": 1, "inprop": "url", "titles": t, "format": "json", "formatversion": 2,
        })
        pages = data.get("query", {}).get("pages", [])
        if pages and not pages[0].get("missing") and pages[0].get("extract"):
            p = pages[0]
            return {
                "title": p["title"], "lang": lang, "url": p.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{p['title']}",
                "text": p["extract"], "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "published": (p.get("touched") or "")[:10],
            }
    return None


def langlink(title: str, lang_from: str, lang_to: str) -> Optional[str]:
    """다른 언어판 문서 제목(예: 누리호 → Nuri (rocket)). 없으면 None."""
    data = get_json(_api(lang_from), {"action": "query", "prop": "langlinks", "lllang": lang_to, "lllimit": 1,
                                       "titles": title, "redirects": 1, "format": "json", "formatversion": 2})
    pages = data.get("query", {}).get("pages", [])
    ll = pages[0].get("langlinks") if pages else None
    return ll[0]["title"] if ll else None


def search_title(query: str, lang: str = "ko") -> Optional[str]:
    data = get_json(_api(lang), {"action": "query", "list": "search", "srsearch": query, "srlimit": 1, "format": "json", "formatversion": 2})
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def _article_file_titles(title: str, lang: str) -> List[str]:
    data = get_json(_api(lang), {"action": "query", "prop": "images", "imlimit": "max", "titles": title, "redirects": 1, "format": "json", "formatversion": 2})
    pages = data.get("query", {}).get("pages", [])
    return [i["title"] for p in pages for i in p.get("images", [])] if pages else []


def _imageinfo(file_titles: List[str], width: int = 1600) -> List[Dict]:
    out: List[Dict] = []
    for i in range(0, len(file_titles), 40):
        batch = file_titles[i:i + 40]
        data = get_json("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "prop": "imageinfo", "titles": "|".join(batch), "format": "json", "formatversion": 2,
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": width,
            "iiextmetadatafilter": "LicenseShortName|Artist|ImageDescription|Credit|Restrictions|ObjectName|DateTimeOriginal",
        })
        for p in data.get("query", {}).get("pages", []):
            info = (p.get("imageinfo") or [None])[0]
            if not info or p.get("missing"):
                continue
            out.append({"file": p["title"], "info": info})
    return out


def _to_asset(entry: Dict) -> Optional[Dict]:
    f, info = entry["file"], entry["info"]
    if _SKIP_FILE.search(f) or not f.lower().endswith(_OK_EXT):
        return None
    if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
        return None
    w, h = info.get("width", 0), info.get("height", 0)
    if w < 900 or h < 600:
        return None
    meta = info.get("extmetadata", {})
    lic = (meta.get("LicenseShortName", {}) or {}).get("value", "")
    if not license_ok(lic):
        return None
    if "trademark" in (meta.get("Restrictions", {}) or {}).get("value", "").lower():
        return None
    artist = _strip_html((meta.get("Artist", {}) or {}).get("value", "")) or _strip_html((meta.get("Credit", {}) or {}).get("value", ""))
    desc = _strip_html((meta.get("ImageDescription", {}) or {}).get("value", "")) or _strip_html((meta.get("ObjectName", {}) or {}).get("value", ""))
    return {
        "file": f,
        "url": info.get("thumburl") or info.get("url"),
        "orig_url": info.get("url"),
        "page": info.get("descriptionurl"),
        "width": w, "height": h,
        "license": lic,
        "artist": artist[:120],
        "caption": (desc or f.replace("File:", "").rsplit(".", 1)[0])[:160],
    }


def article_images(title: str, lang: str = "ko", extra_lang: Optional[str] = "en", limit: int = 24) -> List[Dict]:
    """문서에 실린 이미지 중 라이선스가 허용되는 것. ko 문서에 사진이 적으면 en 문서에서 보충."""
    titles: List[str] = []
    for lg in [lang] + ([extra_lang] if extra_lang and extra_lang != lang else []):
        try:
            t = title if lg == lang else (search_title(title, lg) or title)
            titles += [x for x in _article_file_titles(t, lg) if x not in titles]
        except Exception:
            continue
        if len(titles) >= 12 and lg == lang:
            break
    titles = [t for t in titles if not _SKIP_FILE.search(t)][:60]
    assets = []
    seen = set()
    for e in _imageinfo(titles):
        a = _to_asset(e)
        if a and a["file"] not in seen:
            seen.add(a["file"])
            assets.append(a)
    return assets[:limit]


def commons_search_images(query: str, limit: int = 10) -> List[Dict]:
    """커먼즈 직접 검색(문서에 사진이 부족할 때 보충). 같은 라이선스 필터를 거친다."""
    data = get_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "list": "search", "srsearch": f"{query} filetype:bitmap", "srnamespace": 6, "srlimit": 20,
        "format": "json", "formatversion": 2,
    })
    titles = [h["title"] for h in data.get("query", {}).get("search", [])]
    out = []
    for e in _imageinfo(titles):
        a = _to_asset(e)
        if a:
            out.append(a)
    return out[:limit]


def credit_line(asset: Dict) -> str:
    who = asset.get("artist") or "작성자 미상"
    return f"사진: {who} / {asset.get('license','')} / {asset.get('page','')}"

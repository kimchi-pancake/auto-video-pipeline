"""
v2/research.py
===============
Research & Fact Verification Engine — 자료 수집 + 주장 단위 검증.

SourcePack: 대본이 쓸 수 있는 "근거 전부". 대본은 이 안의 내용만 사실로 쓸 수 있다.
  sources: [{id:S1, title, url, published, fetched_at, text}]
  photos : [{id:P1, caption, license, artist, url, page, width, height}]
검증: 대본의 각 주장은 {source_id, quote}를 달고, quote가 해당 출처 본문에 실제로 있어야 verified.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional

from v2.channels import ChannelProfile, load_v2_config
from v2.sources import wiki

MAX_SOURCE_CHARS = 6500


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\s​]+", "", s)
    return re.sub(r"[\"'“”‘’「」『』()\[\]…·,，.。]", "", s).lower()


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    i = max(cut.rfind(". "), cut.rfind("다.\n"), cut.rfind("\n"))
    return cut[: i + 1] if i > limit * 0.6 else cut


def build_pack(topic: Dict, profile: ChannelProfile) -> Dict:
    cfg = load_v2_config()
    title = topic.get("wiki_title") or topic["title"]
    sources: List[Dict] = []
    ko = wiki.fetch_article(title, "ko")
    if ko:
        sources.append({"title": f"위키백과(한국어) — {ko['title']}", "url": ko["url"], "published": ko["published"],
                        "fetched_at": ko["fetched_at"], "text": _trim(ko["text"], MAX_SOURCE_CHARS), "lang": "ko", "wiki_title": ko["title"]})
        try:
            en_title = wiki.langlink(ko["title"], "ko", "en")
        except Exception:
            en_title = None
        if en_title:
            en = wiki.fetch_article(en_title, "en")
            if en:
                sources.append({"title": f"Wikipedia(English) — {en['title']}", "url": en["url"], "published": en["published"],
                                "fetched_at": en["fetched_at"], "text": _trim(en["text"], MAX_SOURCE_CHARS), "lang": "en", "wiki_title": en["title"]})
    for i, s in enumerate(sources, 1):
        s["id"] = f"S{i}"

    photos: List[Dict] = []
    if ko:
        try:
            photos = wiki.article_images(ko["title"], "ko")
        except Exception:
            photos = []
        if len(photos) < cfg["min_free_photos"]:
            try:
                extra = wiki.commons_search_images(ko["title"])
                have = {p["file"] for p in photos}
                photos += [p for p in extra if p["file"] not in have]
            except Exception:
                pass
    for i, p in enumerate(photos, 1):
        p["id"] = f"P{i}"

    total_chars = sum(len(s["text"]) for s in sources)
    # 근거 점수: 본문 분량 + 사용 가능한 사진 수 (0~1)
    evidence = min(1.0, total_chars / 7000) * 0.6 + min(1.0, len(photos) / 6) * 0.4
    if profile.kind == "world_econ":
        evidence = min(1.0, total_chars / 7000) * 0.8 + 0.2  # 차트/카드로 시각화 가능
    return {"topic": topic, "sources": sources, "photos": photos, "evidence": round(evidence, 3),
            "ok": bool(sources) and sum(len(s["text"]) for s in sources if s["lang"] == "ko") >= 0 and total_chars >= cfg["min_article_chars"],
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def pack_text_for_prompt(pack: Dict) -> str:
    parts = []
    for s in pack["sources"]:
        parts.append(f"=== [{s['id']}] {s['title']} (확인일 {s['fetched_at']}) ===\n{s['text']}")
    return "\n\n".join(parts)


def numbers_for_prompt(pack: Dict, limit: int = 40) -> str:
    """근거 자료 안의 두 자리 이상 숫자와 그 주변 문맥 — 모델이 이 중에서만 숫자를 고르게 한다."""
    seen, lines = set(), []
    for s in pack["sources"]:
        for m in _NUM.finditer(s["text"]):
            n = m.group(0).replace(",", "")
            if re.fullmatch(r"\d", n) or n in seen:
                continue
            seen.add(n)
            a, b = max(m.start() - 14, 0), min(m.end() + 14, len(s["text"]))
            ctx = s["text"][a:b].replace("\n", " ")
            lines.append(f"- {m.group(0)}  ← \"…{ctx}…\" [{s['id']}]")
            if len(lines) >= limit:
                return "\n".join(lines)
    return "\n".join(lines) or "(자료에 쓸 만한 숫자 없음 — 숫자를 쓰지 마라)"


def photos_for_prompt(pack: Dict) -> str:
    if not pack["photos"]:
        return "(사용 가능한 사진 없음 — photo 장면을 쓰지 마라)"
    return "\n".join(f"- {p['id']}: {p['caption']} [{p['license']}]" for p in pack["photos"])


# ── 검증 ────────────────────────────────────────────────

def quote_supported(quote: str, source_text: str, min_cover: float = 0.85) -> bool:
    """quote가 본문에 (공백·구두점 무시) 그대로 있거나, 긴 인용은 8자 조각의 85% 이상이 본문에 있을 때 인정."""
    q, t = norm(quote), norm(source_text)
    if len(q) < 6:
        return False
    if q in t:
        return True
    if len(q) < 24:
        return False
    chunks = [q[i:i + 8] for i in range(0, len(q) - 7, 4)]
    hit = sum(1 for c in chunks if c in t)
    return hit / max(len(chunks), 1) >= min_cover


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.?!])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def find_evidence(claim_text: str, pack: Dict, min_cover: float = 0.45):
    """주장 문장과 가장 겹치는 근거 문장을 코드가 직접 찾는다(모델이 인용을 못 맞춰도 근거가 실제로 있으면 인정).
    반환: (source_id, 문장, 겹침 비율) 또는 None. 주장 속 두 자리 이상 숫자는 그 문장에도 있어야 한다."""
    c = re.sub(r"\s+", "", claim_text)
    cb = {c[i:i + 2] for i in range(len(c) - 1)}
    if len(cb) < 4:
        return None
    best = None
    for src in pack["sources"]:
        for sent in _sentences(src["text"]):
            t = re.sub(r"\s+", "", sent)
            tb = {t[i:i + 2] for i in range(len(t) - 1)}
            cover = len(cb & tb) / len(cb)
            if best is None or cover > best[2]:
                best = (src["id"], sent, cover)
    if not best or best[2] < min_cover:
        return None
    sent_nums = set(numbers_in(best[1]))
    for n in numbers_in(claim_text):
        if not re.fullmatch(r"\d", n) and n not in sent_nums:
            return None
    return best


def numbers_in(text: str) -> List[str]:
    out = []
    for m in _NUM.finditer(text or ""):
        n = m.group(0).replace(",", "")
        out.append(n)
    return out


def source_numbers(pack: Dict, extra_texts: Optional[List[str]] = None) -> set:
    nums = set()
    for s in pack["sources"]:
        nums |= set(numbers_in(s["text"]))
    for t in extra_texts or []:
        nums |= set(numbers_in(t))
    return nums


def ungrounded_numbers(text: str, allowed: set) -> List[str]:
    """한 자리 정수는 개수/서수 표현일 가능성이 커서 제외하고, 두 자리 이상·소수만 근거를 요구한다."""
    bad = []
    for n in numbers_in(text):
        if re.fullmatch(r"\d", n):
            continue
        if n in allowed:
            continue
        # 소수 반올림 허용 (3.597 → 3.6)
        try:
            f = float(n)
            if any(abs(float(a) - f) <= max(0.051, abs(f) * 0.002) for a in allowed if re.fullmatch(r"\d+(\.\d+)?", a)):
                continue
        except ValueError:
            pass
        bad.append(n)
    return bad


def verify_claims(script: Dict, pack: Dict) -> List[Dict]:
    """각 주장에 출처 정보를 붙이고 verified/unverified 판정을 기록한다."""
    by_id = {s["id"]: s for s in pack["sources"]}
    out = []
    n = 0
    for si, scene in enumerate(script["scenes"]):
        for c in scene.get("claims", []):
            n += 1
            src = by_id.get(c.get("source"))
            rec = {
                "id": f"C{n}", "scene": si, "text": c.get("text", ""), "kind": c.get("kind", "fact"),
                "source_id": c.get("source"), "source_title": src["title"] if src else None,
                "source_url": src["url"] if src else None, "published": src["published"] if src else None,
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "quote": c.get("quote", ""),
                "verified": False, "note": "",
            }
            if c.get("kind") in ("interpretation", "forecast"):
                rec["verified"] = True
                rec["note"] = "해석/전망 — 사실 주장 아님(본문에서 단정 표현 금지)"
            elif not src:
                rec["note"] = "출처 id 없음"
            elif quote_supported(c.get("quote", ""), src["text"]):
                rec["verified"] = True
                rec["note"] = "출처 본문에서 " + ("코드가 근거 문장을 찾아 확인" if c.get("quote_found_by") == "code" else "인용 확인")
            else:
                rec["note"] = "인용문이 출처 본문에서 확인되지 않음"
            out.append(rec)
    return out

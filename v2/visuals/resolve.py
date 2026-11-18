"""
v2/visuals/resolve.py — 장면의 visual 명세를 실제 미디어(이미지 또는 애니메이션 mp4)로 만든다.

원칙: 장면과 무관한 그림으로 채우지 않는다. 사진은 SourcePack의 라이선스 확인된 것만, 차트는 세계은행 실제 데이터만.
차트 데이터를 못 구하면 무관한 사진 대신 같은 내용을 문구 카드(text)로 보여 준다(meta에 fallback 기록).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from v2.research import numbers_in, source_numbers, ungrounded_numbers
from v2.sources import worldbank
from v2.visuals import cards, photos


class AssetUnavailable(RuntimeError):
    pass


class ContentMismatch(RuntimeError):
    """장면 나레이션의 숫자가 근거(자료·차트 데이터)와 맞지 않을 때 — 품질 실패로 처리한다."""


def _chart_numbers(data: Dict) -> set:
    nums = set()
    for pts in data["series"].values():
        for y, v in pts:
            nums.add(str(y))
            nums.add(f"{v:.1f}")
            nums.add(f"{v:.0f}")
            nums.add(f"{v:.2f}")
            nums.add(f"{v:,.0f}".replace(",", ""))
    return nums


def resolve(idx: int, scene: Dict, pack: Dict, profile, work: Path) -> Dict:
    v = scene["visual"]
    t = v["type"]
    narration = scene["narration"]
    out_dir = work / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: Dict = {"type": t}

    if t == "photo":
        asset = next((p for p in pack["photos"] if p["id"] == v["id"]), None)
        if not asset:
            raise AssetUnavailable(f"사진 {v['id']} 없음")
        raw = photos.download(asset, out_dir / f"{v['id']}.src")
        canvas = photos.compose_canvas(raw, out_dir / f"scene{idx:02d}_{v['id']}.jpg")
        meta.update({"photo": {k: asset[k] for k in ("file", "page", "license", "artist", "caption")}})
        return {"path": str(canvas), "kind": "image", "meta": meta}

    if t == "chart":
        data = None
        try:
            data = worldbank.fetch_series(v["indicator"], v["countries"], int(v["from"]), int(v["to"]))
        except Exception as e:  # noqa: BLE001
            meta["chart_error"] = str(e)[:120]
        if data:
            allowed = source_numbers(pack) | _chart_numbers(data)
            bad = ungrounded_numbers(narration, allowed)
            if bad:
                raise ContentMismatch(f"장면 {idx+1} 나레이션 수치 {bad}가 자료/차트 데이터에 없음")
            out = out_dir / f"scene{idx:02d}_chart.mp4"
            cards.line_chart(profile, data, out)
            meta["source"] = data["source"]
            return {"path": str(out), "kind": "anim", "meta": meta}
        # 데이터 없음 → 같은 내용을 문구 카드로 (무관한 그림으로 채우지 않는다)
        if numbers_in(narration) and ungrounded_numbers(narration, source_numbers(pack)):
            raise ContentMismatch(f"장면 {idx+1}: 차트 데이터를 못 구했고 나레이션 수치가 자료에 없음")
        headline = _headline(narration)
        out = out_dir / f"scene{idx:02d}_text.mp4"
        cards.kinetic_text(profile, headline, "", out)
        meta["fallback_from"] = "chart"
        return {"path": str(out), "kind": "anim", "meta": meta}

    if t == "stat":
        out = out_dir / f"scene{idx:02d}_stat.mp4"
        cards.stat(profile, str(v["big"]), str(v["small"]), out, source=_src_tag(pack))
        return {"path": str(out), "kind": "anim", "meta": meta}
    if t == "compare":
        out = out_dir / f"scene{idx:02d}_cmp.mp4"
        cards.compare(profile, v.get("title", ""), v["a"], v["b"], v.get("unit", ""), out, source=_src_tag(pack))
        return {"path": str(out), "kind": "anim", "meta": meta}
    if t == "steps":
        out = out_dir / f"scene{idx:02d}_steps.mp4"
        cards.steps(profile, v.get("title", ""), [str(x) for x in v["items"]], out)
        return {"path": str(out), "kind": "anim", "meta": meta}
    out = out_dir / f"scene{idx:02d}_text.mp4"
    cards.kinetic_text(profile, v.get("headline") or _headline(narration), v.get("sub", ""), out)
    return {"path": str(out), "kind": "anim", "meta": meta}


def _headline(narration: str) -> str:
    s = narration.split(".")[0].strip()
    return s if len(s) <= 22 else s[:21].rstrip() + "…"


def _src_tag(pack: Dict) -> str:
    return "자료: " + ("위키백과" if pack["sources"] else "")

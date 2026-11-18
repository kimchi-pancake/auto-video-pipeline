"""
v2/script.py
=============
시청 지속률 중심 대본 생성. 출력은 JSON(장면별 나레이션 + 시각 자료 지정 + 주장·인용).
검증은 코드가 한다: 형식, 길이, 유도 멘트, 외국어, 숫자 근거, 주장 인용, 시각 자료 다양성.
"""
from __future__ import annotations

import re
from typing import Dict, List

from v2.channels import ChannelProfile
from v2.llm import call_json
from v2.research import (norm, numbers_for_prompt, pack_text_for_prompt, photos_for_prompt, source_numbers,
                         ungrounded_numbers, verify_claims)
from v2.sources import worldbank

CHARS_PER_SEC = 6.4          # Edge-TTS 한국어 +12~14% 기준 대략치
MIN_CHARS, MAX_CHARS = 90, 520
VISUAL_TYPES = ("photo", "chart", "stat", "compare", "steps", "text")

_FILLER = re.compile(
    r"구독|좋아요|알림\s*설정|두\s*번\s*(눌|탭)|화면을\s*(두|눌)|도움이\s*되셨|끝까지\s*(보|시청)|"
    r"지금부터(입니다|가\s*진짜)|다음\s*(영상|편)에서|댓글로|공유해\s*주세요|여러분\s*(의견|생각)은"
)
_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿]")  # 한자·가나는 TTS/자막에서 깨지거나 어색함
_HEDGE = re.compile(r"(해석|분석|전망|가능성|수 있|것으로 (보|여)|라는 (시각|평가|지적)|볼 수|보입니다|추정)")

PROMPT_RULES = """[핵심 규칙]
1. 길이는 소재에 맞게 네가 정한다: target_seconds(15~60)를 먼저 정하고, 나레이션 전체 글자 수(공백 포함)를 약 target_seconds×6.4자에 맞춰라(예: 35초 ≈ 220자, 60초 ≈ 380자). 장면당 25~60자.
   정보가 적으면 짧게 끝내라. 늘리려고 같은 말을 반복하지 마라.
2. 첫 장면 나레이션은 [선택한 훅]의 first_line으로 시작한다(문장을 자연스럽게 다듬는 것은 가능).
3. 모든 장면은 앞 장면에 없던 새 정보(수치·이름·원인·결과·비교)를 담는다. 이미 말한 것을 다시 설명하지 마라.
4. 훅에서 던진 궁금증은 영상 끝 전에 실제로 답해라. 마지막 장면은 그 답과 의미를 한두 문장으로 정리한다.
5. 구독·좋아요·댓글 요청, '끝까지 보세요', 내용 없는 예고 문장은 한 문장도 쓰지 마라.
6. 한국어 존댓말, 한 문장 한 정보, TTS로 읽기 자연스럽게. 전문용어는 쉬운 말로 풀어라.
7. 나레이션에는 알파벳을 쓰지 마라(약어도 한글로: 국제통화기금, 엠에스지 등). 숫자는 아라비아 숫자 사용 가능.
8. 숫자·연도·수치는 [근거 자료]에 있는 것만 써라. 자료에 없으면 쓰지 말고, 있는 다른 사실로 채워라.
9. 각 장면마다 claims를 달아라: 그 장면 나레이션의 핵심 주장 1~2개.
   - kind="fact": 근거 자료에서 그대로 확인되는 사실. source(S1 등)와 quote(근거 자료 원문 일부를 그대로 복사, 10자 이상)를 반드시 쓴다.
   - kind="interpretation": 해석·분석. 나레이션에 '~로 해석됩니다/~라는 분석이 있습니다' 같은 표현을 쓴다.
   - kind="forecast": 전망. '~라면 ~할 수 있습니다'처럼 조건부로만. 가격 예측·투자 권유 금지.
10. 시각 자료(visual)는 장면 내용에 실제로 맞는 것만 고른다. 맞는 사진이 없으면 stat/steps/compare/text/chart로 표현해라. 무관한 사진을 억지로 쓰지 마라.
   - photo: {"type":"photo","id":"P3"}  — [사용 가능한 사진] 목록의 id만. 같은 사진은 최대 2번.
   - chart: {"type":"chart","indicator":"지표키","countries":["KOR","USA"],"from":1990,"to":2022}  — 세계은행 실제 통계. 장면 나레이션이 그 그래프가 보여주는 내용을 말할 때만.
   - stat: {"type":"stat","big":"큰 숫자/문구(12자 이내)","small":"설명 한 줄(24자 이내)"}  — big의 숫자는 근거 자료에 있어야 한다.
   - compare: {"type":"compare","title":"","a":{"label":"","value":숫자},"b":{"label":"","value":숫자},"unit":"단위"}  — 두 값 모두 근거 자료에 있어야 한다.
   - steps: {"type":"steps","title":"","items":["원인","과정","결과"]}  — 항목 2~4개, 각 14자 이내. 인과 순서.
   - text: {"type":"text","headline":"핵심 문구(20자 이내)","sub":""}
11. 서로 다른 시각 유형을 최소 3종 이상 섞고, 같은 유형이 3장면 연속되지 않게 한다. 첫 장면은 photo 또는 stat 권장.
"""


from v2.textnorm import normalize_narration  # noqa: E402


def normalize_script(script: Dict) -> Dict:
    for sc in script.get("scenes", []) if isinstance(script, dict) else []:
        if isinstance(sc.get("narration"), str):
            sc["narration"] = normalize_narration(sc["narration"]).strip()
        v = sc.get("visual") or {}
        for k in ("big", "small", "headline", "sub", "title", "unit"):
            if isinstance(v.get(k), str):
                v[k] = normalize_narration(v[k])
    if isinstance(script, dict) and isinstance(script.get("title"), str):
        script["title"] = normalize_narration(script["title"])
    return script


def build_prompt(topic: Dict, pack: Dict, hook: Dict, profile: ChannelProfile, hints: Dict | None = None) -> str:
    hints = hints or {}
    length_hint = {
        "short": "이번 영상은 target_seconds 15~28 범위로 짧고 밀도 있게 만든다.",
        "medium": "이번 영상은 target_seconds 28~42 범위로 만든다.",
        "long": "이번 영상은 target_seconds 42~60 범위로, 정보가 충분할 때만 길게 만든다.",
    }.get(hints.get("length_bucket"), "")
    style_hint = {
        "photo_heavy": "시각 자료는 실제 사진(photo)을 가능한 한 많이 쓰고(절반 이상), 카드·차트는 꼭 필요한 곳에만 써라.",
        "graphic_heavy": "시각 자료는 숫자 카드·차트·비교·단계(stat/chart/compare/steps)를 중심으로 쓰고, 사진은 최대 1~2장만 써라.",
    }.get(hints.get("visual_style"), "")
    chart_block = ""
    if profile.charts_enabled:
        chart_block = f"\n[chart 장면에 쓸 수 있는 세계은행 지표/국가]\n{worldbank.menu_text()}\n"
    return f"""{profile.persona}

{profile.tone_rules}

[주제] {topic['title']} (카테고리: {topic.get('category','')})
[선택한 훅] first_line="{hook['first_line']}" / screen_text="{hook['screen_text']}" / 약속="{hook.get('promise','')}"
{length_hint}
{style_hint}

[근거 자료 — 사실은 여기 있는 것만]
{pack_text_for_prompt(pack)}

[사용 가능한 숫자 — 나레이션과 시각 자료의 두 자리 이상 숫자는 이 목록에서만 골라라]
{numbers_for_prompt(pack)}

[사용 가능한 사진]
{photos_for_prompt(pack)}
{chart_block}
{PROMPT_RULES}
허용 카테고리: {', '.join(profile.categories)}

JSON 객체 하나만 출력(설명·코드펜스 금지):
{{"title":"영상 제목(12~28자)","target_seconds":숫자,"category":"허용 카테고리 중 하나","bgm_mood":"calm|tense|bright",
 "scenes":[{{"narration":"장면 나레이션","visual":{{...}},"claims":[{{"text":"주장","kind":"fact|interpretation|forecast","source":"S1","quote":"원문 일부"}}]}}]}}"""


def validate(script: Dict, pack: Dict, profile: ChannelProfile, hook: Dict) -> None:
    """문제가 있으면 ValueError(메시지는 LLM 재작성 프롬프트에 그대로 쓰인다)."""
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        raise ValueError("scenes 배열이 없음")
    scenes = script["scenes"]
    if not (4 <= len(scenes) <= 11):
        raise ValueError(f"장면 수 {len(scenes)}개 — 4~11개여야 함")
    if script.get("category") not in profile.categories:
        raise ValueError(f"category는 {profile.categories} 중 하나여야 함")
    chars = 0
    photo_ids = {p["id"] for p in pack["photos"]}
    photo_use: Dict[str, int] = {}
    kinds: List[str] = []
    src_nums = source_numbers(pack)
    nums_extra: List[str] = []
    for i, sc in enumerate(scenes):
        nar = (sc.get("narration") or "").strip()
        if not (8 <= len(nar) <= 120):
            raise ValueError(f"장면 {i+1} 나레이션 길이 {len(nar)}자 — 8~120자여야 함")
        if _FILLER.search(nar):
            raise ValueError(f"장면 {i+1}에 구독/좋아요/예고식 유도 문장이 있음: {nar[:40]}")
        if _CJK.search(nar):
            raise ValueError(f"장면 {i+1} 나레이션에 한자/일본어 문자가 있음(한글로 풀어 쓸 것): {nar[:40]}")
        if _LATIN.search(nar):
            raise ValueError(f"장면 {i+1} 나레이션에 알파벳이 있음(한글로 풀어 쓸 것): {nar[:40]}")
        chars += len(nar)
        v = sc.get("visual") or {}
        t = v.get("type")
        if t not in VISUAL_TYPES:
            raise ValueError(f"장면 {i+1} visual.type 오류: {t}")
        kinds.append(t)
        for k in ("big", "small", "headline", "sub", "title", "unit"):
            if isinstance(v.get(k), str) and (_CJK.search(v[k]) or _LATIN.search(v[k])):
                raise ValueError(f"장면 {i+1} 화면 문구({k})에 한자/알파벳이 있음 — 한글로 써라: {v[k][:30]}")
        for it in v.get("items", []) or []:
            if _CJK.search(str(it)) or _LATIN.search(str(it)):
                raise ValueError(f"장면 {i+1} steps 항목에 한자/알파벳이 있음 — 한글로 써라")
        if t == "photo":
            if v.get("id") not in photo_ids:
                raise ValueError(f"장면 {i+1}: 없는 사진 id {v.get('id')} (사용 가능: {sorted(photo_ids)})")
            photo_use[v["id"]] = photo_use.get(v["id"], 0) + 1
            if photo_use[v["id"]] > 2:
                raise ValueError(f"사진 {v['id']}을(를) 3번 이상 사용")
        elif t == "chart":
            if not profile.charts_enabled:
                raise ValueError("이 채널은 chart 사용 불가")
            if v.get("indicator") not in worldbank.INDICATORS:
                raise ValueError(f"장면 {i+1}: 지원하지 않는 지표 {v.get('indicator')}")
            cs = [c for c in (v.get("countries") or []) if c in worldbank.COUNTRIES]
            if not cs:
                raise ValueError(f"장면 {i+1}: 지원하지 않는 국가 코드")
            try:
                int(v.get("from")); int(v.get("to"))
            except (TypeError, ValueError):
                raise ValueError(f"장면 {i+1}: chart from/to 연도 필요")
        elif t == "stat":
            if not v.get("big") or not v.get("small") or len(str(v["big"])) > 14:
                raise ValueError(f"장면 {i+1}: stat big(14자 이내)/small 필요")
            nums_extra.append(str(v["big"]))
        elif t == "compare":
            for side in ("a", "b"):
                if not isinstance(v.get(side), dict) or not v[side].get("label") or v[side].get("value") is None:
                    raise ValueError(f"장면 {i+1}: compare {side}.label/value 필요")
                nums_extra.append(str(v[side]["value"]))
        elif t == "steps":
            items = v.get("items") or []
            if not (2 <= len(items) <= 4) or any(len(str(x)) > 18 for x in items):
                raise ValueError(f"장면 {i+1}: steps 항목 2~4개, 각 18자 이내")
        elif t == "text":
            if not v.get("headline") or len(v["headline"]) > 24:
                raise ValueError(f"장면 {i+1}: text.headline 24자 이내 필요")
    if not (MIN_CHARS <= chars <= MAX_CHARS):
        raise ValueError(f"나레이션 총 {chars}자 — {MIN_CHARS}~{MAX_CHARS}자여야 함")
    if len(set(kinds)) < 3:
        raise ValueError("서로 다른 시각 유형을 3종 이상 써야 함")
    for a, b, c in zip(kinds, kinds[1:], kinds[2:]):
        if a == b == c:
            raise ValueError("같은 시각 유형이 3장면 연속됨")
    first = norm(scenes[0]["narration"])
    fl = norm(hook["first_line"])
    overlap = sum(1 for ch in set(fl) if ch in first) / max(len(set(fl)), 1)
    if overlap < 0.6:
        raise ValueError("첫 장면이 선택한 훅(first_line)으로 시작하지 않음")

    # 숫자 근거: 나레이션/시각 자료의 두 자리 이상 숫자는 근거 자료에 있어야 함(차트 데이터는 렌더 때 따로 검증)
    allowed = set(src_nums)
    for sc in scenes:
        v = sc["visual"]
        if v.get("type") == "chart":
            continue  # 차트 장면의 나레이션은 차트 데이터에서 온 수치일 수 있어 stage 2에서 검증
        bad = ungrounded_numbers(sc["narration"], allowed)
        if bad:
            raise ValueError(f"근거 자료에 없는 숫자 {bad} (나레이션: {sc['narration'][:40]}) — 자료에 있는 사실로 바꿔라")
    for txt in nums_extra:
        bad = ungrounded_numbers(txt, allowed)
        if bad:
            raise ValueError(f"시각 자료의 숫자 {bad}가 근거 자료에 없음")

    # 주장·인용
    by_id = {s["id"]: s for s in pack["sources"]}
    grounded_scenes = 0
    interp = total = 0
    for i, sc in enumerate(scenes):
        claims = sc.get("claims") or []
        if not claims:
            raise ValueError(f"장면 {i+1}에 claims가 없음")
        ok_scene = False
        for c in claims:
            total += 1
            kind = c.get("kind", "fact")
            if kind in ("interpretation", "forecast"):
                if _HEDGE.search(sc["narration"]):
                    interp += 1
                    continue
                # 해석이라고 표시했지만 나레이션이 단정형이면 '사실 주장'으로 취급해 근거를 요구한다.
                # (모델이 같은 지적을 반복해서 못 고치는 일이 잦아 규칙을 되풀이시키기보다 더 엄격한 쪽으로 판정)
                c["kind"] = "fact"
                c["reclassified"] = "단정형 해석 → 사실로 검증"
            src = by_id.get(c.get("source")) or next(iter(by_id.values()), None)
            if not src:
                raise ValueError(f"장면 {i+1}: claim.source 오류 {c.get('source')}")
            c["source"] = src["id"]
            from v2.research import find_evidence, quote_supported
            if quote_supported(c.get("quote", ""), src["text"]):
                ok_scene = True
            else:
                ev = find_evidence(c.get("text", "") or sc["narration"], pack)
                if ev:
                    c["source"], c["quote"], c["quote_found_by"] = ev[0], ev[1][:200], "code"
                    ok_scene = True
                else:
                    raise ValueError(f"장면 {i+1}: 이 주장을 뒷받침하는 문장을 근거 자료에서 찾지 못함 → '{(c.get('text') or '')[:40]}'. "
                                     "자료에 실제로 있는 사실만 쓰거나, 해석이면 kind를 interpretation으로 하고 '~로 해석됩니다'를 써라")
        grounded_scenes += 1 if ok_scene else 0
    if grounded_scenes / len(scenes) < 0.6:
        raise ValueError("사실 근거(quote)가 확인된 장면이 60% 미만")
    if total and interp / total > 0.4:
        raise ValueError("해석/전망 주장이 40%를 넘음 — 확인된 사실 비중을 늘려라")


def evaluate_script(script: Dict) -> Dict:
    """자동 대본 품질 평가(내부 점검용, 조회수 예측 아님)."""
    narr = [s["narration"] for s in script["scenes"]]
    chars = sum(len(n) for n in narr)
    ents = []
    for n in narr:
        ents.append(len(set(re.findall(r"\d+|[가-힣]{2,}", n))))
    from v2.topics import similarity
    rep = max((similarity(a, b) for i, a in enumerate(narr) for b in narr[i + 1:]), default=0.0)
    kinds = [s["visual"]["type"] for s in script["scenes"]]
    return {
        "chars": chars, "est_seconds": round(chars / CHARS_PER_SEC, 1), "scenes": len(narr),
        "avg_new_tokens_per_scene": round(sum(ents) / len(ents), 1),
        "max_scene_similarity": round(rep, 2),
        "visual_mix": {k: kinds.count(k) for k in set(kinds)},
        "target_seconds_declared": script.get("target_seconds"),
    }


def write(topic: Dict, pack: Dict, hook: Dict, profile: ChannelProfile, hints: Dict | None = None) -> Dict:
    prompt = build_prompt(topic, pack, hook, profile, hints)
    def _check(d):
        normalize_script(d)
        validate(d, pack, profile, hook)
    script = call_json(prompt, validate=_check, repairs=3, max_tokens=12000)
    script["claims_verified"] = verify_claims(script, pack)
    script["eval"] = evaluate_script(script)
    return script

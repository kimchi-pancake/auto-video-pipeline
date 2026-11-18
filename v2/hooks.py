"""
v2/hooks.py
============
제목·훅 후보를 여러 개 만들고 규칙 기반으로 평가한다.
평가 점수는 "내부 점검용"이다 — 실제 조회수를 보장하거나 예측하지 않는다.
"""
from __future__ import annotations

import re
from typing import Dict, List

from v2.channels import ChannelProfile
from v2.llm import call_json
from v2.research import norm, pack_text_for_prompt, quote_supported
from v2.topics import similarity

HOOK_TYPES = {
    "surprising_fact": "의외의 사실을 먼저 제시",
    "contrary_result": "상식과 다른 결과",
    "comparison": "두 대상 사이의 흥미로운 비교",
    "who_wins": "손익 구조(누가 얻고 누가 잃나)에 대한 궁금증",
    "result_first": "결과를 먼저 보여주고 원인을 설명",
    "hidden_tech": "익숙한 것에 숨은 기술/역사",
    "number_gap": "실제 수치가 만드는 놀라운 차이",
    "problem_solution": "흥미로운 문제를 제기하고 해결 과정 공개",
}
HYPE = re.compile(r"(충격|역대급|전\s*세계가\s*(놀란|경악)|소름|미쳤|난리|경악|대박|발칵|초토화)")


def prompt(topic: Dict, pack: Dict, profile: ChannelProfile, recent_hooks: List[str]) -> str:
    types = "\n".join(f"  - {k}: {v}" for k, v in HOOK_TYPES.items())
    recent = "\n".join(f"- {h}" for h in recent_hooks[:12]) or "(없음)"
    return f"""{profile.persona}

[주제] {topic['title']}  (카테고리: {topic.get('category','')})
{('[시청자가 궁금해할 점] ' + topic['angle']) if topic.get('angle') else ''}

아래 [근거 자료]만 사실의 근거로 쓸 수 있다.

{pack_text_for_prompt(pack)}

[할 일] 이 주제로 쇼츠 영상의 제목·훅 후보를 서로 다른 유형으로 6개 만들어라. 유형 목록:
{types}

[규칙]
- first_line: 영상 첫 대사(한국어 존댓말, 30자 이내, 인사·소개 금지). 첫 대사만 들어도 무엇이 흥미로운지 알 수 있어야 한다.
- screen_text: 첫 화면에 크게 띄울 문구(16자 이내).
- title: 영상 제목(12~28자). 본문이 실제로 약속을 지킬 수 있는 말만 쓴다. 낚시 금지.
- promise: 시청자가 끝까지 보면 얻는 정보 한 줄.
- evidence_quote: 이 훅이 말하는 사실이 들어 있는 [근거 자료]의 원문 문장 일부(그대로 복사, 10자 이상).
- 훅은 소재의 핵심 매력(원리·역사·성과·반전)에서 뽑아라. 정치·남북관계·논란·사건사고·소송 같은 곁가지를 중심 훅으로 삼지 마라.
- '충격', '역대급', '전 세계가 놀란', '소름' 같은 과장어는 쓰지 마라.
- 최근 훅과 비슷한 문장 구조·유형을 피해라:
{recent}

JSON 배열만 출력:
[{{"type":"유형키","first_line":"","screen_text":"","title":"","promise":"","evidence_quote":""}}]"""


def evaluate(c: Dict, pack: Dict, recent_hooks: List[str], recent_types: List[str]) -> Dict:
    src_text = " ".join(s["text"] for s in pack["sources"])
    fl, title = c.get("first_line", ""), c.get("title", "")
    comp = {}
    comp["grounded"] = 1.0 if quote_supported(c.get("evidence_quote", ""), src_text) else 0.0
    comp["first_line_len"] = 1.0 if 8 <= len(fl) <= 30 else (0.5 if len(fl) <= 38 else 0.0)
    comp["title_len"] = 1.0 if 12 <= len(title) <= 28 else (0.5 if 8 <= len(title) <= 32 else 0.0)
    comp["specific"] = 1.0 if (re.search(r"\d", fl + title) or any(norm(w) in norm(src_text) for w in re.findall(r"[가-힣A-Za-z]{2,}", title)[:3])) else 0.4
    comp["no_hype"] = 0.0 if HYPE.search(fl + title + c.get("screen_text", "")) else 1.0
    comp["no_greeting"] = 0.0 if re.match(r"^(안녕|오늘은|여러분|지금부터)", fl.strip()) else 1.0
    sim = max((similarity(fl, h) for h in recent_hooks), default=0.0)
    comp["novel"] = 1.0 - sim
    comp["type_variety"] = 0.3 if c.get("type") in recent_types[:3] else 1.0
    w = {"grounded": 0.28, "first_line_len": 0.08, "title_len": 0.06, "specific": 0.16, "no_hype": 0.14, "no_greeting": 0.08, "novel": 0.12, "type_variety": 0.08}
    total = sum(w[k] * comp[k] for k in w)
    if comp["grounded"] == 0.0:
        total *= 0.5  # 근거 없는 훅은 크게 감점
    return {"score": round(total, 4), "components": {k: round(v, 2) for k, v in comp.items()}}


def generate(topic: Dict, pack: Dict, profile: ChannelProfile, recent_hooks: List[str], recent_types: List[str], prefer_type: str | None = None) -> Dict:
    def _validate(data):
        if not isinstance(data, list) or len(data) < 3:
            raise ValueError("훅 후보가 3개 미만")
        for d in data:
            for k in ("first_line", "title", "screen_text", "type"):
                if not d.get(k):
                    raise ValueError(f"{k} 누락")
    cands = call_json(prompt(topic, pack, profile, recent_hooks), validate=_validate, max_tokens=6000)
    for c in cands:
        c["eval"] = evaluate(c, pack, recent_hooks, recent_types)
        if prefer_type and c.get("type") == prefer_type:
            # 실험 변수(훅 유형 선호): 점수에 작은 가산만 준다(근거 없는 훅이 뽑히지는 않게)
            c["eval"]["score"] = round(c["eval"]["score"] + 0.12, 4)
            c["eval"]["experiment_bonus"] = 0.12
    cands.sort(key=lambda c: c["eval"]["score"], reverse=True)
    return {"chosen": cands[0], "candidates": cands}

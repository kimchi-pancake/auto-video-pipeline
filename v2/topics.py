"""
v2/topics.py
=============
Topic Discovery & Ranking Engine.

후보 발견 경로
  seed       큐레이션한 상시 소재(콜드스타트용, 근거 자료가 풍부한 문서만)
  trend      위키백과 일간 인기 문서 / Google Trends KR 공개 RSS → LLM이 채널 적합성 분류
  winner     이 채널에서 성과가 좋았던 영상의 후속 관점 (insights.json 이 있을 때)
  comment    시청자 댓글의 반복 질문 (collect_comment_ideas, 자격증명이 있을 때)

점수(Topic Opportunity Score)는 설명 가능한 규칙 기반이며 조회수 확률이 아니다.
  score = Σ w_i · component_i  — 각 성분과 가중치를 후보에 그대로 기록한다.
데이터가 적을 땐 성과 prior를 약하게 반영하고 탐색 보너스(UCB 형)로 소재 쏠림을 막는다.
"""
from __future__ import annotations

import json
import math
import random
import re
from typing import Dict, List, Optional

from v2 import DATA
from v2.channels import ChannelProfile, load_v2_config
from v2.llm import LLMError, call_json
from v2.sources import trends

# (한국어 위키 문서 제목, 카테고리)
SEEDS_DOGAE = [
    ("누리호", "우주과학"), ("다누리", "우주과학"), ("직지심체요절", "역사유산"), ("훈민정음", "역사유산"),
    ("팔만대장경", "역사유산"), ("석굴암", "역사유산"), ("거북선", "역사유산"), ("측우기", "역사유산"),
    ("온돌", "생활편의"), ("김장", "음식문화"), ("불닭볶음면", "음식문화"), ("초코파이", "음식문화"),
    ("인천국제공항", "생활편의"), ("서울 지하철", "생활편의"), ("KTX", "제조산업"), ("포항제철소", "제조산업"),
    ("KF-21", "첨단기술"), ("K2 전차", "첨단기술"), ("K9 자주포", "첨단기술"), ("바라카 원자력 발전소", "첨단기술"),
    ("삼성전자", "첨단기술"), ("SK하이닉스", "첨단기술"), ("HD현대중공업", "제조산업"), ("LNG 운반선", "제조산업"),
    ("한글", "역사유산"), ("수원 화성", "역사유산"), ("해인사 장경판전", "역사유산"), ("성덕대왕신종", "역사유산"),
    ("첨성대", "역사유산"), ("백제 금동대향로", "역사유산"), ("고려청자", "역사유산"), ("판옥선", "역사유산"),
    ("기생충 (영화)", "대중문화"), ("오징어 게임", "대중문화"), ("방탄소년단", "대중문화"), ("웹툰", "대중문화"),
    ("김연아", "스포츠"), ("쇼트트랙 스피드스케이팅", "스포츠"), ("양궁", "스포츠"), ("리그 오브 레전드 월드 챔피언십", "스포츠"),
    ("경부고속도로", "제조산업"), ("청계천", "생활편의"), ("한강의 기적", "제조산업"), ("국민건강보험", "의료복지"),
    ("한지", "역사유산"), ("한복", "역사유산"), ("비빔밥", "음식문화"), ("택배", "생활편의"), ("대한민국의 인터넷", "첨단기술"),
]
SEEDS_UJJJAM = [
    ("플라자 합의", "환율달러"), ("브레턴우즈 체제", "환율달러"), ("닉슨 쇼크", "환율달러"), ("페트로달러", "환율달러"),
    ("기축통화", "환율달러"), ("1997년 아시아 금융 위기", "금융위기"), ("2008년 세계 금융 위기", "금융위기"),
    ("리먼 브러더스", "금융위기"), ("대공황", "금융위기"), ("그리스 정부 부채 위기", "국가부채"),
    ("아르헨티나의 경제 위기", "국가부채"), ("짐바브웨의 초인플레이션", "물가금리"),
    ("바이마르 공화국의 초인플레이션", "물가금리"), ("스태그플레이션", "물가금리"), ("양적 완화", "물가금리"),
    ("제1차 석유 파동", "에너지자원"), ("석유수출국기구", "에너지자원"), ("자원의 저주", "에너지자원"),
    ("네덜란드병", "경제사"), ("스무트-홀리 관세법", "무역제재"), ("관세", "무역제재"), ("보호무역주의", "무역제재"),
    ("경제 제재", "무역제재"), ("수에즈 운하", "공급망"), ("에버 기븐호의 수에즈 운하 좌초 사건", "공급망"),
    ("파나마 운하", "공급망"), ("컨테이너선", "공급망"), ("TSMC", "산업패권"), ("ASML", "산업패권"),
    ("희토류 원소", "산업패권"), ("반도체", "산업패권"), ("잃어버린 10년 (일본)", "경제사"), ("국제통화기금", "국제정세"),
    ("세계무역기구", "국제정세"), ("SWIFT", "국제정세"), ("조세 피난처", "빈부격차"), ("지니 계수", "빈부격차"),
    ("마샬 플랜", "경제사"), ("플랜테이션 경제?", "경제사"), ("빅맥 지수", "환율달러"), ("튤립 파동", "금융위기"),
    ("남해 회사 거품 사건", "금융위기"), ("외환 보유액", "환율달러"), ("국가 부도", "국가부채"), ("인플레이션", "물가금리"),
]


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similarity(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / max(len(A | B), 1)


def max_similarity(title: str, others: List[str]) -> float:
    return max((similarity(title, o) for o in others), default=0.0)


def load_insights() -> dict:
    p = DATA / "insights.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def category_prior(channel: str, category: str, insights: dict) -> tuple[float, int]:
    """(상대 성과 점수 0~1, 표본 수). 표본이 적으면 0.5 쪽으로 수축(shrinkage)한다."""
    cat = (insights.get(channel, {}).get("categories", {}) or {}).get(category)
    if not cat:
        return 0.5, 0
    n, rel = cat.get("n", 0), cat.get("relative", 0.5)
    k = 5.0  # 사전 표본 수
    return (rel * n + 0.5 * k) / (n + k), n


WEIGHTS = {"interest": 0.22, "fit": 0.22, "novelty": 0.20, "evidence": 0.16, "performance": 0.12, "explore": 0.08}


def score(candidate: dict, ctx: dict) -> dict:
    ch, insights = ctx["channel"], ctx["insights"]
    comps = {}
    src = candidate["source"]
    if src == "seed":
        comps["interest"] = 0.45
    elif src == "wikipedia_top":
        comps["interest"] = min(0.6, math.log10(max(candidate.get("views", 1), 10)) / 8.0)  # 인기 문서가 상시 소재를 압도하지 않게 상한
    elif src == "google_trends_kr":
        comps["interest"] = 0.7
    else:
        comps["interest"] = 0.6
    comps["fit"] = candidate.get("fit", 1.0 if src == "seed" else 0.5)
    comps["novelty"] = 1.0 - max_similarity(candidate["title"], ctx["recent_titles"])
    comps["evidence"] = candidate.get("evidence", 0.6)  # 조사 후 research.py 가 갱신
    perf, n = category_prior(ch, candidate.get("category", ""), insights)
    comps["performance"] = perf
    cat_counts = ctx["category_counts"]
    total = max(sum(cat_counts.values()), 1)
    c = ctx["explore_c"]
    comps["explore"] = min(1.0, c * math.sqrt(math.log(total + 1) / (cat_counts.get(candidate.get("category", ""), 0) + 1)))
    total_score = sum(WEIGHTS[k] * comps[k] for k in WEIGHTS)
    # 최근 소재와 거의 같으면 사실상 탈락
    if comps["novelty"] < 0.25:
        total_score *= 0.3
    return {"score": round(total_score, 4), "components": {k: round(v, 3) for k, v in comps.items()},
            "explain": f"관심 {comps['interest']:.2f} · 적합 {comps['fit']:.2f} · 신선도 {comps['novelty']:.2f} · "
                       f"근거 {comps['evidence']:.2f} · 성과prior {perf:.2f}(n={n}) · 탐색 {comps['explore']:.2f}"}


def _triage_prompt(profile: ChannelProfile, items: List[dict]) -> str:
    lines = "\n".join(f"- {i['title']}" for i in items)
    return (
        f"{profile.persona}\n\n아래는 오늘 한국에서 관심을 받는 위키백과 문서/검색어다.\n{lines}\n\n"
        f"이 채널({profile.name}: {profile.kind})이 '설명 영상'으로 만들 가치가 있는 것만 골라라. "
        f"카테고리 후보: {', '.join(profile.categories)}.\n"
        "제외: 연예인 사생활·사건사고·사망·범죄·선거·정당·정치인 논란·스포츠 경기 결과 자체·단순 날짜/요일 문서.\n"
        "포함: 배경 원리·역사·기술·제도·경제 구조를 3~4개 사실로 풀어 설명할 수 있는 주제.\n"
        'JSON 배열만 출력: [{"title":"입력과 똑같은 제목","fit":0.0~1.0,"category":"카테고리","angle":"시청자가 궁금해할 질문 한 줄"}]'
    )


def discover(profile: ChannelProfile, recent_titles: List[str], rng: Optional[random.Random] = None) -> List[dict]:
    """후보 목록(점수 포함, 내림차순). 네트워크/LLM 실패는 건너뛰고 seed 만으로도 항상 후보를 돌려준다."""
    rng = rng or random.Random()
    cfg = load_v2_config()
    insights = load_insights()
    seeds = SEEDS_UJJJAM if profile.kind == "world_econ" else SEEDS_DOGAE
    cands: List[dict] = []
    for title, cat in seeds:
        title = title.rstrip("?")
        cands.append({"title": title, "wiki_title": title, "category": cat, "source": "seed", "angle": ""})

    # 트렌드 신호 (실패해도 계속)
    signals: List[dict] = []
    try:
        signals = trends.wikipedia_top() + trends.google_trends_kr()
    except Exception:
        signals = []
    if signals:
        try:
            picked = call_json(_triage_prompt(profile, signals[:70]), repairs=0, max_tokens=6000)
            by_title = {s["title"]: s for s in signals}
            for p in picked if isinstance(picked, list) else []:
                s = by_title.get(p.get("title"))
                if not s or float(p.get("fit", 0)) < 0.75 or p.get("category") not in profile.categories:
                    continue
                cands.append({"title": p["title"], "wiki_title": p["title"], "category": p["category"], "source": s["source"],
                              "views": s.get("views", 0), "fit": float(p["fit"]), "angle": p.get("angle", "")})
        except (LLMError, ValueError, TypeError):
            pass

    # 채널 성과가 좋았던 영상의 후속 관점
    for w in load_insights().get(profile.name, {}).get("winners", [])[:3]:
        cands.append({"title": w["follow_up"], "wiki_title": w.get("wiki_title", w["follow_up"]), "category": w.get("category", ""),
                      "source": "winner", "angle": w.get("angle", "")})

    cat_counts: Dict[str, int] = {}
    for c in recent_titles:  # 최근 제목으로 카테고리 사용량 추정은 어려우니 job 쪽 카운트를 따로 주입
        pass
    ctx = {"channel": profile.name, "insights": insights, "recent_titles": recent_titles,
           "category_counts": cat_counts, "explore_c": cfg["exploration_c"]}
    return ctx, cands


def rank(profile: ChannelProfile, cands: List[dict], ctx: dict, top: int = 6, rng: Optional[random.Random] = None) -> List[dict]:
    """점수순으로 상위 후보를 뽑되, 상위권 안에서는 점수 비례로 섞어 매일 같은 소재가 나오지 않게 한다."""
    rng = rng or random.Random()
    seen = set()
    scored = []
    for c in cands:
        if c["title"] in seen:
            continue
        seen.add(c["title"])
        s = score(c, ctx)
        scored.append({**c, **s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    pool = scored[:top * 2]
    out = []
    while pool and len(out) < top:
        weights = [max(p["score"], 0.01) ** 2 for p in pool]
        pick = rng.choices(pool, weights=weights, k=1)[0]
        pool.remove(pick)
        out.append(pick)
    return out


def category_usage(store_jobs: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for j in store_jobs:
        c = (j.get("topic") or {}).get("category")
        if c:
            out[c] = out.get(c, 0) + 1
    return out

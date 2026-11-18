"""
v2/sources/worldbank.py
========================
세계은행 공개 API(키 불필요)로 실제 통계를 가져온다. 차트 장면은 이 데이터만 쓴다.
지원 지표/국가는 화이트리스트 — LLM이 임의 코드를 지어내도 여기서 걸러진다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from v2.sources.http import get_json

INDICATORS: Dict[str, Dict] = {
    "gdp_growth": {"code": "NY.GDP.MKTP.KD.ZG", "label": "경제성장률", "unit": "%"},
    "inflation": {"code": "FP.CPI.TOTL.ZG", "label": "소비자물가 상승률", "unit": "%"},
    "gdp_per_capita": {"code": "NY.GDP.PCAP.CD", "label": "1인당 GDP", "unit": "달러"},
    "unemployment": {"code": "SL.UEM.TOTL.ZS", "label": "실업률", "unit": "%"},
    "trade_share": {"code": "NE.TRD.GNFS.ZS", "label": "무역 비중(GDP 대비)", "unit": "%"},
    "exports_share": {"code": "NE.EXP.GNFS.ZS", "label": "수출 비중(GDP 대비)", "unit": "%"},
    "exchange_rate": {"code": "PA.NUS.FCRF", "label": "달러당 자국 통화", "unit": ""},
    "reserves": {"code": "FI.RES.TOTL.CD", "label": "외환보유액", "unit": "달러"},
    "population_65": {"code": "SP.POP.65UP.TO.ZS", "label": "65세 이상 인구 비중", "unit": "%"},
    "co2_per_capita": {"code": "EN.GHG.CO2.PC.CE.AR5", "label": "1인당 이산화탄소 배출", "unit": "톤"},
}
COUNTRIES: Dict[str, str] = {
    "KOR": "한국", "USA": "미국", "JPN": "일본", "CHN": "중국", "DEU": "독일", "GBR": "영국", "FRA": "프랑스",
    "IND": "인도", "BRA": "브라질", "RUS": "러시아", "ARG": "아르헨티나", "TUR": "튀르키예", "MEX": "멕시코",
    "THA": "태국", "IDN": "인도네시아", "GRC": "그리스", "VEN": "베네수엘라", "ZWE": "짐바브웨", "SAU": "사우디아라비아",
    "TWN": "대만", "VNM": "베트남", "ITA": "이탈리아", "ESP": "스페인", "CAN": "캐나다", "AUS": "호주", "WLD": "세계",
    "EUU": "유럽연합", "SGP": "싱가포르", "NGA": "나이지리아", "EGY": "이집트", "ZAF": "남아프리카공화국",
}


def menu_text() -> str:
    ind = "\n".join(f"  - {k}: {v['label']}({v['unit']})" for k, v in INDICATORS.items())
    cty = ", ".join(f"{k}={v}" for k, v in COUNTRIES.items())
    return f"지표:\n{ind}\n국가코드: {cty}"


def fetch_series(indicator: str, countries: List[str], year_from: int, year_to: int) -> Optional[Dict]:
    """{"indicator","label","unit","series":{국가코드:[(연도,값),...]},"source":{...}} 또는 None(데이터 부족)."""
    spec = INDICATORS.get(indicator)
    countries = [c for c in countries if c in COUNTRIES][:3]
    if not spec or not countries:
        return None
    this_year = datetime.now().year
    year_to = min(year_to, this_year - 1)
    year_from = max(1960, min(year_from, year_to - 5))
    url = f"https://api.worldbank.org/v2/country/{';'.join(countries)}/indicator/{spec['code']}"
    data = get_json(url, {"format": "json", "per_page": 4000, "date": f"{year_from}:{year_to}"})
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return None
    series: Dict[str, List] = {c: [] for c in countries}
    for row in data[1]:
        c, v, y = row.get("countryiso3code"), row.get("value"), row.get("date")
        if c in series and v is not None and y and y.isdigit():
            series[c].append((int(y), float(v)))
    series = {c: sorted(v) for c, v in series.items() if len(v) >= 6}
    if not series:
        return None
    return {
        "indicator": indicator, "label": spec["label"], "unit": spec["unit"],
        "series": series,
        "source": {"title": f"World Bank Open Data — {spec['label']} ({spec['code']})",
                   "url": f"https://data.worldbank.org/indicator/{spec['code']}",
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
    }

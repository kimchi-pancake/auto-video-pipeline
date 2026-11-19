"""
v2/textnorm.py — TTS가 잘못 읽는 표기를 한글 발음으로 바꾼다.
숫자 뒤 단위(cm, km, kg …)와 흔한 약어, 그 밖의 영문 대문자 약어(글자 읽기)를 처리한다.
"""
from __future__ import annotations

import re

# (단위 정규식, 한글). 숫자는 그대로 두고 단위만 바꾼다.
_UNIT_WORDS = [
    ("cm", "센티미터"), ("mm", "밀리미터"), ("km", "킬로미터"), ("kg", "킬로그램"), ("m", "미터"), ("t", "톤"),
]
_UNIT_RES = [(re.compile(r"(?<=\d)\s*" + u + r"(?![A-Za-z])"), w) for u, w in _UNIT_WORDS]
_KMH = re.compile(r"km/h(?![A-Za-z])")
_CELSIUS = re.compile("(?<=\\d)\\s*℃")

ACRONYMS = {
    "GDP": "국내총생산", "IMF": "국제통화기금", "WTO": "세계무역기구", "OPEC": "석유수출국기구", "TSMC": "티에스엠씨",
    "ASML": "에이에스엠엘", "LNG": "엘엔지", "KTX": "케이티엑스", "AI": "에이아이", "FRB": "연방준비제도",
    "Fed": "연방준비제도", "CPI": "소비자물가지수", "SWIFT": "스위프트", "BTS": "비티에스", "USD": "달러",
    "EU": "유럽연합", "UN": "국제연합", "NASA": "나사", "KAIST": "카이스트", "LoL": "리그 오브 레전드",
}
_LETTER_NAMES = "에이 비 씨 디 이 에프 지 에이치 아이 제이 케이 엘 엠 엔 오 피 큐 알 에스 티 유 브이 더블유 엑스 와이 제트".split()
_LETTERS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", _LETTER_NAMES))
_FIVEG = re.compile(r"(?<![A-Za-z0-9])5G(?![A-Za-z])")


def _batchim(ch: str) -> bool:
    return "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 != 0


_JOSA = [("은", "는"), ("이", "가"), ("을", "를"), ("과", "와")]


def fix_josa_after(text: str, word: str) -> str:
    """약어를 한글 낱말로 바꾼 뒤 조사가 어색해지지 않게(국내총생산를 → 국내총생산을) 받침에 맞춘다."""
    if not word:
        return text
    last = word[-1]
    has = _batchim(last)
    for with_b, without_b in _JOSA:
        want = with_b if has else without_b
        other = without_b if has else with_b
        text = re.sub(re.escape(word) + other + r"(?![가-힣])", word + want, text)
    return text


def normalize_narration(text: str) -> str:
    text = _KMH.sub("킬로미터 매시", text)
    for rx, word in _UNIT_RES:
        text = rx.sub(word, text)
    text = _CELSIUS.sub("도", text)
    text = _FIVEG.sub("파이브지", text)
    for k, v in sorted(ACRONYMS.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", v, text)
    for v in set(ACRONYMS.values()):
        text = fix_josa_after(text, v)
    # 기종·모델명(F-35, KF-21, K9, A320): 글자는 한글 이름으로, 숫자는 그대로 둔다
    text = re.sub(r"(?<![A-Za-z])([A-Z]{1,4})-?(\d+)(?![A-Za-z])", lambda m: "".join(_LETTERS[c] for c in m.group(1)) + m.group(2), text)
    text = re.sub(r"(?<![A-Za-z])([A-Z]{2,6})(?![A-Za-z])", lambda m: "".join(_LETTERS[c] for c in m.group(1)), text)
    return text

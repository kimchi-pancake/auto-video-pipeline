"""
subtitle/phrasing.py — 자막 끊기를 '의미 단위'로.

예전: 11글자·3어절에서 기계적으로 끊어서 "태환정책을 / 폐지했습니다", "2001년 예금 / 200억 달러가"처럼
어구 중간에서 잘렸다. 지금: 문장의 어절을 동적계획법(DP)으로 묶어서
  - 덩어리 길이가 목표(12자) 근처가 되고(최대 18자 = 두 줄),
  - 쉼표·절 연결어미(-고, -며, -서, -지만 …)·조사 뒤처럼 자연스러운 곳에서 끊기며,
  - 숫자와 그 뒤 단위어, 한두 글자짜리 어절이 혼자 남지 않게 한다.
"""
from __future__ import annotations

import re
from typing import List

_STRONG = re.compile(r"[,.?!…~]$")
# 절이 끝나는 연결/관형 어미 — 여기서 끊으면 듣는 호흡과 맞는다
_CLAUSE = re.compile(r"(고|며|서|면|는데|은데|지만|으나|니까|도록|라서|여서|하여|하며|하고|면서|다가|던|ㄴ데|라며|라고|다며|인데)[,]?$")
# 조사 뒤(약한 끊김 후보)
_PARTICLE = re.compile(r"(에서|으로|로|에게|께|와|과|보다|처럼|부터|까지|마다|조차|만|도|을|를|은|는|이|가)$")
_NUMERIC = re.compile(r"^[~\d.,]+(?:[가-힣]{0,2})$")


def _end_score(tok: str, last: bool) -> float:
    if last:
        return 3.0
    if _STRONG.search(tok):
        return 3.0
    if _CLAUSE.search(tok):
        return 2.0
    if _PARTICLE.search(tok):
        return 1.0
    return 0.0


def _chunk_len(tokens: List[str], i: int, j: int) -> int:
    return sum(len(t) for t in tokens[i:j]) + (j - i - 1)


def phrase_groups(tokens: List[str], target: int = 12, max_chars: int = 18) -> List[List[int]]:
    n = len(tokens)
    if n == 0:
        return []
    INF = 1e9
    dp = [INF] * (n + 1)
    prev = [0] * (n + 1)
    dp[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            L = _chunk_len(tokens, i, j)
            if L > max_chars and (j - i) > 1:
                break
            cost = 0.03 * (L - target) ** 2
            if L < 5 and not (j == n and i == 0):
                cost += 2.5                      # 너무 짧은 조각(혼자 번쩍이는 자막) 억제
            last_chunk = j == n
            cost += (3.0 - _end_score(tokens[j - 1], last_chunk)) * 0.9   # 끊는 자리가 부자연스러울수록 감점
            # 숫자 어절 바로 뒤에서 끊지 않는다(1992년 | 태환법), 그리고 숫자로 끝나는 덩어리 금지
            if not last_chunk and _NUMERIC.match(tokens[j - 1]):
                cost += 4.0
            # 한 덩어리 안에 쉼표가 중간에 끼면 감점(쉼표는 끝에서 끊는 게 자연스럽다)
            if any(_STRONG.search(t) for t in tokens[i:j - 1]):
                cost += 3.0
            if dp[i] + cost < dp[j]:
                dp[j] = dp[i] + cost
                prev[j] = i
    groups: List[List[int]] = []
    j = n
    while j > 0:
        i = prev[j]
        groups.append(list(range(i, j)))
        j = i
    groups.reverse()
    return groups


def two_line_break(tokens: List[str]) -> int:
    """덩어리가 길 때(>10자) 두 줄로 나눌 위치(앞 줄에 들어갈 어절 수). 길이가 가장 비슷하게 나뉘는 곳, 단 숫자 뒤는 피한다."""
    if len(tokens) < 2:
        return 0
    best, best_cost = 0, 1e9
    for k in range(1, len(tokens)):
        a = sum(len(t) for t in tokens[:k]) + k - 1
        b = sum(len(t) for t in tokens[k:]) + len(tokens) - k - 1
        cost = abs(a - b) + (4 if _NUMERIC.match(tokens[k - 1]) else 0) - _end_score(tokens[k - 1], False)
        if cost < best_cost:
            best, best_cost = k, cost
    return best

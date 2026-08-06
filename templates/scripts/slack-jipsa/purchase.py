"""비품 구매기록·월간분석 순수 로직. supply 모듈의 파싱/배치매칭 재활용."""
from __future__ import annotations
import json, re, importlib.util
from pathlib import Path

def _norm(s: str) -> str:
    s = re.sub(r'[\s()（）]+', '', (s or ''))
    return s.lower()

def normalize_dept(raw: str, known: list[str], aliases: dict) -> tuple[str, bool]:
    raw = (raw or '').strip()
    if not raw:
        return '', True
    if raw in known:
        return raw, False
    if raw in aliases and aliases[raw] in known:
        return aliases[raw], False
    nr = _norm(raw)
    for k in known:                       # 정규화 부분일치(짧은 쪽이 긴 쪽에 포함)
        nk = _norm(k)
        if nr and (nr in nk or nk in nr):
            return k, False
    return raw, True

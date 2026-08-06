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

CATEGORIES = ['사무용품','다과·음료','청소·위생','IT·전자','비품·가구','기타']
USES = ['사내비품','공용(사내·외부 혼재)','재판매·대고객']

def classify_items(items: list[str], run_claude, guide_text: str = '') -> dict:
    items = [i for i in dict.fromkeys(items) if i]     # 중복 제거·순서 보존
    if not items:
        return {}
    prompt = (
        "다음 비품 품목들을 각각 '용도'와 '카테고리'로 분류해 JSON 배열로만 출력하라.\n"
        f"카테고리(택1): {', '.join(CATEGORIES)}\n"
        f"용도(택1): {', '.join(USES)}. 기본값은 '사내비품'.\n"
        + (f"분류 가이드:\n{guide_text}\n" if guide_text else "")
        + '각 원소는 {"품목":문자열,"용도":문자열,"카테고리":문자열}. JSON 외 출력 금지.\n\n'
        + '품목:\n' + '\n'.join(f'- {i}' for i in items))
    out = (run_claude(prompt) or '').strip()
    m = re.search(r'\[.*\]', out, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    res = {}
    for d in data:
        if not isinstance(d, dict):
            continue
        name = (d.get('품목') or '').strip()
        cat = (d.get('카테고리') or '').strip()
        use = (d.get('용도') or '').strip()
        if not name:
            continue
        res[name] = {'용도': use if use in USES else '사내비품',
                     '카테고리': cat if cat in CATEGORIES else '기타'}
    return res

def classify_with_cache(items, cache: dict, run_claude, guide_text: str = ''):
    cache = dict(cache)
    misses = [i for i in dict.fromkeys(items) if i and i not in cache]
    if misses:
        fresh = classify_items(misses, run_claude, guide_text)
        cache.update(fresh)
    result = {i: cache[i] for i in dict.fromkeys(items) if i in cache}
    return result, cache

REC_HEADERS = ['일자','부서','용도','카테고리','품목','수량','단가','금액']

def build_records(rows, classify_map, when_ymd, known_depts, dept_aliases):
    recs, warns = [], []
    for r in rows:
        name = (r.get('품명') or '').strip()
        if not name:
            continue
        qty = int(r.get('수량') or 0)
        amt = int(r.get('금액') or 0)
        dept, is_new = normalize_dept(r.get('부서',''), known_depts, dept_aliases)
        if is_new and dept:
            warns.append(f'신규 부서 후보: "{dept}" (마스터에 없음)')
        cls = classify_map.get(name, {'용도':'사내비품','카테고리':'기타'})
        recs.append({'일자': when_ymd, '부서': dept, '용도': cls['용도'],
                     '카테고리': cls['카테고리'], '품목': name, '수량': qty,
                     '단가': round(amt/qty) if qty > 0 else 0, '금액': amt})
    return recs, warns

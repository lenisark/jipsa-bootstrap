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

def _amt(v):
    try: return int(float(str(v).replace(',', '')))
    except (TypeError, ValueError): return 0

def _day_of(v):
    s = str(v or '')
    m = re.search(r'(\d{1,2})\s*일', s)
    if m: return f'{int(m.group(1))}일'
    m = re.search(r'\d{4}-\d{2}-(\d{2})', s)
    if m: return f'{int(m.group(1))}일'
    return s

def log_rows_to_integrated(log_rows):
    """누적 구매로그 행 → 통합원본 행. 로그의 블록ID 보존, 일자는 'N일'로 변환."""
    out=[]
    for r in log_rows:
        out.append({'월':str(r.get('월','')).strip(),'일자':_day_of(r.get('일자')),
                    '부서':r.get('부서',''),'용도':r.get('용도',''),
                    '카테고리':r.get('카테고리',''),'품목':r.get('품목',''),
                    '금액':_amt(r.get('금액')),'블록ID':r.get('블록ID','')})
    return out

def unreflected_months(integrated_rows, candidate_month) -> bool:
    have = {str(r.get('월','')).strip() for r in integrated_rows}
    return candidate_month not in have

def compute_pivots(rows) -> dict:
    from collections import defaultdict
    dm = defaultdict(int); cm = defaultdict(int); um = defaultdict(int)
    du = defaultdict(int); dc = defaultdict(int); mo = defaultdict(int)
    items = []; total = 0
    for r in rows:
        a = _amt(r.get('금액'))
        dep, mon = r.get('부서',''), r.get('월','')
        use, cat = r.get('용도',''), r.get('카테고리','')
        dm[(dep,mon)] += a; cm[(cat,mon)] += a; um[(use,mon)] += a
        du[(dep,use)] += a; dc[(dep,cat)] += a; mo[mon] += a; total += a
        # top20 튜플: (금액, 월, 부서, 용도, 카테고리, 품목) — 큰지출_TOP20 시트 열 순서
        items.append((a, mon, dep, use, cat, r.get('품목','')))
    items.sort(key=lambda x: x[0], reverse=True)
    return {'부서월':dict(dm),'카테고리월':dict(cm),'용도월':dict(um),
            '부서용도':dict(du),'부서카테고리':dict(dc),'월합':dict(mo),
            'top20':items[:20],'총계':total}

def _sibling(mod_file):
    spec = importlib.util.spec_from_file_location(mod_file[:-3], Path(__file__).with_name(mod_file))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def apply_purchase_record(cfg, rows, run_claude, when_ymd) -> dict:
    """붙여넣기 행들 → 분류 → 데몬 전용 누적 구매로그에 append(사람 양식 미접촉).
    반환 {'appended','records'(로그행),'warns','error'}."""
    pstore = _sibling('purchase_store.py')
    log_path = Path(cfg['folder']) / cfg.get('purchase_log', '비품 구매기록_자동.xlsx')
    if pstore.is_locked(log_path):
        return {'appended': 0, 'records': [], 'warns': [], 'error': 'locked'}
    cache_path = Path(cfg['classify_cache']).expanduser()
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    except Exception:
        cache = {}
    names = [(r.get('품명') or '').strip() for r in rows if (r.get('품명') or '').strip()]
    cmap, cache2 = classify_with_cache(names, cache, run_claude, cfg.get('guide_text',''))
    recs, warns = build_records(rows, cmap, when_ymd,
                                cfg.get('known_depts', []), cfg.get('dept_aliases', {}))
    yyyymm = when_ymd[:7].replace('-', '')
    month_label = f'{int(yyyymm[4:6])}월'
    # 블록ID seq: 그 달 기존 로그 건수에서 이어짐(재실행/중복 추적용, 고유)
    existing = pstore.read_purchase_log(log_path)
    seq = sum(1 for r in existing if str(r.get('월', '')).strip() == month_label) + 1
    log_rows = []
    for rec in recs:
        log_rows.append({'월': month_label, '일자': rec['일자'], '부서': rec['부서'],
                         '용도': rec['용도'], '카테고리': rec['카테고리'], '품목': rec['품목'],
                         '수량': rec['수량'], '단가': rec['단가'], '금액': rec['금액'],
                         '블록ID': f'{yyyymm}#{seq}'})
        seq += 1
    n = pstore.append_purchase_log(log_path, log_rows)
    if cache2 != cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache2, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'appended': n, 'records': log_rows, 'warns': warns, 'error': None}


def _yymmdd(ymd):
    return ymd[2:].replace('-', '')          # 2026-08-06 -> 260806


def merge_month_into_analysis(cfg, yyyymm, when_ymd, dry_run=True) -> dict:
    """최신 분석 로드 → 미반영월 판정 → 구매로그에서 해당 월 읽기 → 통합원본 변환 →
    dry_run이면 제안만, 아니면 새 버전 파일 저장(원본 불변). 결정론적 core(LLM 미사용).
    반환 {'status':'proposed'|'merged'|'nothing'|'locked','month','rows','out','summary'}."""
    pstore = _sibling('purchase_store.py')
    folder = Path(cfg['folder'])
    month_label = f'{int(yyyymm[4:6])}월'
    nothing = {'status': 'nothing', 'month': month_label, 'rows': 0, 'out': None, 'summary': {}}
    analysis, ver = pstore.latest_analysis(folder, cfg['analysis_prefix'])
    if not analysis:
        return nothing
    integ = pstore.read_integrated(analysis)
    if not unreflected_months(integ, month_label):
        return nothing                       # 이미 반영된 월
    log_path = folder / cfg.get('purchase_log', '비품 구매기록_자동.xlsx')
    if not log_path.exists():
        return nothing                       # 구매로그 없음
    if pstore.is_locked(analysis) or pstore.is_locked(log_path):
        return {'status': 'locked', 'month': month_label, 'rows': 0, 'out': None, 'summary': {}}
    month_rows = [r for r in pstore.read_purchase_log(log_path)
                  if str(r.get('월', '')).strip() == month_label]
    new_rows = log_rows_to_integrated(month_rows)
    if not new_rows:
        return nothing                       # 그 달 로그 없음
    all_rows = integ + new_rows
    pivots = compute_pivots(all_rows)
    summary = {'월합': pivots['월합'], '총계': pivots['총계'], '건수': len(new_rows)}
    if dry_run:
        return {'status': 'proposed', 'month': month_label, 'rows': len(new_rows),
                'out': None, 'summary': summary}
    nv = pstore.next_version(ver)
    out = folder / f"{_yymmdd(when_ymd)}-{cfg['dept_code']}-{cfg['analysis_prefix']}-v{nv[0]}.{nv[1]}.xlsx"
    pstore.write_merged_analysis(analysis, out, new_rows, pivots)
    return {'status': 'merged', 'month': month_label, 'rows': len(new_rows),
            'out': str(out), 'summary': summary}

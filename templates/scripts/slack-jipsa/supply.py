"""비품관리 코어 + 오케스트레이터.

순수 코어(IO 없음): parse_record, normalize_name, resolve_item, process_events.
오케스트레이터(Task 6/7): 슬랙 리스트 reader·LLM resolver·poster 주입.
재고 수량은 정수 가감으로 결정적. LLM은 이름 매칭에만(별칭 캐시).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _field(rec: dict, column_id: str) -> dict | None:
    for f in rec.get('fields', []):
        if f.get('column_id') == column_id:
            return f
    return None


def parse_record(rec: dict, cols: dict, status_done_option: str) -> dict:
    """슬랙 리스트 레코드 → {record_id, raw_item, qty, done}."""
    fi = _field(rec, cols['item']) or {}
    raw_item = (fi.get('text') or fi.get('value') or '').strip()
    fq = _field(rec, cols['qty']) or {}
    qv = fq.get('number')
    if isinstance(qv, list):
        qv = qv[0] if qv else None
    try:
        qty = int(float(qv))
    except (TypeError, ValueError):
        qty = 0
    fs = _field(rec, cols['status']) or {}
    sel = fs.get('select')
    if isinstance(sel, list):
        sel = sel[0] if sel else None
    return {'record_id': rec.get('id', ''), 'raw_item': raw_item,
            'qty': qty, 'done': sel == status_done_option}


def normalize_name(s: str) -> str:
    """매칭 키용 정규화: 좌우공백 제거 + 소문자 + 내부 공백 1칸."""
    return re.sub(r'\s+', ' ', (s or '').strip()).lower()


def _now_kst() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M')


def process_events(events: list[dict], stock: dict, aliases: dict, state: dict,
                   resolver, dry_run: bool = False, default_min: int = 1,
                   auto: str = 'high') -> dict:
    """수령완료 이벤트 → 재고 차감(결정적). 순수: 원본 미변경, 변경분을 반환.

    반환: {stock, ledger, aliases, alerts, pending, state, alias_adds}
      - dry_run: 작업 사본에 계산만, 원본 stock/state 그대로 반환 + '(dry-run)' alerts.
      - baseline_done=False: 현재 done 이벤트를 차감 없이 counted 로만 등록.
    """
    import copy
    work = copy.deepcopy(stock)            # 작업 사본(dry-run이면 버림)
    aliases_w = dict(aliases)
    counted = set(state.get('counted', []))
    baseline_done = bool(state.get('baseline_done'))
    ledger: list[dict] = []
    alerts: list[str] = []
    pending: list[dict] = []
    alias_adds: dict = {}
    done_events = [e for e in events if e.get('done')]

    def result(stock_out, alerts_out, state_out):
        return {'stock': stock_out, 'ledger': ledger, 'aliases': aliases_w,
                'alerts': alerts_out, 'pending': pending, 'state': state_out,
                'alias_adds': alias_adds}

    # baseline: 최초 1회 — 기존 수령완료를 차감 없이 처리됨 등록
    if not baseline_done:
        for e in done_events:
            counted.add(e['record_id'])
        if dry_run:
            return result(stock, [f'(dry-run) baseline {len(done_events)}건 등록 예정'], state)
        return result(work, [f'baseline: 기존 수령완료 {len(done_events)}건 등록(차감 없음)'],
                      {'counted': sorted(counted), 'baseline_done': True})

    known = sorted(work.keys())
    for e in done_events:
        rid, raw, qty = e['record_id'], e['raw_item'], int(e.get('qty') or 0)
        if rid in counted:
            continue
        if qty <= 0:
            alerts.append(f'⚠️ 수량 0/없음 — 스킵: {raw} (record {rid[:8]})')
            continue
        r = resolve_item(raw, known, aliases_w, resolver, auto=auto)
        if r['status'] == 'pending':
            pending.append({'record_id': rid, 'raw_item': raw, 'qty': qty,
                            'suggest': r.get('canonical', ''), 'alias_norm': r['alias_norm']})
            alerts.append(f'❓ 품목 확인 필요: "{raw}" → 제안 "{r.get("canonical") or "?"}" (확인 전 미차감)')
            continue
        canonical = r['canonical']
        if r.get('confidence') != 'cached' and r['alias_norm'] not in aliases_w:
            aliases_w[r['alias_norm']] = canonical
            alias_adds[r['alias_norm']] = {'canonical': canonical, '출처': 'list',
                '확신도': r.get('confidence', ''), '결정방식': 'auto', '결정시각': _now_kst()}
        row = work.get(canonical)
        if row is None:
            row = {'품목': canonical, '카테고리': r.get('category', ''), '현재수량': 0,
                   '최소수량': default_min, '단위': '', '비고': ''}
            work[canonical] = row
            alerts.append(f'🆕 재고표에 없던 품목 "{canonical}" 추가(등록 확인 필요)')
        after = int(row['현재수량']) - qty
        row['현재수량'] = after
        ledger.append({'일시': _now_kst(), '유형': '출고', 'canonical품목': canonical,
                       '원문품목': raw, '수량': qty, '처리후잔여': after,
                       '신청자/발주처': '', '출처키': rid})
        counted.add(rid)
        alerts.append(f'✅ {canonical} {qty} 지급(수령완료) — 잔여 {after}')
        if after < int(row.get('최소수량', 0)):
            alerts.append(f'⚠️ 저재고: {canonical} {after} (최소 {row["최소수량"]})')

    if dry_run:
        return result(stock, ['(dry-run) ' + a for a in alerts], state)
    return result(work, alerts, {'counted': sorted(counted), 'baseline_done': True})


def resolve_item(raw_item: str, known: list[str], aliases: dict,
                 resolver, auto: str = 'high') -> dict:
    """원문 품목명 → canonical 매칭.

    1) 별칭 캐시 적중 → 즉시 resolved(LLM 미호출)
    2) 미스 → resolver(LLM) 호출. confidence==auto면 resolved(+캐시키), 아니면 pending
    3) resolver 예외 → pending(추측 금지)

    반환: {status:'resolved'|'pending', canonical, category, confidence, alias_norm}
    """
    norm = normalize_name(raw_item)
    if norm in aliases:
        return {'status': 'resolved', 'canonical': aliases[norm],
                'category': '', 'confidence': 'cached', 'alias_norm': norm}
    try:
        sug = resolver(norm, known) or {}
    except Exception:
        sug = {}
    canonical = (sug.get('canonical') or '').strip()
    confidence = sug.get('confidence', 'low')
    if canonical and confidence == auto:
        return {'status': 'resolved', 'canonical': canonical,
                'category': sug.get('category', ''), 'confidence': confidence,
                'alias_norm': norm}
    return {'status': 'pending', 'canonical': canonical, 'category': sug.get('category', ''),
            'confidence': confidence, 'alias_norm': norm}


def fetch_list_records(web, list_id: str) -> list[dict]:
    """slackLists.items.list 전체 레코드(커서 페이징)."""
    items, cursor = [], None
    while True:
        params = {'list_id': list_id, 'limit': 100}
        if cursor:
            params['cursor'] = cursor
        r = web.api_call('slackLists.items.list', params=params).data
        if not r.get('ok'):
            break
        items.extend(r.get('items', []))
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
    return items


def resolve_batch_llm(names_norm: list, known: list, run_claude) -> dict:
    """여러 정규화 품목명을 *한 번의 LLM 호출* 로 매칭. {norm: {canonical,category,confidence}}.

    입고등록처럼 신규 품목이 많을 때 품목당 호출(N번)을 1번으로 줄인다.
    """
    if not names_norm:
        return {}
    numbered = '\n'.join(f'{i + 1}. {n}' for i, n in enumerate(names_norm))
    prompt = (
        "여러 사무비품 품목명을 표준 품목으로 매칭하라. 각 입력에 대해:\n"
        "1) '기존 품목 목록'에 *확실히 같은 물건*이 있으면 그 이름을 canonical 로 쓰고 confidence=high.\n"
        "2) 없으면 입력명을 규격(16온스/180ml/대·소·용량) 유지해 거의 그대로 신규 표준명으로 쓰고 "
        "confidence=high (기존에 없는 신규 품목이어도 품명이 멀쩡하면 high 로 등록).\n"
        "3) confidence=low 는 품명이 불완전하거나 오타·깨짐으로 무슨 물건인지 판단 불가할 때만 써라.\n"
        "★규격이 다르면 절대 합치지 마라(예: '16온스 종이컵'≠'종이컵 180ml'). 너무 짧게 일반화하지 마라. "
        "반드시 JSON 배열만, 입력 순서대로 출력: "
        '[{"in":"입력명","canonical":"표준명",'
        '"category":"사무용품|다과·음료|청소·위생|IT·전자|비품·가구|기타","confidence":"high|low"}].\n\n'
        f"기존 품목 목록: {known}\n입력 품목들:\n{numbered}")
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
        if isinstance(d, dict) and d.get('in') and d.get('canonical'):
            res[normalize_name(str(d['in']))] = {
                'canonical': str(d['canonical']).strip(),
                'category': d.get('category', ''),
                'confidence': d.get('confidence', 'low')}
    return res


def make_llm_resolver(run_claude):
    """run_claude(prompt)->str(JSON) 를 받아 resolver(raw,known)->dict 생성."""
    def resolver(raw_norm: str, known: list[str]) -> dict:
        prompt = (
            "너는 사무비품 품목명을 표준 품목으로 매칭하는 분류기다. "
            "'신규 품목명'이 '기존 품목 목록' 중 하나와 *확실히 같은 물건*이면 그 이름을(confidence high), "
            "아니면 입력 품목명을 거의 그대로(규격·용량·사이즈 유지) 표준명으로 써라(confidence high). "
            "★규격이 다르면 절대 합치지 마라: '16온스 종이컵'과 '종이컵 180ml'는 다른 품목, "
            "'포스트잇 대'와 '포스트잇 소'도 다른 품목이다. 너무 짧게 일반화(예: 그냥 '종이컵')하지 마라. "
            "반드시 JSON만 출력: "
            '{"canonical":"표준품목명","category":"사무용품|다과·음료|청소·위생|IT·전자|비품·가구|기타",'
            '"confidence":"high|low"}.\n\n'
            f"기존 품목 목록: {known}\n신규 품목명: {raw_norm}")
        txt = (run_claude(prompt) or '').strip()
        m = re.search(r'\{.*\}', txt, re.S)
        return json.loads(m.group(0)) if m else {}
    return resolver


def sync_once(web, cfg: dict, run_claude, post, dry_run: bool = False) -> dict:
    """1회 동기화: 리스트 읽기→파싱→process_events→xlsx/state 반영→알림."""
    import supply_store as store          # 지연 import (순수코어 테스트 로딩 보호)
    folder = Path(cfg['folder'])
    stock_path = folder / cfg['stock_xlsx']
    ledger_path = folder / cfg['ledger_xlsx']
    state_path = Path.home() / '.claude/scripts/slack-jipsa/supply_state.json'

    if not dry_run and (store.is_locked(stock_path) or store.is_locked(ledger_path)):
        post('⚠️ 비품 엑셀이 열려 있어 이번 동기화를 건너뜁니다(다음 주기 재시도).')
        return {'skipped': 'locked'}

    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception:
            state = {}
    state.setdefault('counted', [])
    state.setdefault('baseline_done', False)

    stock = store.read_stock(stock_path)
    aliases = store.read_aliases(stock_path)
    records = fetch_list_records(web, cfg['list_id'])
    events = [parse_record(r, cfg['cols'], cfg['status_done_option']) for r in records]
    resolver = make_llm_resolver(run_claude)
    res = process_events(events, stock, aliases, state, resolver,
                         dry_run=dry_run, default_min=cfg.get('default_min_qty', 1),
                         auto=cfg.get('match_confidence_auto', 'high'))

    for a in res['alerts']:
        post(a)
    if dry_run:
        return res

    if res.get('alias_adds'):
        full = store.read_aliases_full(stock_path)   # 기존 메타 보존
        full.update(res['alias_adds'])
        store.write_aliases(stock_path, full)
    if res['ledger'] or res['stock'] != stock:
        store.write_stock(stock_path, res['stock'])
    if res['ledger']:
        store.append_ledger(ledger_path, res['ledger'])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(res['state'], ensure_ascii=False, indent=2),
                          encoding='utf-8')
    return res


# ── 입고 (P2): 구매표 붙여넣기 → 재고 가산 ───────────────────────────
def parse_purchase_table(text: str) -> list[dict]:
    """붙여넣은 구매표 텍스트 → [{품명, 수량, 금액, 부서}]. 탭/파이프/2+공백 구분.

    형식 예: `no  품명  수량  금액  계좌  출금계좌  부서` (앞 no는 선택).
    품명 뒤 첫 정수 토큰 = 수량, 그 다음 정수 토큰 = 금액(없으면 0). 못 읽는 줄은 건너뛴다.
    """
    rows = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t')]
        elif '|' in line:
            parts = [p.strip() for p in line.split('|')]
        else:
            parts = [p for p in re.split(r'\s{2,}', line) if p.strip()]
        parts = [p for p in parts if p != '']
        if len(parts) < 2:
            continue
        if re.fullmatch(r'\d+', parts[0]):     # 앞 'no' 컬럼 제거
            parts = parts[1:]
        if len(parts) < 2:
            continue
        if '품명' in parts[0] or '수량' in parts[0]:   # 헤더 행 skip
            continue
        name = parts[0]
        qty = None
        for tok in parts[1:]:                  # 품명 뒤 첫 정수 = 수량
            t = tok.replace(',', '').strip()
            if re.fullmatch(r'\d+', t):
                qty = int(t)
                break
        if not name or qty is None:
            continue
        dept = parts[-1] if len(parts) >= 3 and not re.fullmatch(r'[\d,]+', parts[-1]) else ''
        # 수량 토큰 이후의 첫 '금액형' 토큰(쉼표 포함 큰 수) = 금액
        amount = 0
        seen_qty = False
        for tok in parts[1:]:
            t = tok.replace(',', '').strip()
            if not re.fullmatch(r'\d+', t):
                continue
            if not seen_qty:            # 첫 정수 = 수량(위에서 이미 사용)
                seen_qty = True
                continue
            amount = int(t)             # 그 다음 정수 = 금액
            break
        rows.append({'품명': name, '수량': qty, '금액': amount, '부서': dept})
    return rows


def parse_purchase_table_llm(text: str, run_claude) -> list[dict]:
    """칸 구분이 깨진(탭 없이 뭉개진) 붙여넣기 표를 LLM으로 추출 → [{품명,수량,금액,부서}].

    규칙 파서(parse_purchase_table)가 0건일 때 폴백. 금액(쉼표 큰 수)을 수량으로
    착각하지 않도록 컬럼 순서를 명시한다.
    """
    prompt = (
        "다음은 붙여넣은 '비품 구매 표'인데 칸 구분이 깨져 글자가 붙어 있다. "
        "각 구매 항목을 정확히 분리해 JSON 배열로만 출력하라. "
        "각 원소는 {\"품명\":문자열, \"수량\":정수, \"금액\":정수, \"부서\":문자열}. "
        "표의 컬럼 순서는 보통: 번호 · 품명 · 수량 · 금액 · 계좌/링크 · 출금계좌 · 부서. "
        "주의: *수량* 은 품명 바로 뒤의 작은 정수다. 쉼표가 들어간 큰 수(예: 179,250)는 "
        "금액이니 수량으로 쓰지 마라. '계좌/링크' 칸은 품명과 비슷하게 반복될 수 있다. "
        "행 끝은 부서명(전부서/인사총무/관리사무소/영업1본부 등)이다. JSON 외에는 아무것도 출력하지 마라.\n\n"
        f"표:\n{text}")
    out = (run_claude(prompt) or '').strip()
    m = re.search(r'\[.*\]', out, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    rows = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = (d.get('품명') or '').strip()
        try:
            qty = int(d.get('수량'))
        except (TypeError, ValueError):
            continue
        try:
            amount = int(str(d.get('금액', '0')).replace(',', ''))
        except (TypeError, ValueError):
            amount = 0
        if name and qty > 0:
            rows.append({'품명': name, '수량': qty, '금액': amount, '부서': (d.get('부서') or '').strip()})
    return rows


_PACK_RE = re.compile(r'[,\s]*([0-9][0-9,]*)\s*(개입|매입|개|매|장|입|박스|세트|팩|롤|병|캔|p)\s*$',
                      re.IGNORECASE)


def parse_pack(name: str) -> tuple:
    """품명 끝의 '팩당 개수' 추출 → (규격유지 정리명, 팩당개수).

    예) '16온스 종이컵, 1000개' → ('16온스 종이컵', 1000)
        '클립10박스' → ('클립', 10) · '핸드워시, 4L, 4개' → ('핸드워시, 4L', 4)
        '클래식 점보롤' → ('클래식 점보롤', 1)  (팩 표기 없음)
    """
    name = (name or '').strip()
    m = _PACK_RE.search(name)
    if not m:
        return name, 1
    try:
        size = int(m.group(1).replace(',', ''))
    except ValueError:
        return name, 1
    clean = name[:m.start()].rstrip(' ,·-').strip()
    if clean and size > 1:
        return clean, size
    return name, 1


def process_inbound(rows: list[dict], stock: dict, aliases: dict, resolver,
                    default_min: int = 1, auto: str = 'high') -> dict:
    """입고 행들 → 재고 가산(결정적). 순수: 원본 미변경, 변경분 반환.

    반환: {stock, ledger, aliases, alerts, pending, alias_adds}
    pending(애매)·수량0은 가산 안 함.
    """
    import copy
    work = copy.deepcopy(stock)
    aliases_w = dict(aliases)
    ledger, alerts, pending, alias_adds = [], [], [], {}
    for r in rows:
        raw = (r.get('품명') or '').strip()
        packs = int(r.get('수량') or 0)
        dept = r.get('부서', '')
        if not raw or packs <= 0:
            alerts.append(f'⚠️ 건너뜀(품명/수량 확인): "{raw}" x {r.get("수량")}')
            continue
        # 품명 끝의 '팩당 개수'를 떼어내 실제 입고 개수 = 팩수 × 팩당개수.
        clean, pack = parse_pack(raw)
        eff = packs * pack
        res = resolve_item(clean, sorted(work.keys()), aliases_w, resolver, auto=auto)
        if res['status'] == 'pending':
            pending.append({'raw_item': raw, 'qty': eff, 'suggest': res.get('canonical', '')})
            alerts.append(f'❓ 품목 확인 필요(입고 보류): "{clean}" → 제안 "{res.get("canonical") or "?"}"')
            continue
        canon = res['canonical']
        if res.get('confidence') != 'cached' and res['alias_norm'] not in aliases_w:
            aliases_w[res['alias_norm']] = canon
            alias_adds[res['alias_norm']] = {'canonical': canon, '출처': 'order',
                '확신도': res.get('confidence', ''), '결정방식': 'auto', '결정시각': _now_kst()}
        row = work.get(canon)
        if row is None:
            row = {'품목': canon, '카테고리': res.get('category', ''), '현재수량': 0,
                   '최소수량': default_min, '단위': ('개' if pack > 1 else ''), '비고': ''}
            work[canon] = row
            alerts.append(f'🆕 신규 품목 "{canon}" 추가')
        after = int(row['현재수량']) + eff
        row['현재수량'] = after
        ledger.append({'일시': _now_kst(), '유형': '입고', 'canonical품목': canon,
                       '원문품목': raw, '수량': eff, '처리후잔여': after,
                       '신청자/발주처': dept, '출처키': 'manual'})
        if pack > 1:
            alerts.append(f'📥 {canon} +{eff}개 입고 ({packs}×{pack}) — 현재 {after}')
        else:
            alerts.append(f'📥 {canon} +{eff} 입고 — 현재 {after}')
    return {'stock': work, 'ledger': ledger, 'aliases': aliases_w,
            'alerts': alerts, 'pending': pending, 'alias_adds': alias_adds}


def read_inventory(cfg: dict) -> dict:
    """재고 현황 {품목: row} 읽기(조회용)."""
    import supply_store as store
    return store.read_stock(Path(cfg['folder']) / cfg['stock_xlsx'])


def apply_inbound(cfg: dict, rows: list[dict], run_claude) -> dict:
    """입고 행들을 재고 xlsx에 반영(가산)하고 결과 반환. 엑셀 열림이면 skip.

    성능: 캐시에 없는 신규 품목명을 모아 *한 번의 LLM 호출* 로 일괄 매칭한 뒤
    별칭 캐시에 주입 → process_inbound는 품목당 LLM 호출 없이 결정적으로 처리.
    """
    import supply_store as store
    folder = Path(cfg['folder'])
    stock_path = folder / cfg['stock_xlsx']
    ledger_path = folder / cfg['ledger_xlsx']
    if store.is_locked(stock_path) or store.is_locked(ledger_path):
        return {'error': 'locked'}
    stock = store.read_stock(stock_path)
    aliases = store.read_aliases(stock_path)
    auto = cfg.get('match_confidence_auto', 'high')

    # 1) 캐시에 없는 신규 품목(규격정리명) 수집 → 1회 일괄 매칭
    known = sorted(stock.keys())
    misses, seen = [], set()
    for r in rows:
        clean, _ = parse_pack((r.get('품명') or '').strip())
        n = normalize_name(clean)
        if n and n not in aliases and n not in seen:
            seen.add(n)
            misses.append(n)
    batch = resolve_batch_llm(misses, known, run_claude) if misses else {}
    seed_meta, cat_by_canon = {}, {}
    for n, sug in batch.items():
        if sug.get('canonical') and sug.get('confidence') == auto:
            aliases[n] = sug['canonical']            # 캐시 주입(LLM 재호출 방지)
            seed_meta[n] = {'canonical': sug['canonical'], '출처': 'order',
                            '확신도': sug.get('confidence', ''), '결정방식': 'batch',
                            '결정시각': _now_kst()}
            cat_by_canon[sug['canonical']] = sug.get('category', '')

    # 2) 결정적 처리(캐시 적중분은 LLM 없음, 미해결은 pending)
    res = process_inbound(rows, stock, aliases, lambda raw, kn: {'confidence': 'low'},
                          default_min=cfg.get('default_min_qty', 1), auto=auto)

    # 3) 신규 품목 카테고리 보강(캐시 경로는 카테고리가 비므로 일괄 결과로 채움)
    for canon, row in res['stock'].items():
        if not row.get('카테고리') and cat_by_canon.get(canon):
            row['카테고리'] = cat_by_canon[canon]

    # 4) 별칭 영속화: 일괄 매칭 결과(seed_meta) + process가 만든 alias_adds
    adds = dict(seed_meta)
    adds.update(res.get('alias_adds') or {})
    if adds:
        full = store.read_aliases_full(stock_path)
        full.update(adds)
        store.write_aliases(stock_path, full)
    if res['ledger']:
        store.write_stock(stock_path, res['stock'])
        store.append_ledger(ledger_path, res['ledger'])
    return res


def process_adjust(raw_item: str, qty: int, mode: str, stock: dict, aliases: dict,
                   resolver, default_min: int = 1, auto: str = 'high') -> dict:
    """단건 수동 조정(순수). mode: '출고'(차감) | '실사'(현재고를 qty로 설정) | '입고'(가산).
    비치형 비품 소진 처리용. 원본 미변경, 변경분 반환."""
    import copy
    work = copy.deepcopy(stock)
    aliases_w = dict(aliases)
    alerts, ledger, pending, alias_adds = [], [], [], {}
    res = resolve_item(raw_item, sorted(work.keys()), aliases_w, resolver, auto=auto)
    if res['status'] == 'pending':
        return {'stock': work, 'ledger': [], 'aliases': aliases_w,
                'alerts': [f'❓ 품목 확인 필요: "{raw_item}" → [별칭] 시트에 등록 후 다시 시도'],
                'pending': [{'raw_item': raw_item, 'qty': qty}], 'alias_adds': {}}
    canon = res['canonical']
    if res.get('confidence') != 'cached' and res['alias_norm'] not in aliases_w:
        aliases_w[res['alias_norm']] = canon
        alias_adds[res['alias_norm']] = {'canonical': canon, '출처': 'manual',
            '확신도': res.get('confidence', ''), '결정방식': 'auto', '결정시각': _now_kst()}
    row = work.get(canon)
    if row is None:
        row = {'품목': canon, '카테고리': res.get('category', ''), '현재수량': 0,
               '최소수량': default_min, '단위': '', '비고': ''}
        work[canon] = row
        alerts.append(f'🆕 신규 품목 "{canon}" 추가')
    cur = int(row['현재수량'])
    after = qty if mode == '실사' else (cur - qty if mode == '출고' else cur + qty)
    row['현재수량'] = after
    ledger.append({'일시': _now_kst(), '유형': mode, 'canonical품목': canon,
                   '원문품목': raw_item, '수량': qty, '처리후잔여': after,
                   '신청자/발주처': '', '출처키': 'manual'})
    icon = {'실사': '📋', '출고': '📤', '입고': '📥'}.get(mode, '•')
    if mode == '실사':
        alerts.append(f'{icon} {canon} 실사 완료 — 현재 {after} (이전 {cur})')
    else:
        sign = '-' if mode == '출고' else '+'
        alerts.append(f'{icon} {canon} {sign}{qty} {mode} — 현재 {after}')
    if after < int(row.get('최소수량', 0) or 0):
        alerts.append(f'⚠️ 저재고: {canon} {after} (최소 {row["최소수량"]})')
    return {'stock': work, 'ledger': ledger, 'aliases': aliases_w,
            'alerts': alerts, 'pending': pending, 'alias_adds': alias_adds}


def apply_adjust(cfg: dict, raw_item: str, qty: int, mode: str, run_claude) -> dict:
    """단건 수동 조정(출고/실사/입고)을 재고 xlsx에 반영. 엑셀 열림이면 skip."""
    import supply_store as store
    folder = Path(cfg['folder'])
    stock_path = folder / cfg['stock_xlsx']
    ledger_path = folder / cfg['ledger_xlsx']
    if store.is_locked(stock_path) or store.is_locked(ledger_path):
        return {'error': 'locked'}
    stock = store.read_stock(stock_path)
    aliases = store.read_aliases(stock_path)
    res = process_adjust(raw_item, qty, mode, stock, aliases, make_llm_resolver(run_claude),
                         default_min=cfg.get('default_min_qty', 1),
                         auto=cfg.get('match_confidence_auto', 'high'))
    if res.get('alias_adds'):
        full = store.read_aliases_full(stock_path)
        full.update(res['alias_adds'])
        store.write_aliases(stock_path, full)
    if res['ledger']:
        store.write_stock(stock_path, res['stock'])
        store.append_ledger(ledger_path, res['ledger'])
    return res

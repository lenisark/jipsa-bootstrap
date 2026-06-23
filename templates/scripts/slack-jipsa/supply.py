"""비품관리 코어 + 오케스트레이터.

순수 코어(IO 없음): parse_record, normalize_name, resolve_item, process_events.
오케스트레이터(Task 6/7): 슬랙 리스트 reader·LLM resolver·poster 주입.
재고 수량은 정수 가감으로 결정적. LLM은 이름 매칭에만(별칭 캐시).
"""
from __future__ import annotations

import re


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

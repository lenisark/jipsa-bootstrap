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

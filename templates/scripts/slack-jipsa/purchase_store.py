"""비품 구매기록·분석 xlsx IO. supply_store의 잠금감지/원자저장 재활용."""
from __future__ import annotations
import shutil, importlib.util
from pathlib import Path
import openpyxl

_ss = importlib.util.spec_from_file_location('supply_store_io',
        Path(__file__).with_name('supply_store.py'))
supply_store = importlib.util.module_from_spec(_ss); _ss.loader.exec_module(supply_store)
is_locked = supply_store.is_locked
_atomic_save = supply_store._atomic_save

INPUT_HEADERS = ['일자','부서','용도','카테고리','품목','수량','단가','금액','발주처','재구매주기','비고']
INPUT_SHEET = '입력'
HEADER_ROW = 4          # 데이터는 5행부터

def month_filename(pattern: str, yyyymm: str) -> str:
    return pattern.replace('{yyyymm}', yyyymm)

def append_month_records(month_path, template_path, records, month_label) -> int:
    month_path, template_path = Path(month_path), Path(template_path)
    if is_locked(month_path):
        raise RuntimeError('locked')
    if not month_path.exists():
        shutil.copyfile(template_path, month_path)
        wb = openpyxl.load_workbook(month_path)
        ws = wb[INPUT_SHEET]
        title = ws.cell(row=1, column=1).value
        if title and '월' in str(title):
            import re as _re
            ws.cell(row=1, column=1).value = _re.sub(r'__?\d*_?월', month_label, str(title))
    else:
        wb = openpyxl.load_workbook(month_path)
        ws = wb[INPUT_SHEET]
    # 마지막 데이터 행 탐색(A열 기준, HEADER_ROW 이후)
    last = HEADER_ROW
    for r in range(HEADER_ROW + 1, ws.max_row + 1):
        if ws.cell(row=r, column=1).value not in (None, ''):
            last = r
    n = 0
    for rec in records:
        last += 1
        for ci, h in enumerate(INPUT_HEADERS, start=1):
            ws.cell(row=last, column=ci).value = rec.get(h, '')
        n += 1
    _atomic_save(wb, month_path)
    return n

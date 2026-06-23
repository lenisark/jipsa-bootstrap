# 비품관리 P1 (출고→정규화→재고) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 슬랙 비품신청 리스트에서 `수령완료`로 바뀐 신청을 감지해, 자유텍스트 품목명을 LLM으로 정규화(별칭 캐시)한 뒤 G드라이브 `비품재고_현황.xlsx` 재고를 결정적으로 차감하고 이력·알림을 남긴다. (dry-run 우선)

**Architecture:** 순수 코어(`supply.py`: 파싱·정규화·재고계산 — IO 없음, 주입식 resolver)와 IO 셸(`supply_store.py`: openpyxl 원자적 read/write + 엑셀 잠금감지)을 분리한다. daemon은 `supply.py`의 오케스트레이터를 폴링 스레드로 돌리며 슬랙 리스트 reader·LLM resolver·poster를 주입한다. 재고 수량은 정수 가감으로 100% 결정적이고, LLM은 *이름→canonical 매칭*에만 쓰여 별칭 시트에 캐시된다.

**Tech Stack:** Python 3 · `openpyxl`(xlsx) · `slack_sdk`(`slackLists.items.list`) · stdlib `unittest`/`json`/`os` · Claude Code(`_run_claude`, 매칭 resolver).

설계서: `docs/superpowers/specs/2026-06-23-bipum-inventory-design.md`

---

## File Structure

| 파일 | 책임 | 신규/변경 |
|---|---|---|
| `templates/scripts/slack-jipsa/supply_store.py` | openpyxl IO: 재고/별칭/이력 read·write, 원자적 저장, 엑셀 잠금감지 | 신규 |
| `templates/scripts/slack-jipsa/supply.py` | 순수 코어(레코드 파싱·이름 정규화·resolve·process_events) + 오케스트레이터(폴링·주입) | 신규 |
| `templates/scripts/slack-jipsa/daemon.py` | supply 폴링 스레드 시작 + reader/resolver/poster 주입(설정 게이트) | 변경 |
| `templates/scripts/slack-jipsa/supply.json.example` | 설정 예시 | 신규 |
| `tests/test_supply_store.py` | supply_store 단위테스트(임시 xlsx) | 신규 |
| `tests/test_supply.py` | supply 순수 코어 단위테스트(가짜 resolver/레코드) | 신규 |
| `modules/11-supply-inventory.md` | 셋업 가이드 | 신규 |

**핵심 자료구조 (전 태스크 공통 계약):**
- **재고 행 dict**: `{'품목': str, '카테고리': str, '현재수량': int, '최소수량': int, '단위': str, '비고': str}`
- **별칭 dict**: `{정규화원문(str): canonical품목(str)}` (시트엔 메타 포함 저장, 메모리 조회는 이 맵)
- **이벤트 dict**(파싱 결과): `{'record_id': str, 'raw_item': str, 'qty': int, 'done': bool}`
- **resolver 콜러블**: `resolve_fn(raw_norm: str, known: list[str]) -> dict` → `{'canonical': str, 'category': str, 'confidence': 'high'|'low'}` (LLM 또는 테스트용 가짜)
- **process 결과 dict**: `{'stock': dict[str,row], 'ledger': list[row], 'aliases': dict, 'alerts': list[str], 'pending': list[dict], 'state': dict}`

---

## Task 1: supply_store.py — 재고 시트 read/write (원자적 + 잠금감지)

**Files:**
- Create: `templates/scripts/slack-jipsa/supply_store.py`
- Test: `tests/test_supply_store.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_supply_store.py`

```python
import os, tempfile, unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/supply_store.py'


def load_mod():
    spec = importlib.util.spec_from_file_location('supply_store_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class StockIOTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()
        self.dir = Path(tempfile.mkdtemp())
        self.stock = self.dir / '비품재고_현황.xlsx'

    def test_write_then_read_roundtrip(self):
        rows = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품',
                           '현재수량': 5, '최소수량': 2, '단위': '박스', '비고': ''}}
        self.m.write_stock(self.stock, rows)
        back = self.m.read_stock(self.stock)
        self.assertEqual(back['A4용지']['현재수량'], 5)
        self.assertEqual(back['A4용지']['카테고리'], '사무용품')

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(self.m.read_stock(self.dir / 'nope.xlsx'), {})

    def test_atomic_write_no_temp_left(self):
        self.m.write_stock(self.stock, {'X': {'품목': 'X', '카테고리': '기타',
                           '현재수량': 1, '최소수량': 0, '단위': '', '비고': ''}})
        leftovers = [p.name for p in self.dir.glob('*.tmp')]
        self.assertEqual(leftovers, [])

    def test_excel_lock_detected(self):
        (self.dir / ('~$' + self.stock.name)).write_text('lock')
        self.assertTrue(self.m.is_locked(self.stock))
        self.assertFalse(self.m.is_locked(self.dir / '비품재고_현황.xlsx2'))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest tests.test_supply_store -v`
Expected: FAIL — `supply_store.py` 없음(ModuleNotFoundError/FileNotFoundError)

- [ ] **Step 3: 구현** — `templates/scripts/slack-jipsa/supply_store.py`

```python
"""비품관리 xlsx 저장소 — openpyxl read/write, 원자적 저장, 엑셀 잠금감지.

순수 IO만 담당(슬랙/LLM 모름). daemon 형제 모듈.
재고/별칭은 한 파일(비품재고_현황.xlsx)의 두 시트, 이력은 별도 파일.
"""
from __future__ import annotations

import os
from pathlib import Path

import openpyxl

STOCK_SHEET = '재고'
ALIAS_SHEET = '별칭'
STOCK_HEADERS = ['품목', '카테고리', '현재수량', '최소수량', '단위', '비고']
ALIAS_HEADERS = ['원문품목', 'canonical품목', '출처', '확신도', '결정방식', '결정시각']
LEDGER_HEADERS = ['일시', '유형', 'canonical품목', '원문품목', '수량', '처리후잔여',
                  '신청자/발주처', '출처키']


def is_locked(path: Path) -> bool:
    """엑셀이 파일을 열고 있으면 같은 폴더에 '~$이름' 잠금파일이 생긴다."""
    p = Path(path)
    return (p.parent / ('~$' + p.name)).exists()


def _atomic_save(wb, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    wb.save(tmp)
    os.replace(tmp, path)


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def read_stock(path: Path) -> dict:
    """재고 시트 → {품목: row dict}. 파일 없으면 {}."""
    path = Path(path)
    if not path.exists():
        return {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if STOCK_SHEET not in wb.sheetnames:
        wb.close(); return {}
    ws = wb[STOCK_SHEET]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    out = {}
    for r in rows[1:]:                      # 첫 행 헤더
        if not r or not r[0]:
            continue
        d = {h: (r[i] if i < len(r) else None) for i, h in enumerate(STOCK_HEADERS)}
        d['현재수량'] = _to_int(d['현재수량'])
        d['최소수량'] = _to_int(d['최소수량'])
        d['품목'] = str(d['품목'])
        d['카테고리'] = d['카테고리'] or ''
        d['단위'] = d['단위'] or ''
        d['비고'] = d['비고'] or ''
        out[d['품목']] = d
    return out


def write_stock(path: Path, rows: dict) -> None:
    """{품목: row} 를 재고 시트로 원자적 저장. 별칭 시트가 있으면 보존."""
    path = Path(path)
    if path.exists():
        wb = openpyxl.load_workbook(path)
        if STOCK_SHEET in wb.sheetnames:
            del wb[STOCK_SHEET]
        ws = wb.create_sheet(STOCK_SHEET, 0)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = STOCK_SHEET
    ws.append(STOCK_HEADERS)
    for item in sorted(rows):
        d = rows[item]
        ws.append([d.get(h, '') for h in STOCK_HEADERS])
    _atomic_save(wb, path)
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest tests.test_supply_store -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply_store.py tests/test_supply_store.py
git commit -m "feat(bipum): 재고 xlsx read/write(원자적)+엑셀 잠금감지"
```

---

## Task 2: supply_store.py — 별칭 read/write + 이력 append

**Files:**
- Modify: `templates/scripts/slack-jipsa/supply_store.py`
- Test: `tests/test_supply_store.py`

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_supply_store.py` 의 `StockIOTest` 에 메서드 추가

```python
    def test_alias_roundtrip(self):
        self.m.write_stock(self.stock, {})           # 파일 생성(재고 시트)
        self.m.write_aliases(self.stock, {
            '복사용지 a4': {'canonical': 'A4용지', '출처': 'list',
                          '확신도': 'high', '결정방식': 'auto', '결정시각': '2026-06-23'}})
        amap = self.m.read_aliases(self.stock)
        self.assertEqual(amap['복사용지 a4'], 'A4용지')   # 조회맵은 원문→canonical

    def test_ledger_append(self):
        ledger = self.dir / '비품_입출고이력.xlsx'
        self.m.append_ledger(ledger, [
            {'일시': '2026-06-23 10:00', '유형': '출고', 'canonical품목': 'A4용지',
             '원문품목': '복사용지 A4', '수량': 3, '처리후잔여': 2,
             '신청자/발주처': '홍길동', '출처키': 'Rec1'}])
        self.m.append_ledger(ledger, [
            {'일시': '2026-06-23 11:00', '유형': '입고', 'canonical품목': 'A4용지',
             '원문품목': 'A4용지', '수량': 10, '처리후잔여': 12,
             '신청자/발주처': '쿠팡', '출처키': 'batch1#1'}])
        rows = self.m.read_ledger(ledger)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['유형'], '출고')
        self.assertEqual(rows[1]['수량'], 10)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest tests.test_supply_store -v`
Expected: FAIL — `write_aliases`/`read_aliases`/`append_ledger`/`read_ledger` 없음(AttributeError)

- [ ] **Step 3: 구현** — `supply_store.py` 끝에 추가

```python
def read_aliases(path: Path) -> dict:
    """별칭 시트 → {원문품목: canonical품목}. 파일/시트 없으면 {}."""
    path = Path(path)
    if not path.exists():
        return {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if ALIAS_SHEET not in wb.sheetnames:
        wb.close(); return {}
    ws = wb[ALIAS_SHEET]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    out = {}
    for r in rows[1:]:
        if r and r[0] and r[1]:
            out[str(r[0])] = str(r[1])
    return out


def write_aliases(path: Path, aliases: dict) -> None:
    """{원문: {canonical,출처,확신도,결정방식,결정시각}} 를 별칭 시트로 저장(재고 시트 보존)."""
    path = Path(path)
    if path.exists():
        wb = openpyxl.load_workbook(path)
    else:
        wb = openpyxl.Workbook(); wb.active.title = STOCK_SHEET; wb.active.append(STOCK_HEADERS)
    if ALIAS_SHEET in wb.sheetnames:
        del wb[ALIAS_SHEET]
    ws = wb.create_sheet(ALIAS_SHEET)
    ws.append(ALIAS_HEADERS)
    for raw in sorted(aliases):
        a = aliases[raw]
        ws.append([raw, a.get('canonical', ''), a.get('출처', ''),
                   a.get('확신도', ''), a.get('결정방식', ''), a.get('결정시각', '')])
    _atomic_save(wb, path)


def read_ledger(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    out = []
    for r in rows[1:]:
        if not r or all(c is None for c in r):
            continue
        out.append({h: (r[i] if i < len(r) else None) for i, h in enumerate(LEDGER_HEADERS)})
    return out


def append_ledger(path: Path, entries: list[dict]) -> None:
    """이력 행들을 append(원자적). 파일 없으면 헤더 생성."""
    path = Path(path)
    if path.exists():
        wb = openpyxl.load_workbook(path)
        ws = wb.active
    else:
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = '이력'; ws.append(LEDGER_HEADERS)
    for e in entries:
        ws.append([e.get(h, '') for h in LEDGER_HEADERS])
    _atomic_save(wb, path)
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest tests.test_supply_store -v`
Expected: PASS (6 tests)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply_store.py tests/test_supply_store.py
git commit -m "feat(bipum): 별칭 시트 + 입출고 이력 read/append"
```

---

## Task 3: supply.py — 리스트 레코드 파싱 (순수)

**Files:**
- Create: `templates/scripts/slack-jipsa/supply.py`
- Test: `tests/test_supply.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_supply.py`

```python
import unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/supply.py'


def load():
    spec = importlib.util.spec_from_file_location('supply_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


COLS = {'item': 'Col_I', 'qty': 'Col_Q', 'status': 'Col_S'}
DONE = 'OptDONE'


def rec(rid, item, qty, status_opt):
    return {'id': rid, 'fields': [
        {'column_id': 'Col_I', 'text': item, 'value': item},
        {'column_id': 'Col_Q', 'number': [qty]},
        {'column_id': 'Col_S', 'select': [status_opt]},
    ]}


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_parse_record_done(self):
        e = self.m.parse_record(rec('R1', 'A4용지', 3, 'OptDONE'), COLS, DONE)
        self.assertEqual(e, {'record_id': 'R1', 'raw_item': 'A4용지', 'qty': 3, 'done': True})

    def test_parse_record_not_done(self):
        e = self.m.parse_record(rec('R2', '볼펜', 5, 'OptETC'), COLS, DONE)
        self.assertFalse(e['done'])

    def test_parse_record_missing_qty_zero(self):
        r = {'id': 'R3', 'fields': [{'column_id': 'Col_I', 'text': 'X', 'value': 'X'},
                                    {'column_id': 'Col_S', 'select': ['OptDONE']}]}
        e = self.m.parse_record(r, COLS, DONE)
        self.assertEqual(e['qty'], 0)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: FAIL — `supply.py`/`parse_record` 없음

- [ ] **Step 3: 구현** — `templates/scripts/slack-jipsa/supply.py` (Task 3 부분)

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.py tests/test_supply.py
git commit -m "feat(bipum): 슬랙 리스트 레코드 파싱(순수)"
```

---

## Task 4: supply.py — 이름 정규화 + resolve (별칭 캐시 + 주입 resolver)

**Files:**
- Modify: `templates/scripts/slack-jipsa/supply.py`
- Test: `tests/test_supply.py`

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_supply.py` 에 클래스 추가

```python
class ResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_normalize(self):
        n = self.m.normalize_name
        self.assertEqual(n('  A4 용지 '), 'a4 용지')      # trim+소문자+공백압축
        self.assertEqual(n('A4용지'), 'a4용지')

    def test_resolve_uses_alias_cache_no_llm(self):
        calls = []
        def resolver(raw, known): calls.append(raw); return {'canonical': 'X', 'category': '기타', 'confidence': 'high'}
        aliases = {'a4용지': 'A4용지'}
        r = self.m.resolve_item('A4용지', ['A4용지'], aliases, resolver, auto='high')
        self.assertEqual(r['canonical'], 'A4용지')
        self.assertEqual(r['status'], 'resolved')
        self.assertEqual(calls, [])                       # 캐시 적중 → LLM 미호출

    def test_resolve_high_confidence_auto(self):
        def resolver(raw, known): return {'canonical': 'A4용지', 'category': '사무용품', 'confidence': 'high'}
        r = self.m.resolve_item('복사용지 A4', ['A4용지'], {}, resolver, auto='high')
        self.assertEqual(r['status'], 'resolved')
        self.assertEqual(r['canonical'], 'A4용지')
        self.assertEqual(r['alias_norm'], '복사용지 a4')     # 캐시에 기록될 키

    def test_resolve_low_confidence_pending(self):
        def resolver(raw, known): return {'canonical': 'A4용지', 'category': '사무용품', 'confidence': 'low'}
        r = self.m.resolve_item('머시기 종이', ['A4용지'], {}, resolver, auto='high')
        self.assertEqual(r['status'], 'pending')           # 사람 확인 필요

    def test_resolve_llm_fail_pending(self):
        def resolver(raw, known): raise RuntimeError('llm down')
        r = self.m.resolve_item('무엇', [], {}, resolver, auto='high')
        self.assertEqual(r['status'], 'pending')           # 실패 시 추측 금지
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: FAIL — `normalize_name`/`resolve_item` 없음

- [ ] **Step 3: 구현** — `supply.py` 에 추가

```python
def normalize_name(s: str) -> str:
    """매칭 키용 정규화: 좌우공백 제거 + 소문자 + 내부 공백 1칸."""
    return re.sub(r'\s+', ' ', (s or '').strip()).lower()


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
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: PASS (정규화/캐시/high-auto/low-pending/실패-pending)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.py tests/test_supply.py
git commit -m "feat(bipum): 이름 정규화 + resolve(별칭 캐시/주입 resolver/pending)"
```

---

## Task 5: supply.py — process_events (baseline·차감·이력·저재고·dry-run·멱등)

**Files:**
- Modify: `templates/scripts/slack-jipsa/supply.py`
- Test: `tests/test_supply.py`

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_supply.py` 에 클래스 추가

```python
def ev(rid, item, qty, done=True):
    return {'record_id': rid, 'raw_item': item, 'qty': qty, 'done': done}


def hi_resolver(canonical, category='기타'):
    def r(raw, known): return {'canonical': canonical, 'category': category, 'confidence': 'high'}
    return r


class ProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def base_state(self):
        return {'counted': [], 'baseline_done': False}

    def test_baseline_marks_without_decrement(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, self.base_state(),
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 10)   # baseline: 차감 없음
        self.assertIn('R1', res['state']['counted'])
        self.assertTrue(res['state']['baseline_done'])

    def test_decrement_after_baseline(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, st,
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 7)
        self.assertEqual(len(res['ledger']), 1)
        self.assertEqual(res['ledger'][0]['처리후잔여'], 7)

    def test_idempotent_counted(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '', '현재수량': 7,
                            '최소수량': 2, '단위': '', '비고': ''}}
        st = {'counted': ['R1'], 'baseline_done': True}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, st,
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 7)     # 이미 처리됨 → 무차감

    def test_not_done_skipped(self):
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', 'A4용지', 3, done=False)], {}, {}, st,
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['ledger'], [])
        self.assertNotIn('R1', res['state']['counted'])

    def test_new_item_created_and_alert(self):
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', '새품목', 2)], {}, {}, st,
                                    hi_resolver('새품목', '기타'), dry_run=False)
        self.assertIn('새품목', res['stock'])
        self.assertEqual(res['stock']['새품목']['현재수량'], -2)    # 0에서 차감
        self.assertTrue(any('새품목' in a and ('신규' in a or '재고' in a) for a in res['alerts']))

    def test_low_stock_alert(self):
        stock = {'볼펜': {'품목': '볼펜', '카테고리': '사무용품', '현재수량': 3,
                          '최소수량': 5, '단위': '', '비고': ''}}
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', '볼펜', 1)], stock, {}, st,
                                    hi_resolver('볼펜'), dry_run=False)
        self.assertEqual(res['stock']['볼펜']['현재수량'], 2)
        self.assertTrue(any('저재고' in a for a in res['alerts']))

    def test_qty_zero_skipped_with_alert(self):
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', 'A4용지', 0)], {}, {}, st,
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['ledger'], [])
        self.assertTrue(any('수량' in a for a in res['alerts']))

    def test_dry_run_no_state_change(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, st,
                                    hi_resolver('A4용지'), dry_run=True)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 10)    # dry-run: 미변경
        self.assertNotIn('R1', res['state']['counted'])
        self.assertTrue(any('A4용지' in a for a in res['alerts']))  # 예상 결과는 리포트

    def test_pending_when_unresolved(self):
        def low(raw, known): return {'canonical': '', 'category': '', 'confidence': 'low'}
        st = {'counted': [], 'baseline_done': True}
        res = self.m.process_events([ev('R1', '애매품목', 2)], {}, {}, st, low, dry_run=False)
        self.assertEqual(len(res['pending']), 1)
        self.assertEqual(res['ledger'], [])
        self.assertNotIn('R1', res['state']['counted'])              # 확인 전 미차감
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: FAIL — `process_events` 없음

- [ ] **Step 3: 구현** — `supply.py` 에 추가

> dry-run은 **작업 사본(work)** 에 계산하고 원본 `stock`/`state`를 그대로 반환한다(실반영 없음). 실가동은 work/새 state를 반환. `result()` 헬퍼로 일관 반환.

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest tests.test_supply -v`
Expected: PASS (baseline/decrement/idempotent/not-done/new-item/low-stock/qty0/dry-run/pending 전부)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.py tests/test_supply.py
git commit -m "feat(bipum): process_events — baseline/차감/이력/저재고/신규/dry-run/멱등/pending"
```

---

## Task 6: supply.py — 오케스트레이터(리스트 reader + LLM resolver + 1회 동기화)

**Files:**
- Modify: `templates/scripts/slack-jipsa/supply.py`

이 태스크는 슬랙/엑셀/LLM IO를 묶는다. 단위테스트 대신 **dry-run 스모크**로 검증한다(주입식이라 가짜로도 호출 가능).

- [ ] **Step 1: 구현** — `supply.py` 에 추가

```python
import json
from pathlib import Path

import supply_store as store


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


def make_llm_resolver(run_claude):
    """run_claude(prompt)->str(JSON) 를 받아 resolver(raw,known)->dict 생성."""
    def resolver(raw_norm: str, known: list[str]) -> dict:
        prompt = (
            "너는 사무비품 품목명을 표준 품목으로 매칭하는 분류기다. "
            "아래 '신규 품목명'이 '기존 품목 목록' 중 하나와 같은 물건이면 그 이름을, "
            "아니면 새 표준명을 제안하라. 반드시 JSON만 출력: "
            '{"canonical":"표준품목명","category":"사무용품|다과·음료|청소·위생|IT·전자|비품·가구|기타",'
            '"confidence":"high|low"}. 확실할 때만 high.\n\n'
            f"기존 품목 목록: {known}\n신규 품목명: {raw_norm}")
        txt = (run_claude(prompt) or '').strip()
        m = re.search(r'\{.*\}', txt, re.S)
        return json.loads(m.group(0)) if m else {}
    return resolver


def sync_once(web, cfg: dict, run_claude, post, dry_run: bool = False) -> dict:
    """1회 동기화: 리스트 읽기→파싱→process_events→xlsx/state 반영→알림."""
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
    state.setdefault('counted', []); state.setdefault('baseline_done', False)

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

    # 별칭 추가분 시트 반영
    if res.get('alias_adds'):
        merged = store.read_aliases(stock_path)  # {raw:canonical}만 → 메타까지 다시 쓰려면 alias_adds 사용
        full = {k: {'canonical': v} for k, v in merged.items()}
        full.update(res['alias_adds'])
        store.write_aliases(stock_path, full)
    if res['ledger'] or res['stock'] != stock:
        store.write_stock(stock_path, res['stock'])
    if res['ledger']:
        store.append_ledger(ledger_path, res['ledger'])
    state_path.write_text(json.dumps(res['state'], ensure_ascii=False, indent=2),
                          encoding='utf-8')
    return res
```

- [ ] **Step 2: dry-run 스모크 검증** — 실제 리스트를 읽어 차감 없이 매칭/계산 결과만 출력. 가짜 resolver/poster로:

Run:
```bash
PYTHONIOENCODING=utf-8 python - <<'PY'
import importlib.util, sys
from pathlib import Path
base=Path.home()/'.claude/scripts/slack-jipsa'; sys.path.insert(0,str(base))
spec=importlib.util.spec_from_file_location('supply', base/'supply.py')  # 배포 후 경로(스모크는 배포 단계에서)
# 레포 단계 스모크는 parse_record + process_events 만으로 충분(Task3~5 테스트가 커버).
print('orchestrator 구문 점검은 py_compile 로 대체')
PY
python -m py_compile templates/scripts/slack-jipsa/supply.py
```
Expected: `py_compile` 통과(구문). 실제 리스트 dry-run은 Task 7 배포 후 라이브에서.

- [ ] **Step 3: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.py
git commit -m "feat(bipum): 오케스트레이터 — 리스트 reader/LLM resolver/sync_once(dry-run)"
```

---

## Task 7: daemon.py — supply 폴링 스레드 + 주입 배선

**Files:**
- Modify: `templates/scripts/slack-jipsa/daemon.py`

- [ ] **Step 1: supply 모듈 로드(방어적)** — `import tasks as tsk` 블록 아래에 추가

```python
# 비품관리(jipsa supply). 설정 없으면 비활성.
try:
    import supply as sply
except Exception:
    sply = None
```

- [ ] **Step 2: supply 설정 로더 + 폴링 스레드** — `_gate_sweeper` 근처에 추가

```python
SUPPLY_CONFIG_FILE = Path.home() / '.claude/scripts/slack-jipsa/supply.json'


def _load_supply_cfg() -> dict | None:
    if not SUPPLY_CONFIG_FILE.exists():
        return None
    try:
        return json.loads(SUPPLY_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log(f'supply.json 파싱 실패: {e}')
        return None


def _supply_poll_loop() -> None:
    if sply is None:
        return
    cfg = _load_supply_cfg()
    if not cfg:
        log('supply.json 없음 — 비품관리 비활성')
        return
    notify = cfg.get('notify_channel', '')
    dry = bool(cfg.get('dry_run', True))   # 기본 dry-run(안전)

    def post(msg: str) -> None:
        if notify:
            try:
                web.chat_postMessage(channel=notify, text=msg, mrkdwn=True)
            except Exception as e:
                log(f'  supply post fail: {e}')

    def run_claude_for_match(prompt: str) -> str:
        # 매칭용 1회성 claude 호출(새 세션, 도구 없음 — 순수 분류)
        notools = ['Bash', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit',
                   'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'Task']
        try:
            r = _run_claude(prompt, str(uuid.uuid4()), True, 120, 'sonnet',
                            cfg.get('cwd'), [], notools)
            return r.stdout or '' if r.returncode == 0 else ''
        except Exception:
            return ''

    log(f'비품관리 폴링 시작 (every {cfg.get("poll_min",5)}분, dry_run={dry})')
    while True:
        try:
            sply.sync_once(web, cfg, run_claude_for_match, post, dry_run=dry)
        except Exception as e:
            log(f'  supply sync err: {e}')
        time.sleep(max(60, int(cfg.get('poll_min', 5)) * 60))
```

- [ ] **Step 3: main 에서 스레드 시작** — 게이트 sweeper 스레드 시작 줄 아래에 추가

```python
    threading.Thread(target=_supply_poll_loop, daemon=True).start()
```

- [ ] **Step 4: 구문 검증**

Run: `python -m py_compile templates/scripts/slack-jipsa/daemon.py`
Expected: 통과. (supply.json 없으면 비활성이라 기존 동작 무변.)

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/daemon.py
git commit -m "feat(bipum): daemon supply 폴링 스레드(설정 게이트, 기본 dry-run)"
```

---

## Task 8: 설정 예시 + 모듈 문서 + 의존성

**Files:**
- Create: `templates/scripts/slack-jipsa/supply.json.example`, `modules/11-supply-inventory.md`
- Modify: `SKILL.md`(의존성 openpyxl, 모듈 11), `README.md`(기능 행)

- [ ] **Step 1: supply.json.example 작성**

```json
{
  "list_id": "F0AGQLRGQKC",
  "poll_min": 5,
  "dry_run": true,
  "notify_channel": "C0AGPKSSR8A",
  "cwd": "~/.claude/scripts/slack-team",
  "folder": "G:/공유 드라이브/인사총무팀_일반/05_총무 영역/비품·고정비",
  "stock_xlsx": "비품재고_현황.xlsx",
  "ledger_xlsx": "비품_입출고이력.xlsx",
  "cols": { "item": "Col0AG9D5TQTZ", "qty": "Col0AGUEUTS7N",
            "status": "Col0AGJF67S83", "dept": "Col0AGU94KCHJ",
            "amount": "Col0AGUEZ0XQC", "requester": "Col0AGMED767P" },
  "status_done_option": "OptXGCU61KL",
  "categories": ["사무용품", "다과·음료", "청소·위생", "IT·전자", "비품·가구", "기타"],
  "managers": ["U0ANN3GJRDE"],
  "default_min_qty": 1,
  "match_confidence_auto": "high"
}
```

- [ ] **Step 2: 모듈 11 작성** — `modules/11-supply-inventory.md`

내용(설계서 §1·§6·§11 요약): 무엇/흐름, 사전준비(`lists:read`·`openpyxl`·폴더·초기재고 0), `supply.json` 작성, **dry_run:true로 먼저 1주기 관찰**(슬랙에 "(dry-run) …" 예상 결과만 뜸) → 검증되면 `dry_run:false`, baseline 1회(기존 수령완료 차감 없이 등록), 품목 확인(pending) 흐름, 트러블슈팅(엑셀 열림/권한/품목 불일치). 기존 모듈 톤 따름.

- [ ] **Step 3: SKILL/README 반영**
  - SKILL.md 의존성: `pip install slack_sdk holidays openpyxl` 로 `openpyxl` 추가. 검증코드 목록에 `supply.py`/`supply_store.py`, .tmpl/설정에 `supply.json.example`, 모듈 진행에 11 추가.
  - README 기능표에 `11. 비품관리 (2.0)` 행 + 폴더구조에 supply 파일 추가.

- [ ] **Step 4: 검증**

Run: `python -c "import json; json.load(open('templates/scripts/slack-jipsa/supply.json.example', encoding='utf-8')); print('OK')"`
Expected: `OK`

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.json.example modules/11-supply-inventory.md SKILL.md README.md
git commit -m "docs(bipum): supply.json 예시 + 모듈 11 + SKILL/README(openpyxl) 반영"
```

---

## Self-Review

**1. 스펙 커버리지(설계서 대비):**
- 출고(수령완료 차감) → Task5 process_events ✅
- LLM 정규화+별칭 캐시+pending → Task4 resolve_item, Task6 make_llm_resolver ✅
- 결정적 수량계산 → Task5(정수 가감, LLM 무관) ✅
- G드라이브 .xlsx 재고/별칭/이력 + 원자적/잠금감지 → Task1·2 ✅
- baseline/멱등/저재고/신규/수량0/dry-run → Task5 테스트 ✅
- 폴링/주입/알림 → Task6·7 ✅
- 설정·문서·openpyxl → Task8 ✅
- **P2(입고등록 붙여넣기)·P3(리포트)는 범위 외** — 별도 계획. (이 계획 산출물은 출고+재고만으로 동작·검증 가능.)

**2. 플레이스홀더 스캔:** 없음. Task5는 dry-run을 작업 사본(work)으로 처리해 원본 보존(단일 Step 3, `result()` 헬퍼). Task6 Step2 스모크는 py_compile로 대체(실 리스트 dry-run은 Task7 배포 후 라이브) — 명시됨.

**3. 타입 일관성:** `read_stock/write_stock/read_aliases/write_aliases/append_ledger/read_ledger`(supply_store) 시그니처가 Task1·2·6에서 동일. `parse_record(rec,cols,status_done_option)`·`normalize_name`·`resolve_item(raw,known,aliases,resolver,auto)`·`process_events(events,stock,aliases,state,resolver,dry_run,default_min,auto)` 시그니처가 Task3·4·5·6에서 일치. 재고 행 dict 키(품목/카테고리/현재수량/최소수량/단위/비고)·이벤트 dict 키(record_id/raw_item/qty/done)·결과 dict 키(stock/ledger/aliases/alerts/pending/state) 전 태스크 통일.

**주의(구현 시):** Task5 `process_events`는 dry-run 시 **작업 사본(work)** 에만 계산하고 원본 `stock`/`state`를 반환한다(테스트 `test_dry_run_no_state_change`가 강제). 실가동은 `work`/새 state 반환.

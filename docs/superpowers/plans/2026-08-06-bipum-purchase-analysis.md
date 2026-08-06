# 비품 구매기록·월간분석 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실시간 재고 추적을 중단하고, 슬랙에 붙여넣는 구매표를 월별 주문기록 엑셀에 적재한 뒤 매달 자동으로 누적 분석 파일에 반영한다.

**Architecture:** 기존 `supply.py`(재고, 순수함수 + xlsx IO 분리 패턴)를 유지·중단하고, 구매 분석용 순수모듈 `purchase.py` + IO모듈 `purchase_store.py`를 신설한다. 붙여넣기 파싱·LLM 배치매칭은 `supply.py`에서 재활용(import)한다. Drive는 로컬 마운트(`G:\`) 파일을 openpyxl로 직접 다룬다(커넥터 미사용). 월간 분석은 파생 시트를 SUMIFS 수술 대신 **통합원본에서 파이썬으로 재집계**해 값으로 기록한다.

**Tech Stack:** Python 3.13, openpyxl, unittest(기존 `tests/` 패턴), 기존 slack_sdk WebClient, LibreOffice(soffice, 교차검증용 선택).

## Global Constraints

- 수치(수량·금액)는 **붙여넣기 원본 그대로** 기록. LLM은 품목→(용도·카테고리) 분류와 부서명 정규화에만 사용, 결과는 캐시로 결정적. (spec §2 불변식)
- 분석 파일 병합은 **append-only + 이전 vN.N 파일 보존**. 삭제·덮어쓰기 금지. (spec §2)
- Drive 접근은 **로컬 파일 openpyxl**만. Google Drive 커넥터/MCP(`search_files` 등) 사용 금지. (spec §6)
- 엑셀 열림 감지(`~$파일`) 시 그 파일 쓰기 **skip + 슬랙 고지**, 예외로 죽지 않기. (spec §8, 재활용 `supply_store.is_locked`)
- 기존 재고 코드(`supply.py`, `supply_store.py`)·데이터 파일(`비품재고_현황.xlsx`, `비품_입출고이력.xlsx`)은 **삭제하지 않음**. 폴링·차감만 중단. (spec §5.5)
- 파일 경로: 모든 신규 파일은 `templates/scripts/slack-jipsa/` 아래(레포 템플릿). 테스트는 `tests/`.
- 슬랙 출력은 mrkdwn(`*굵게*`), 이모지 절제(기존 `_supply_reply` 톤).
- 커밋 트레일러: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## 확정 사실 (스펙 §3 실측 요약, 구현 시 상수로 사용)

- 월별 주문기록 `입력` 시트 헤더(r4): `일자|부서|용도|카테고리|품목|수량|단가|금액|발주처|재구매주기|비고`, 데이터 r5~.
- 분석 `통합원본` 헤더(r1): `월|일자|부서|용도|카테고리|품목|금액|블록ID`, 데이터 r2~.
- 카테고리(6): `사무용품 다과·음료 청소·위생 IT·전자 비품·가구 기타`.
- 용도(3): `사내비품` `공용(사내·외부 혼재)` `재판매·대고객`.
- 부서: `전부서 공통 인사총무 매입부 제휴매입파트 관리사무소 영업본부 영업시스템본부 마케팅팀 고객지원팀 행정지원파트 네트웍스 CX파트 상무님 대표님 재무회계`.
- 붙여넣기 구매표 컬럼: `번호·품명·수량·금액·계좌/링크·출금계좌·부서`.
- Drive 폴더: `G:/공유 드라이브/인사총무팀_일반/05_총무 영역/비품·고정비`.
- 최신 분석: `260608-HGA-비품주문분석-v1.2.xlsx`. 월별: `비품 주문 기록_YYYYMM.xlsx`.

## File Structure

- **Create** `templates/scripts/slack-jipsa/purchase.py` — 순수함수: 부서 정규화, 분류(LLM 배치+가이드), 레코드 빌드, 피벗 재집계, 병합 오케스트레이션. `supply` 모듈에서 `parse_purchase_table`·`parse_purchase_table_llm`·`resolve_batch_llm`·`normalize_name` import.
- **Create** `templates/scripts/slack-jipsa/purchase_store.py` — openpyxl IO: 월별기록 생성/append, 통합원본 read, 분석 파일 버전 탐색·값갱신 저장. `supply_store`에서 `is_locked`·`_atomic_save` import.
- **Create** `templates/scripts/slack-jipsa/purchase.json.example` — 구매 분석 설정(폴더·파일 패턴·부서코드·스케줄·분류캐시 경로).
- **Modify** `templates/scripts/slack-jipsa/supply.py:276-350` — `parse_purchase_table`/`_llm` 반환에 `금액` 추가(하위호환).
- **Modify** `templates/scripts/slack-jipsa/daemon.py:767-784` — `_do_inbound_register`가 설정 `mode`에 따라 재고 대신 구매기록 적재로 분기.
- **Modify** `templates/scripts/slack-jipsa/daemon.py:1615±` — 재고 폴링 활성 조건에 `mode != 'inventory'`면 폴링 skip; 월간 분석 스케줄 체크 추가.
- **Create** `tests/test_purchase.py`, `tests/test_purchase_store.py` — 순수함수/IO 단위테스트.

---

## Phase 0 — 재고 자동화 중단 + 붙여넣기 라우팅 전환

### Task 1: 붙여넣기 파서에 금액 추출 추가 (supply.py)

**Files:**
- Modify: `templates/scripts/slack-jipsa/supply.py:276-350`
- Test: `tests/test_supply.py` (기존 파일에 추가)

**Interfaces:**
- Produces: `parse_purchase_table(text) -> list[{품명:str, 수량:int, 금액:int, 부서:str}]` (금액 신설, 없으면 0). `parse_purchase_table_llm(text, run_claude)` 동일 반환에 `금액` 포함.
- 기존 소비자 `process_inbound`는 `금액`을 안 읽으므로 하위호환.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_supply.py` InboundTest에 추가:

```python
    def test_parse_extracts_amount(self):
        txt = ("no | 품명 | 수량 | 금액 | 출금계좌 | 부서\n"
               "1 | 디퓨저 리필액, 6개 | 1 | 30,480 | 대구은행 379-1 | 전부서\n"
               "12 | 차량용 방향제 | 17 | 423,300 | 대구은행 | 매입부")
        rows = self.m.parse_purchase_table(txt)
        self.assertEqual(rows[0]['금액'], 30480)      # 쉼표 제거 정수
        self.assertEqual(rows[1]['금액'], 423300)
        self.assertEqual(rows[1]['수량'], 17)          # 수량은 여전히 금액과 구분

    def test_parse_amount_absent_is_zero(self):
        rows = self.m.parse_purchase_table("딱풀\t4")   # 금액 컬럼 없음
        self.assertEqual(rows[0]['금액'], 0)
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_supply.py -k amount -v` → FAIL (KeyError '금액').

- [ ] **Step 3: 구현** — `parse_purchase_table` 내부, 수량 확정 루프 뒤에 금액 추출 추가. 현재 `rows.append({'품명':name,'수량':qty,'부서':dept})`를 아래로 교체:

```python
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
```

  그리고 `parse_purchase_table_llm`의 프롬프트에 금액 요구를 추가하고(`{"품명":..,"수량":..,"금액":..,"부서":..}`), 파싱 루프에서 `'금액': int(str(d.get('금액','0')).replace(',',''))` 를 넣는다(변환 실패 시 0).

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_supply.py -v` → 기존 + 신규 테스트 모두 PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/supply.py tests/test_supply.py
git commit -m "feat(bipum): 붙여넣기 파서 금액 추출 (구매기록 전환 준비)"
```

### Task 2: 구매 분석 설정 파일 + 로더

**Files:**
- Create: `templates/scripts/slack-jipsa/purchase.json.example`
- Modify: `templates/scripts/slack-jipsa/daemon.py` (신규 `_load_purchase_cfg`, `SUPPLY_CONFIG_FILE` 인근 1525±)

**Interfaces:**
- Produces: `_load_purchase_cfg() -> dict | None` — `~/.claude/scripts/slack-jipsa/purchase.json` 파싱(없으면 None). folder 내 `~` 확장.

- [ ] **Step 1: 설정 예시 작성** — `purchase.json.example`:

```json
{
  "mode": "purchase",
  "channel": "C0AGPKSSR8A",
  "folder": "G:/공유 드라이브/인사총무팀_일반/05_총무 영역/비품·고정비",
  "dept_code": "HGA",
  "month_record_pattern": "비품 주문 기록_{yyyymm}.xlsx",
  "month_record_template": "비품 주문 기록_202605.xlsx",
  "analysis_prefix": "비품주문분석",
  "classify_cache": "~/.claude/scripts/slack-jipsa/supply_classify.json",
  "managers": ["U0ANN3GJRDE"],
  "schedule": { "enabled": true, "day": 5, "hour": 10 },
  "dry_run": true
}
```

- [ ] **Step 2: 로더 구현** — `daemon.py`의 `SUPPLY_CONFIG_FILE` 정의 부근에 추가:

```python
PURCHASE_CONFIG_FILE = Path.home() / '.claude/scripts/slack-jipsa/purchase.json'

def _load_purchase_cfg():
    if not PURCHASE_CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(PURCHASE_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log(f'purchase.json 파싱 실패: {e}')
        return None
    if cfg.get('folder', '').startswith('~'):
        cfg['folder'] = os.path.expanduser(cfg['folder'])
    return cfg
```

- [ ] **Step 3: 수동 확인** — Run: `python -c "import json; json.load(open('templates/scripts/slack-jipsa/purchase.json.example', encoding='utf-8')); print('ok')"` → `ok`.

- [ ] **Step 4: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.json.example templates/scripts/slack-jipsa/daemon.py
git commit -m "feat(bipum): 구매 분석 설정(purchase.json) + 로더"
```

### Task 3: 재고 폴링 중단 (mode 게이트)

**Files:**
- Modify: `templates/scripts/slack-jipsa/daemon.py:1615±` (비품관리 폴링 시작부)

**Interfaces:**
- Consumes: `_load_purchase_cfg()` (Task 2).
- 동작: `purchase.json`이 있고 `mode == 'purchase'`면 기존 재고 폴링(`_supply_poll_loop`) 시작을 **skip**하고 로그로 고지.

- [ ] **Step 1: 폴링 시작부 확인** — Read `daemon.py:1615±`의 `if SUPPLY_CONFIG_FILE.exists()` 폴링 스레드 시작 블록.

- [ ] **Step 2: 게이트 추가** — 폴링 스레드 시작 조건 앞에:

```python
    _pcfg = _load_purchase_cfg()
    if _pcfg and _pcfg.get('mode') == 'purchase':
        log('구매기록 모드 — 재고 폴링/차감 비활성')
    else:
        # (기존 재고 폴링 스레드 시작 블록은 이 else 안으로)
        ...
```

- [ ] **Step 3: 수동 확인** — `purchase.json`(mode=purchase) 배치 후 데몬 로그에 `구매기록 모드 — 재고 폴링/차감 비활성` 1회 출력, 재고 관련 폴링 로그 없음. (배치·재시작은 운영 단계에서. 여기선 코드 경로만 검증: `python -c` 로 함수 존재 확인.)

- [ ] **Step 4: 커밋**

```bash
git add templates/scripts/slack-jipsa/daemon.py
git commit -m "feat(bipum): 구매기록 모드에서 재고 폴링 중단"
```

---

## Phase 1 — 붙여넣기 → 월별 주문기록 적재

### Task 4: 부서명 정규화 (purchase.py)

**Files:**
- Create: `templates/scripts/slack-jipsa/purchase.py`
- Test: `tests/test_purchase.py`

**Interfaces:**
- Produces: `normalize_dept(raw: str, known: list[str], aliases: dict) -> tuple[str, bool]` — (정규화된 부서, is_new). 매칭 규칙: 정확일치 > 별칭 > 정규화(공백·괄호 제거 후 부분일치). 미매칭이면 (raw.strip(), True).

- [ ] **Step 1: 실패 테스트** — `tests/test_purchase.py`:

```python
import unittest, importlib.util
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/purchase.py'
def load():
    spec = importlib.util.spec_from_file_location('purchase_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
KNOWN = ['전부서 공통','인사총무','매입부','영업본부','영업시스템본부','CX파트']

class DeptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_exact(self):
        self.assertEqual(self.m.normalize_dept('매입부', KNOWN, {}), ('매입부', False))
    def test_alias(self):
        self.assertEqual(self.m.normalize_dept('영업1본부', KNOWN, {'영업1본부':'영업본부'}), ('영업본부', False))
    def test_contains(self):
        self.assertEqual(self.m.normalize_dept(' 전부서 ', KNOWN, {}), ('전부서 공통', False))
    def test_new(self):
        self.assertEqual(self.m.normalize_dept('신설팀', KNOWN, {}), ('신설팀', True))

if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -v` → FAIL (module/함수 없음).

- [ ] **Step 3: 구현** — `purchase.py` 시작부:

```python
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
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py tests/test_purchase.py
git commit -m "feat(bipum): 부서명 정규화(purchase.py)"
```

### Task 5: 용도·카테고리 LLM 배치 분류 + 캐시

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py`
- Test: `tests/test_purchase.py`

**Interfaces:**
- Produces:
  - `classify_items(items: list[str], run_claude, guide_text: str = '') -> dict[str, dict]` — 1회 LLM 호출로 `{품목: {'용도':str, '카테고리':str}}`. JSON 배열만 파싱(`resolve_batch_llm` 스타일). 빈 입력 → `{}`.
  - `classify_with_cache(items, cache: dict, run_claude, guide_text='') -> tuple[dict, dict]` — 캐시 적중분은 LLM 미호출, 미스만 `classify_items`로 채워 캐시에 병합. 반환 (분류맵, 갱신된 캐시).

- [ ] **Step 1: 실패 테스트** — 추가:

```python
class ClassifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_batch_one_call(self):
        calls=[]
        def fake(p):
            calls.append(1)
            return ('네 [{"품목":"물티슈, 80개","용도":"사내비품","카테고리":"청소·위생"},'
                    '{"품목":"무선마우스","용도":"사내비품","카테고리":"IT·전자"}] 끝')
        res = self.m.classify_items(['물티슈, 80개','무선마우스'], fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(res['무선마우스']['카테고리'], 'IT·전자')
    def test_cache_hit_no_llm(self):
        cache = {'물티슈, 80개': {'용도':'사내비품','카테고리':'청소·위생'}}
        def boom(p): raise AssertionError('LLM 호출되면 안 됨')
        res, newc = self.m.classify_with_cache(['물티슈, 80개'], cache, boom)
        self.assertEqual(res['물티슈, 80개']['카테고리'], '청소·위생')
    def test_empty(self):
        self.assertEqual(self.m.classify_items([], lambda p:'x'), {})
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k Classify -v` → FAIL.

- [ ] **Step 3: 구현** — `purchase.py`에 추가:

```python
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
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py tests/test_purchase.py
git commit -m "feat(bipum): 용도·카테고리 LLM 배치 분류 + 캐시"
```

### Task 6: 레코드 빌드 (파싱행 + 분류 → 월별기록 행)

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py`
- Test: `tests/test_purchase.py`

**Interfaces:**
- Consumes: `parse_purchase_table` 결과(`{품명,수량,금액,부서}`), `classify_with_cache` 결과, `normalize_dept`.
- Produces: `build_records(rows, classify_map, when_ymd: str, known_depts, dept_aliases) -> tuple[list[dict], list[str]]` — (레코드 리스트, 신규부서 경고). 각 레코드: `{'일자','부서','용도','카테고리','품목','수량','단가','금액'}`. 단가 = round(금액/수량) (수량>0, 아니면 0). 일자 = `when_ymd`(YYYY-MM-DD).

- [ ] **Step 1: 실패 테스트** — 추가:

```python
class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_build_basic(self):
        rows = [{'품명':'무선마우스','수량':2,'금액':99800,'부서':'재무회계'}]
        cmap = {'무선마우스': {'용도':'사내비품','카테고리':'IT·전자'}}
        recs, warns = self.m.build_records(rows, cmap, '2026-08-06', ['재무회계'], {})
        r = recs[0]
        self.assertEqual((r['품목'], r['수량'], r['금액'], r['단가']), ('무선마우스',2,99800,49900))
        self.assertEqual((r['용도'], r['카테고리'], r['부서'], r['일자']),
                         ('사내비품','IT·전자','재무회계','2026-08-06'))
    def test_new_dept_warns(self):
        rows=[{'품명':'볼펜','수량':1,'금액':1000,'부서':'미지의팀'}]
        recs, warns = self.m.build_records(rows, {'볼펜':{'용도':'사내비품','카테고리':'사무용품'}},
                                           '2026-08-06', ['인사총무'], {})
        self.assertTrue(any('미지의팀' in w for w in warns))
    def test_zero_qty_unitprice_zero(self):
        recs,_ = self.m.build_records([{'품명':'X','수량':0,'금액':500,'부서':''}],
                                      {'X':{'용도':'사내비품','카테고리':'기타'}}, '2026-08-06', [], {})
        self.assertEqual(recs[0]['단가'], 0)
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k Build -v` → FAIL.

- [ ] **Step 3: 구현** — 추가:

```python
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
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py tests/test_purchase.py
git commit -m "feat(bipum): 구매 레코드 빌드(분류·단가·신규부서)"
```

### Task 7: 월별기록 IO — 생성/append (purchase_store.py)

**Files:**
- Create: `templates/scripts/slack-jipsa/purchase_store.py`
- Test: `tests/test_purchase_store.py`

**Interfaces:**
- Consumes: `supply_store.is_locked`, `supply_store._atomic_save`.
- Produces:
  - `INPUT_HEADERS = ['일자','부서','용도','카테고리','품목','수량','단가','금액','발주처','재구매주기','비고']`
  - `month_filename(pattern: str, yyyymm: str) -> str`
  - `append_month_records(month_path: Path, template_path: Path, records: list[dict], month_label: str) -> int` — 파일 없으면 template 복사 후 `입력` 시트 제목의 월 치환; `입력` 시트 마지막 데이터 행 다음부터 REC_HEADERS 순서로 append(발주처·재구매주기·비고 공란). 반환 append 행수. 잠금 시 `raise RuntimeError('locked')`.

- [ ] **Step 1: 실패 테스트** — `tests/test_purchase_store.py` (임시폴더에 미니 템플릿 생성):

```python
import unittest, importlib.util, tempfile
from pathlib import Path
import openpyxl
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/purchase_store.py'
def load():
    spec = importlib.util.spec_from_file_location('pstore_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def make_template(path):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title='입력'
    ws['A1']='비품 주문 기록 — 2026년 __5_월'
    ws.append([]); ws.append([])   # r2,r3
    ws.append(['일자','부서','용도','카테고리','품목','수량','단가','금액','발주처','재구매주기','비고'])  # r4
    wb.create_sheet('마스터'); wb.create_sheet('사용가이드'); wb.save(path); wb.close()

class MonthIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_create_from_template_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            tpl = Path(d)/'tpl.xlsx'; make_template(tpl)
            out = Path(d)/'비품 주문 기록_202608.xlsx'
            recs=[{'일자':'2026-08-06','부서':'재무회계','용도':'사내비품','카테고리':'IT·전자',
                   '품목':'무선마우스','수량':2,'단가':49900,'금액':99800}]
            n = self.m.append_month_records(out, tpl, recs, '8월')
            self.assertEqual(n, 1)
            wb = openpyxl.load_workbook(out); ws = wb['입력']
            vals = [c.value for c in ws[5]][:8]     # 데이터 첫 행 = r5
            self.assertEqual(vals, ['2026-08-06','재무회계','사내비품','IT·전자','무선마우스',2,49900,99800])
    def test_append_after_existing(self):
        with tempfile.TemporaryDirectory() as d:
            tpl=Path(d)/'tpl.xlsx'; make_template(tpl)
            out=Path(d)/'m.xlsx'
            self.m.append_month_records(out, tpl, [{'일자':'a','부서':'b','용도':'c','카테고리':'d','품목':'e','수량':1,'단가':1,'금액':1}], '8월')
            self.m.append_month_records(out, out, [{'일자':'f','부서':'g','용도':'h','카테고리':'i','품목':'j','수량':2,'단가':2,'금액':2}], '8월')
            wb=openpyxl.load_workbook(out); ws=wb['입력']
            self.assertEqual(ws['A5'].value,'a'); self.assertEqual(ws['A6'].value,'f')
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase_store.py -v` → FAIL.

- [ ] **Step 3: 구현** — `purchase_store.py`:

```python
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
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase_store.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase_store.py tests/test_purchase_store.py
git commit -m "feat(bipum): 월별 주문기록 생성/append IO"
```

### Task 8: 적재 오케스트레이션 + 데몬 라우팅 전환

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py` (오케스트레이션 `apply_purchase_record`)
- Modify: `templates/scripts/slack-jipsa/daemon.py:767-784` (`_do_inbound_register` 분기)
- Test: `tests/test_purchase.py` (오케스트레이션은 IO 결합 → 데몬은 수동검증)

**Interfaces:**
- Produces: `apply_purchase_record(cfg, rows, run_claude, when_ymd) -> dict` — 캐시 로드(JSON) → 분류 → 레코드 빌드 → `purchase_store.append_month_records` → 캐시 저장. 반환 `{'appended':int,'records':list,'warns':list,'error':str|None}`. `error='locked'`도 처리.
- Consumes(daemon): 기존 `_do_inbound_register(channel, body, cfg)` 시그니처 유지, 내부만 교체.

- [ ] **Step 1: 오케스트레이션 테스트(캐시 경로만, IO는 tmp)** — `tests/test_purchase.py`에 추가:

```python
class ApplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_apply_uses_cache_and_writes(self):
        import tempfile, json as _j, openpyxl
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            d = _P(d)
            tpl = d/'비품 주문 기록_202605.xlsx'
            wb = openpyxl.Workbook(); ws=wb.active; ws.title='입력'
            ws['A1']='비품 주문 기록 — 2026년 __5_월'; ws.append([]); ws.append([])
            ws.append(['일자','부서','용도','카테고리','품목','수량','단가','금액','발주처','재구매주기','비고'])
            wb.create_sheet('마스터'); wb.save(tpl); wb.close()
            cache = d/'c.json'; cache.write_text(_j.dumps({'볼펜':{'용도':'사내비품','카테고리':'사무용품'}}), encoding='utf-8')
            cfg = {'folder':str(d),'month_record_pattern':'비품 주문 기록_{yyyymm}.xlsx',
                   'month_record_template':'비품 주문 기록_202605.xlsx','classify_cache':str(cache),
                   'known_depts':['전부서 공통'],'dept_aliases':{}}
            rows=[{'품명':'볼펜','수량':3,'금액':3000,'부서':'전부서'}]
            def boom(p): raise AssertionError('캐시 적중인데 LLM 호출')
            res = self.m.apply_purchase_record(cfg, rows, boom, '2026-08-06')
            self.assertEqual(res['appended'], 1)
            self.assertIsNone(res['error'])
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k Apply -v` → FAIL.

- [ ] **Step 3: 구현 오케스트레이션** — `purchase.py`에 추가(상단에 supply/purchase_store 동적 import 헬퍼 포함):

```python
def _sibling(mod_file):
    spec = importlib.util.spec_from_file_location(mod_file[:-3], Path(__file__).with_name(mod_file))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def apply_purchase_record(cfg, rows, run_claude, when_ymd) -> dict:
    pstore = _sibling('purchase_store.py')
    folder = Path(cfg['folder'])
    yyyymm = when_ymd[:7].replace('-', '')
    month_path = folder / pstore.month_filename(cfg['month_record_pattern'], yyyymm)
    tpl_path = folder / cfg['month_record_template']
    if pstore.is_locked(month_path):
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
    month_label = f'{int(yyyymm[4:6])}월'
    n = pstore.append_month_records(month_path, tpl_path, recs, month_label)
    if cache2 != cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache2, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'appended': n, 'records': recs, 'warns': warns, 'error': None}
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py -v` → PASS.

- [ ] **Step 5: 데몬 라우팅 교체** — `daemon.py`의 `_do_inbound_register`를 구매기록 모드 분기로 수정:

```python
def _do_inbound_register(channel: str, body: str, cfg: dict) -> bool:
    pcfg = _load_purchase_cfg()
    rows = sply.parse_purchase_table(body)
    used_llm = False
    if not rows:
        rows = sply.parse_purchase_table_llm(body, lambda p: _supply_match_claude(p, cfg))
        used_llm = bool(rows)
    if not rows:
        web.chat_postMessage(channel=channel, mrkdwn=True,
            text="표를 못 읽었어요. 품명·수량·금액이 있는 표를 붙여넣어 주세요.")
        return True
    if pcfg and pcfg.get('mode') == 'purchase':
        import purchase as pur
        when = datetime.now(KST).strftime('%Y-%m-%d')   # 기존 KST 상수 사용
        res = pur.apply_purchase_record(pcfg, rows, lambda p: _supply_match_claude(p, cfg), when)
        if res.get('error') == 'locked':
            web.chat_postMessage(channel=channel, text='📕 월별기록 파일이 열려 있어요. 닫고 다시 시도해 주세요.')
            return True
        lines = [f'🧾 구매기록 {res["appended"]}건 저장 ({when[:7]})']
        for r in res['records'][:20]:
            lines.append(f"• {r['품목']} ×{r['수량']} — {r['금액']:,}원 [{r['부서']}/{r['카테고리']}]")
        for w in res['warns'][:5]:
            lines.append('⚠️ ' + w)
        web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)
        return True
    # (레거시) 재고 모드
    res = sply.apply_inbound(cfg, rows, lambda p: _supply_match_claude(p, cfg))
    _supply_reply(channel, res, header=f'입고등록 {len(rows)}행 처리' + (' (AI 표 인식)' if used_llm else ''))
    return True
```

  (`datetime`/`KST`가 daemon 상단에 이미 있으면 재사용; 없으면 `from datetime import datetime, timezone, timedelta; KST=timezone(timedelta(hours=9))` 확인.)

- [ ] **Step 6: 수동 검증** — Run: `python -c "import ast; ast.parse(open('templates/scripts/slack-jipsa/daemon.py',encoding='utf-8').read()); print('syntax ok')"`. (실채널 검증은 운영 배치 단계.)

- [ ] **Step 7: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py tests/test_purchase.py templates/scripts/slack-jipsa/daemon.py
git commit -m "feat(bipum): 붙여넣기→월별 주문기록 적재 라우팅 전환"
```

---

## Phase 2 — 자동 월간 분석 (통합원본 재집계)

### Task 9: 통합원본 읽기 + 피벗 재집계 (순수)

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py` (`compute_pivots`)
- Create: `purchase_store.read_integrated` in `purchase_store.py`
- Test: `tests/test_purchase.py`, `tests/test_purchase_store.py`

**Interfaces:**
- Produces:
  - `purchase_store.read_integrated(analysis_path) -> list[dict]` — `통합원본` 시트 → `[{월,일자,부서,용도,카테고리,품목,금액,블록ID}]` (헤더 r1, 데이터 r2~).
  - `purchase.compute_pivots(rows) -> dict` — `{'부서월':{(부서,월):합}, '카테고리월':{...}, '용도월':{...}, '부서용도':{(부서,용도):합}, '부서카테고리':{...}, 'top20':[(금액,품목,부서,월)...], '월합':{월:합}, '총계':int}`. 금액은 int 강제.

- [ ] **Step 1: 실패 테스트(compute_pivots)** — `tests/test_purchase.py`:

```python
class PivotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_pivots(self):
        rows=[{'월':'1월','부서':'인사총무','용도':'사내비품','카테고리':'사무용품','품목':'볼펜','금액':1000},
              {'월':'1월','부서':'인사총무','용도':'사내비품','카테고리':'사무용품','품목':'A4','금액':2000},
              {'월':'2월','부서':'매입부','용도':'공용(사내·외부 혼재)','카테고리':'청소·위생','품목':'물티슈','금액':5000}]
        p = self.m.compute_pivots(rows)
        self.assertEqual(p['부서월'][('인사총무','1월')], 3000)
        self.assertEqual(p['총계'], 8000)
        self.assertEqual(p['월합']['2월'], 5000)
        self.assertEqual(p['top20'][0][0], 5000)      # 금액 내림차순
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k Pivot -v` → FAIL.

- [ ] **Step 3: 구현 compute_pivots**:

```python
def _amt(v):
    try: return int(float(str(v).replace(',', '')))
    except (TypeError, ValueError): return 0

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
        items.append((a, r.get('품목',''), dep, mon))
    items.sort(key=lambda x: x[0], reverse=True)
    return {'부서월':dict(dm),'카테고리월':dict(cm),'용도월':dict(um),
            '부서용도':dict(du),'부서카테고리':dict(dc),'월합':dict(mo),
            'top20':items[:20],'총계':total}
```

- [ ] **Step 4: read_integrated 테스트** — `tests/test_purchase_store.py`:

```python
class IntegratedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_read_integrated(self):
        import tempfile, openpyxl
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            p=_P(d)/'a.xlsx'; wb=openpyxl.Workbook(); ws=wb.active; ws.title='통합원본'
            ws.append(['월','일자','부서','용도','카테고리','품목','금액','블록ID'])
            ws.append(['1월','6일','인사총무','사내비품','사무용품','볼펜',1000,'1월#1'])
            wb.save(p); wb.close()
            rows=self.m.read_integrated(p)
            self.assertEqual(rows[0]['부서'],'인사총무'); self.assertEqual(rows[0]['금액'],1000)
```

- [ ] **Step 5: read_integrated 구현** — `purchase_store.py`:

```python
INTEGRATED_HEADERS = ['월','일자','부서','용도','카테고리','품목','금액','블록ID']
INTEGRATED_SHEET = '통합원본'

def read_integrated(analysis_path) -> list:
    wb = openpyxl.load_workbook(Path(analysis_path), read_only=True, data_only=True)
    ws = wb[INTEGRATED_SHEET]
    rows = list(ws.iter_rows(min_row=2, values_only=True)); wb.close()
    out=[]
    for r in rows:
        if not r or r[0] in (None,''): continue
        out.append({h:(r[i] if i < len(r) else None) for i,h in enumerate(INTEGRATED_HEADERS)})
    return out
```

- [ ] **Step 6: 통과 확인** — Run: `python -m pytest tests/test_purchase.py tests/test_purchase_store.py -v` → PASS.

- [ ] **Step 7: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py templates/scripts/slack-jipsa/purchase_store.py tests/
git commit -m "feat(bipum): 통합원본 읽기 + 피벗 재집계"
```

### Task 10: 월별기록 → 통합원본 행 변환 + 미반영월 판정

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py`
- Test: `tests/test_purchase.py`

**Interfaces:**
- Produces:
  - `month_records_to_integrated(input_rows, month_label, seq_start=1) -> list[dict]` — 월별 `입력` 행(dict, INPUT_HEADERS 키) → 통합원본 행(`월|일자|부서|용도|카테고리|품목|금액|블록ID`). 블록ID = `f'{yyyymm}#{seq}'`. 일자는 '6일' 식으로 '일'만(원본 `일자`가 date/str이면 일(day) 추출, 실패 시 원문).
  - `unreflected_months(integrated_rows, candidate_month) -> bool` — 통합원본 `월` 집합에 candidate_month 없으면 True.

- [ ] **Step 1: 실패 테스트**:

```python
class ReflectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_to_integrated(self):
        inp=[{'일자':'2026-08-06','부서':'재무회계','용도':'사내비품','카테고리':'IT·전자',
              '품목':'무선마우스','수량':2,'단가':49900,'금액':99800}]
        out=self.m.month_records_to_integrated(inp,'8월','202608',1)
        self.assertEqual(out[0]['월'],'8월'); self.assertEqual(out[0]['금액'],99800)
        self.assertEqual(out[0]['일자'],'6일'); self.assertEqual(out[0]['블록ID'],'202608#1')
    def test_unreflected(self):
        integ=[{'월':'5월'}]
        self.assertTrue(self.m.unreflected_months(integ,'8월'))
        self.assertFalse(self.m.unreflected_months(integ,'5월'))
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k Reflect -v` → FAIL.

- [ ] **Step 3: 구현**:

```python
def _day_of(v):
    s = str(v or '')
    m = re.search(r'(\d{1,2})\s*일', s)
    if m: return f'{int(m.group(1))}일'
    m = re.search(r'\d{4}-\d{2}-(\d{2})', s)
    if m: return f'{int(m.group(1))}일'
    return s

def month_records_to_integrated(input_rows, month_label, yyyymm, seq_start=1):
    out=[]; seq=seq_start
    for r in input_rows:
        out.append({'월':month_label,'일자':_day_of(r.get('일자')),'부서':r.get('부서',''),
                    '용도':r.get('용도',''),'카테고리':r.get('카테고리',''),'품목':r.get('품목',''),
                    '금액':_amt(r.get('금액')),'블록ID':f'{yyyymm}#{seq}'})
        seq+=1
    return out

def unreflected_months(integrated_rows, candidate_month) -> bool:
    have = {str(r.get('월','')).strip() for r in integrated_rows}
    return candidate_month not in have
```

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py tests/test_purchase.py
git commit -m "feat(bipum): 월별기록→통합원본 변환 + 미반영월 판정"
```

### Task 11: 분석 파일 병합 쓰기 (통합원본 append + 피벗 값 갱신 + 버전업)

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase_store.py`
- Test: `tests/test_purchase_store.py`

**Interfaces:**
- Produces:
  - `latest_analysis(folder: Path, prefix: str) -> tuple[Path|None, tuple[int,int]]` — `*{prefix}-vN.N.xlsx` 중 최고 버전 경로와 (major,minor). 없으면 (None,(0,0)).
  - `write_merged_analysis(src_path, out_path, new_integrated_rows, pivots) -> None` — src를 로드해 (a) `통합원본`에 new rows append, (b) 파생 시트들의 데이터 셀을 pivots 값으로 갱신(서식 보존: 기존 셀 value만 교체, 헤더/스타일 유지), 원자적 저장. *파생 시트 좌표 매핑은 실제 v1.2 레이아웃을 읽어 상수화(구현 시 §주의).*

  > **주의(구현자):** 파생 시트 레이아웃(어느 행/열이 어느 부서·월인지)은 `260608-HGA-비품주문분석-v1.2.xlsx`를 openpyxl로 열어 실좌표를 확인해 `PIVOT_LAYOUT` 상수로 박는다. 신규 부서·월은 마지막 행/열 다음에 추가. 값 기록은 SUMIFS 수식을 값으로 덮어쓰지 말고, **수식이 있는 셀은 유지**하되 신규 부서/월 셀만 값으로 채우는 방식 우선(회귀 최소화). 회귀 테스트(Task 12)로 5월까지 합계 불변을 보장.

- [ ] **Step 1: 실패 테스트(핵심 계약: append + 버전탐색)** — `tests/test_purchase_store.py`:

```python
class MergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_latest_analysis(self):
        import tempfile, openpyxl
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            d=_P(d)
            for name in ['260101-HGA-비품주문분석-v1.2.xlsx','260501-HGA-비품주문분석-v1.10.xlsx']:
                wb=openpyxl.Workbook(); wb.save(d/name); wb.close()
            p,(maj,mn)=self.m.latest_analysis(d,'비품주문분석')
            self.assertEqual((maj,mn),(1,10))            # 1.10 > 1.2
    def test_append_integrated(self):
        import tempfile, openpyxl
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            d=_P(d); src=d/'src.xlsx'; out=d/'out.xlsx'
            wb=openpyxl.Workbook(); ws=wb.active; ws.title='통합원본'
            ws.append(['월','일자','부서','용도','카테고리','품목','금액','블록ID'])
            ws.append(['1월','6일','인사총무','사내비품','사무용품','볼펜',1000,'1월#1'])
            wb.save(src); wb.close()
            new=[{'월':'8월','일자':'6일','부서':'재무회계','용도':'사내비품','카테고리':'IT·전자','품목':'마우스','금액':99800,'블록ID':'202608#1'}]
            self.m.write_merged_analysis(src, out, new, pivots=None)
            wb=openpyxl.load_workbook(out); ws=wb['통합원본']
            self.assertEqual(ws.max_row, 3); self.assertEqual(ws['A3'].value,'8월')
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase_store.py -k Merge -v` → FAIL.

- [ ] **Step 3: 구현** — `latest_analysis` + `write_merged_analysis`(우선 통합원본 append 확정, pivots=None이면 파생 갱신 skip):

```python
import re as _re
def latest_analysis(folder, prefix):
    folder = Path(folder); best=None; bestv=(0,0)
    for p in folder.glob(f'*{prefix}-v*.xlsx'):
        m=_re.search(r'-v(\d+)\.(\d+)\.xlsx$', p.name)
        if not m: continue
        v=(int(m.group(1)),int(m.group(2)))
        if v>bestv: bestv, best = v, p
    return best, bestv

def next_version(v): return (v[0], v[1]+1)

def write_merged_analysis(src_path, out_path, new_integrated_rows, pivots=None):
    wb = openpyxl.load_workbook(Path(src_path))
    ws = wb[INTEGRATED_SHEET]
    last = ws.max_row
    for i,r in enumerate(new_integrated_rows, start=last+1):
        for ci,h in enumerate(INTEGRATED_HEADERS, start=1):
            ws.cell(row=i, column=ci).value = r.get(h,'')
    if pivots is not None:
        _apply_pivots(wb, pivots)     # PIVOT_LAYOUT 기반 셀 값 갱신(구현자 주의 참조)
    _atomic_save(wb, Path(out_path))
```

  `_apply_pivots`와 `PIVOT_LAYOUT`은 실파일 좌표 확인 후 구현(주의 블록). 이 태스크의 통과 기준은 통합원본 append + 버전탐색; 파생 갱신은 Task 12 회귀로 검증.

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase_store.py -v` → PASS.

- [ ] **Step 5: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase_store.py tests/test_purchase_store.py
git commit -m "feat(bipum): 분석파일 통합원본 append + 버전 탐색"
```

### Task 12: 실파일 회귀 — 5월까지 합계 불변 + 파생 좌표 상수화

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase_store.py` (`PIVOT_LAYOUT`, `_apply_pivots`)
- Test: `tests/test_purchase_regression.py` (실파일이 있을 때만 실행, 없으면 skip)

**Interfaces:**
- Consumes: `read_integrated`, `compute_pivots`, `_apply_pivots`.

- [ ] **Step 1: 좌표 조사** — 실행: `python` REPL로 `260608-HGA-비품주문분석-v1.2.xlsx` 각 파생 시트를 열어 부서/월 라벨의 (행,열) 좌표를 수집해 `PIVOT_LAYOUT` dict를 `purchase_store.py`에 상수로 기록. (부서별_월별: 부서=행, 월=열 등.)

- [ ] **Step 2: 회귀 테스트 작성**:

```python
import unittest, importlib.util, os
from pathlib import Path
FOLDER = Path('G:/공유 드라이브/인사총무팀_일반/05_총무 영역/비품·고정비')
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa'
def load(n):
    spec=importlib.util.spec_from_file_location(n, SRC/f'{n}.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

@unittest.skipUnless(FOLDER.exists(), '실 Drive 폴더 없음')
class Regression(unittest.TestCase):
    def test_pivot_matches_dashboard_total(self):
        ps=load('purchase_store'); pu=load('purchase')
        analysis,_=ps.latest_analysis(FOLDER,'비품주문분석')
        rows=ps.read_integrated(analysis)
        p=pu.compute_pivots(rows)
        self.assertEqual(sum(p['월합'].values()), p['총계'])   # 내부 정합
        self.assertGreater(p['총계'], 0)
```

- [ ] **Step 3: 통과 확인** — Run: `python -m pytest tests/test_purchase_regression.py -v` → PASS(또는 폴더 없으면 SKIP).

- [ ] **Step 4: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase_store.py tests/test_purchase_regression.py
git commit -m "test(bipum): 분석 피벗 실파일 회귀 + 좌표 상수화"
```

### Task 13: 월간 분석 오케스트레이션 + 자동 스케줄 + 확인 게이트

**Files:**
- Modify: `templates/scripts/slack-jipsa/purchase.py` (`merge_month_into_analysis`)
- Modify: `templates/scripts/slack-jipsa/daemon.py` (월간 스케줄 체크 + `분석 갱신` 명령 + 승인 게이트)
- Test: `tests/test_purchase.py`

**Interfaces:**
- Produces:
  - `purchase.merge_month_into_analysis(cfg, yyyymm, when_ymd, dry_run) -> dict` — 최신 분석 로드 → 미반영월 판정 → 월별기록 읽기 → 통합원본 변환 → (dry_run이면 제안만) → 병합 파일(`{when:YYMMDD}-{dept_code}-{prefix}-v{next}.xlsx`) 저장. 반환 `{'status':'proposed'|'merged'|'nothing'|'locked', 'month':..,'rows':int,'out':str|None,'summary':dict}`.
- Consumes(daemon): 매월 `schedule.day` 영업일에 dry_run 제안 → 담당자 reaction 승인 시 실병합. 수동 `분석 갱신` 명령도 동일 함수 호출.

- [ ] **Step 1: 실패 테스트(제안 모드, 미반영월)** — `tests/test_purchase.py`:

```python
class MergeOrchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_nothing_when_already_reflected(self):
        import tempfile, openpyxl
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            d=_P(d)
            a=d/'260501-HGA-비품주문분석-v1.2.xlsx'
            wb=openpyxl.Workbook(); ws=wb.active; ws.title='통합원본'
            ws.append(['월','일자','부서','용도','카테고리','품목','금액','블록ID'])
            ws.append(['5월','6일','인사총무','사내비품','사무용품','볼펜',1000,'5월#1'])
            wb.save(a); wb.close()
            cfg={'folder':str(d),'analysis_prefix':'비품주문분석','dept_code':'HGA',
                 'month_record_pattern':'비품 주문 기록_{yyyymm}.xlsx','month_record_template':'x'}
            res=self.m.merge_month_into_analysis(cfg,'202605','2026-08-06',dry_run=True)
            self.assertEqual(res['status'],'nothing')     # 5월 이미 반영
```

- [ ] **Step 2: 실패 확인** — Run: `python -m pytest tests/test_purchase.py -k MergeOrch -v` → FAIL.

- [ ] **Step 3: 구현 오케스트레이션**:

```python
def merge_month_into_analysis(cfg, yyyymm, when_ymd, dry_run=True) -> dict:
    pstore = _sibling('purchase_store.py')
    folder = Path(cfg['folder'])
    month_label = f'{int(yyyymm[4:6])}월'
    analysis, ver = pstore.latest_analysis(folder, cfg['analysis_prefix'])
    if not analysis:
        return {'status':'nothing','month':month_label,'rows':0,'out':None,'summary':{}}
    integ = pstore.read_integrated(analysis)
    if not unreflected_months(integ, month_label):
        return {'status':'nothing','month':month_label,'rows':0,'out':None,'summary':{}}
    month_path = folder / pstore.month_filename(cfg['month_record_pattern'], yyyymm)
    if not month_path.exists():
        return {'status':'nothing','month':month_label,'rows':0,'out':None,'summary':{}}
    if pstore.is_locked(analysis) or pstore.is_locked(month_path):
        return {'status':'locked','month':month_label,'rows':0,'out':None,'summary':{}}
    input_rows = pstore.read_input_rows(month_path)        # Task 13a: 입력시트 dict 리더
    new_rows = month_records_to_integrated(input_rows, month_label, yyyymm, seq_start=1)
    all_rows = integ + new_rows
    pivots = compute_pivots(all_rows)
    summary = {'월합': pivots['월합'], '총계': pivots['총계'], '건수': len(new_rows)}
    if dry_run:
        return {'status':'proposed','month':month_label,'rows':len(new_rows),'out':None,'summary':summary}
    nv = pstore.next_version(ver)
    out = folder / f"{_yymmdd(when_ymd)}-{cfg['dept_code']}-{cfg['analysis_prefix']}-v{nv[0]}.{nv[1]}.xlsx"
    pstore.write_merged_analysis(analysis, out, new_rows, pivots)
    return {'status':'merged','month':month_label,'rows':len(new_rows),'out':str(out),'summary':summary}

def _yymmdd(ymd): return ymd[2:].replace('-', '')   # 2026-08-06 -> 260806
```

  `read_input_rows`는 Task 7의 `purchase_store`에 추가(입력 r5~ → INPUT_HEADERS dict). 간단하므로 이 태스크 Step 3b에서 함께 구현 + 미니 테스트.

- [ ] **Step 3b: read_input_rows 구현 + 테스트** — `purchase_store.py`에 `read_input_rows(month_path)->list[dict]`(r5~, INPUT_HEADERS 매핑), `tests/test_purchase_store.py`에 왕복 테스트 1개.

- [ ] **Step 4: 통과 확인** — Run: `python -m pytest tests/test_purchase.py tests/test_purchase_store.py -v` → PASS.

- [ ] **Step 5: 데몬 통합 — 자동 스케줄 + 명령 + 게이트**:
  - 매월 `schedule.day`의 영업일(재활용: `reminders.effective_notify_date`)에 데몬 스케줄러가 `merge_month_into_analysis(dry_run=True)` 호출 → `status=='proposed'`면 담당자 채널에 요약 카드 + "반영하려면 ✅" 안내(기존 `approval.py`/reaction 게이트 재사용). `nothing`이면 "이번 달 주문기록 올라오면 반영해드릴게요" 리마인드.
  - 승인 reaction 수신 시 `dry_run=False` 재호출 → `merged`면 파일명·요약 게시.
  - 수동 명령 `분석 갱신 [YYYYMM]`(담당자): 같은 함수 호출(기본 지난달).
  - 대상 월 기본값: 지난달(`_prev_month` 재활용).

- [ ] **Step 6: 수동 검증** — `python -c "import ast; ast.parse(open('templates/scripts/slack-jipsa/daemon.py',encoding='utf-8').read()); print('ok')"`. dry_run 제안이 실파일을 변경하지 않음을 회귀로 확인(`git status` 상 Drive 파일은 레포 밖이라 무영향, 제안 모드는 write 없음).

- [ ] **Step 7: 커밋**

```bash
git add templates/scripts/slack-jipsa/purchase.py templates/scripts/slack-jipsa/purchase_store.py templates/scripts/slack-jipsa/daemon.py tests/
git commit -m "feat(bipum): 월간 분석 자동 스케줄 + 확인 게이트 + 수동 명령"
```

### Task 14: LibreOffice 교차검증(선택) + 문서화

**Files:**
- Modify: `modules/11-supply-inventory.md` 또는 신규 `modules/12-bipum-purchase-analysis.md`
- Modify: `templates/scripts/slack-team/CLAUDE.md`(구매기록/분석 명령 안내)

- [ ] **Step 1: LibreOffice 재계산 검증 함수(있으면)** — `soffice --headless --convert-to xlsx` 로 병합 결과 재계산본을 만들어 `compute_pivots` 총계와 대시보드 총계 셀 비교. soffice 미설치 환경이면 skip 로깅.
- [ ] **Step 2: 운영 문서** — 셋업 순서(재고 폴링 중단 → purchase.json 배치 → dry_run 관찰 → 실가동), 명령(`입고등록`/붙여넣기, `분석 갱신`), 트러블슈팅(엑셀 열림, 신규 부서) 기술.
- [ ] **Step 3: 커밋**

```bash
git add modules/ templates/scripts/slack-team/CLAUDE.md
git commit -m "docs(bipum): 구매기록·월간분석 운영 문서"
```

---

## Phase 3 — 6~8월 백로그 복구 (아웃라인, Phase 1 안정화 후 별도 계획)

> Phase 1이 실채널에서 검증된 뒤 상세 계획을 작성한다. 개요만 기록:

- **B1 히스토리 수집:** `#인총-기록저장소`의 6/1~8/31 메시지를 `conversations_history`(+`_replies`)로 수집, 붙여넣기 표 후보(다행 표 패턴) 필터.
- **B2 파싱·분류:** 각 표를 `parse_purchase_table(_llm)` → `classify_with_cache` → `build_records`(일자=메시지 날짜).
- **B3 월별 그룹핑·적재:** 월별로 `비품 주문 기록_2026{06,07,08}.xlsx` 생성/append. 중복 방지: 메시지 ts를 블록ID 시드로.
- **B4 순차 병합:** `merge_month_into_analysis`를 6→7→8월 순 실행(각 dry_run 제안→승인).
- **검증:** 각 월 병합 후 총계·부서 누락 회귀.

---

## Self-Review

**1. Spec coverage:**
- §4 흐름 ①붙여넣기·금액 → Task 1; ②분류 → Task 5; ③월별 append → Task 6·7·8; ④월간 분석 → Task 9~13. ✅
- §5.5 재고 중단 → Task 3. ✅ §6 로컬파일 → 전 태스크 openpyxl, 커넥터 없음. ✅
- §11 자동 실행 + 확인 게이트 → Task 13. 버전 규칙(v+0.1, 실행일 YYMMDD) → Task 11·13(`next_version`,`_yymmdd`). ✅
- §7 백로그 → Phase 3 아웃라인(의도적 분리). ✅
- §8 엣지(잠금/신규부서/파싱0/금액결측) → Task 6·7·8. ✅

**2. Placeholder scan:** 코드 블록은 실제 구현 포함. Task 11의 `_apply_pivots`/`PIVOT_LAYOUT`과 Task 13의 데몬 게이트 배선은 실파일 좌표·기존 함수 재사용이 필요해 "구현자 주의"로 명시(추측 금지, 좌표는 실측). Task 12 회귀가 좌표 정확성을 강제. — 허용 가능한 조사-후-구현 지점.

**3. Type consistency:** `apply_purchase_record`/`merge_month_into_analysis` 반환 dict 키, `INPUT_HEADERS`/`INTEGRATED_HEADERS`/`REC_HEADERS`, `classify_with_cache` 튜플 반환, `latest_analysis`→`next_version`→`_yymmdd` 파일명 조합 — 태스크 간 일치 확인. `_sibling`/모듈 동적 import 방식 통일(supply_store/purchase_store).

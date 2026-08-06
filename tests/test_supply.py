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


def ev(rid, item, qty, done=True):
    return {'record_id': rid, 'raw_item': item, 'qty': qty, 'done': done}


def hi_resolver(canonical, category='기타'):
    def r(raw, known): return {'canonical': canonical, 'category': category, 'confidence': 'high'}
    return r


class ProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_baseline_marks_without_decrement(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, {'counted': [], 'baseline_done': False},
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 10)
        self.assertIn('R1', res['state']['counted'])
        self.assertTrue(res['state']['baseline_done'])

    def test_decrement_after_baseline(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 7)
        self.assertEqual(len(res['ledger']), 1)
        self.assertEqual(res['ledger'][0]['처리후잔여'], 7)

    def test_idempotent_counted(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '', '현재수량': 7,
                            '최소수량': 2, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, {'counted': ['R1'], 'baseline_done': True},
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 7)

    def test_not_done_skipped(self):
        res = self.m.process_events([ev('R1', 'A4용지', 3, done=False)], {}, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['ledger'], [])
        self.assertNotIn('R1', res['state']['counted'])

    def test_new_item_created_and_alert(self):
        res = self.m.process_events([ev('R1', '새품목', 2)], {}, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('새품목', '기타'), dry_run=False)
        self.assertIn('새품목', res['stock'])
        self.assertEqual(res['stock']['새품목']['현재수량'], -2)
        self.assertTrue(any('새품목' in a and ('신규' in a or '재고' in a) for a in res['alerts']))

    def test_low_stock_alert(self):
        stock = {'볼펜': {'품목': '볼펜', '카테고리': '사무용품', '현재수량': 3,
                          '최소수량': 5, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', '볼펜', 1)], stock, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('볼펜'), dry_run=False)
        self.assertEqual(res['stock']['볼펜']['현재수량'], 2)
        self.assertTrue(any('저재고' in a for a in res['alerts']))

    def test_qty_zero_skipped_with_alert(self):
        res = self.m.process_events([ev('R1', 'A4용지', 0)], {}, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('A4용지'), dry_run=False)
        self.assertEqual(res['ledger'], [])
        self.assertTrue(any('수량' in a for a in res['alerts']))

    def test_dry_run_no_state_change(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '', '현재수량': 10,
                            '최소수량': 2, '단위': '', '비고': ''}}
        res = self.m.process_events([ev('R1', 'A4용지', 3)], stock, {}, {'counted': [], 'baseline_done': True},
                                    hi_resolver('A4용지'), dry_run=True)
        self.assertEqual(res['stock']['A4용지']['현재수량'], 10)
        self.assertNotIn('R1', res['state']['counted'])
        self.assertTrue(any('A4용지' in a for a in res['alerts']))

    def test_pending_when_unresolved(self):
        def low(raw, known): return {'canonical': '', 'category': '', 'confidence': 'low'}
        res = self.m.process_events([ev('R1', '애매품목', 2)], {}, {}, {'counted': [], 'baseline_done': True},
                                    low, dry_run=False)
        self.assertEqual(len(res['pending']), 1)
        self.assertEqual(res['ledger'], [])
        self.assertNotIn('R1', res['state']['counted'])


class InboundTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_parse_pipe_table(self):
        txt = ("no | 품명 | 수량 | 금액 | 출금계좌 | 부서\n"
               "1 | 디퓨저 리필액, 6개 | 1 | 30,480 | 대구은행 379-1 | 전부서\n"
               "12 | 차량용 방향제 | 17 | 423,300 | 대구은행 379-1 | 매입부")
        rows = self.m.parse_purchase_table(txt)
        self.assertEqual(len(rows), 2)                 # 헤더 제외
        self.assertEqual(rows[0]['품명'], '디퓨저 리필액, 6개')
        self.assertEqual(rows[0]['수량'], 1)            # 금액(30480) 아님
        self.assertEqual(rows[1]['수량'], 17)
        self.assertEqual(rows[1]['부서'], '매입부')

    def test_parse_tab_table_no_header(self):
        txt = "딱풀\t4\t8000\n클립\t2\t3000"
        rows = self.m.parse_purchase_table(txt)
        self.assertEqual([(r['품명'], r['수량']) for r in rows], [('딱풀', 4), ('클립', 2)])

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

    def test_process_inbound_adds(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 5,
                            '최소수량': 0, '단위': '', '비고': ''}}
        rows = [{'품명': 'A4', '수량': 3, '부서': '전부서'}]
        res = self.m.process_inbound(rows, stock, {'a4': 'A4용지'}, hi_resolver('A4용지'))
        self.assertEqual(res['stock']['A4용지']['현재수량'], 8)   # 5+3
        self.assertEqual(res['ledger'][0]['유형'], '입고')
        self.assertFalse(res['stock'] is stock)                   # 원본 미변경

    def test_process_inbound_new_item(self):
        res = self.m.process_inbound([{'품명': '클립', '수량': 2, '부서': ''}], {}, {},
                                     hi_resolver('클립', '사무용품'))
        self.assertEqual(res['stock']['클립']['현재수량'], 2)
        self.assertTrue(any('신규' in a for a in res['alerts']))

    def test_process_inbound_pending_not_added(self):
        def low(raw, known): return {'canonical': '', 'category': '', 'confidence': 'low'}
        res = self.m.process_inbound([{'품명': '머시기', '수량': 9, '부서': ''}], {}, {}, low)
        self.assertEqual(res['stock'], {})
        self.assertEqual(len(res['pending']), 1)

    def test_llm_parse_fallback(self):
        # run_claude 가 JSON 배열을 돌려준다고 가정(칸 깨진 표를 LLM이 추출한 결과)
        def fake(prompt):
            return ('답: [{"품명":"16온스 종이컵, 1000개","수량":5,"부서":"전부서"},'
                    '{"품명":"클래식 점보롤","수량":12,"부서":"관리사무소"}] 끝')
        rows = self.m.parse_purchase_table_llm("아무 뭉개진 텍스트", fake)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['수량'], 5)
        self.assertEqual(rows[1]['품명'], '클래식 점보롤')

    def test_llm_parse_bad_output(self):
        self.assertEqual(self.m.parse_purchase_table_llm("x", lambda p: '죄송 JSON없음'), [])

    def test_batch_resolver_one_call(self):
        calls = []
        def fake(prompt):
            calls.append(1)
            return ('잡담 [{"in":"16온스 종이컵","canonical":"16온스 종이컵","category":"다과·음료","confidence":"high"},'
                    '{"in":"마우수","canonical":"마우스","category":"IT·전자","confidence":"high"}] 끝')
        res = self.m.resolve_batch_llm(['16온스 종이컵', '마우수'], [], fake)
        self.assertEqual(len(calls), 1)                  # 여러 품목 → 단 1회 호출
        self.assertEqual(res['16온스 종이컵']['canonical'], '16온스 종이컵')
        self.assertEqual(res['마우수']['canonical'], '마우스')

    def test_batch_resolver_empty(self):
        self.assertEqual(self.m.resolve_batch_llm([], [], lambda p: 'x'), {})

    def test_parse_pack(self):
        self.assertEqual(self.m.parse_pack('16온스 종이컵, 1000개'), ('16온스 종이컵', 1000))
        self.assertEqual(self.m.parse_pack('클립10박스'), ('클립', 10))
        self.assertEqual(self.m.parse_pack('핸드워시, 4L, 4개'), ('핸드워시, 4L', 4))
        self.assertEqual(self.m.parse_pack('클래식 점보롤'), ('클래식 점보롤', 1))
        self.assertEqual(self.m.parse_pack('A4용지'), ('A4용지', 1))
        self.assertEqual(self.m.parse_pack('AA건전지, 32개입'), ('AA건전지', 32))

    def test_inbound_multiplies_pack(self):
        # '16온스 종이컵, 1000개' 5팩 → 5000개, 규격 보존(canonical='16온스 종이컵')
        rows = [{'품명': '16온스 종이컵, 1000개', '수량': 5, '부서': '전부서'}]
        res = self.m.process_inbound(rows, {}, {}, hi_resolver('16온스 종이컵'))
        self.assertEqual(res['stock']['16온스 종이컵']['현재수량'], 5000)
        self.assertEqual(res['ledger'][0]['수량'], 5000)
        self.assertEqual(res['stock']['16온스 종이컵']['단위'], '개')

    def test_adjust_outbound_decrements(self):
        stock = {'물티슈': {'품목': '물티슈', '카테고리': '청소·위생', '현재수량': 10,
                           '최소수량': 3, '단위': '', '비고': ''}}
        res = self.m.process_adjust('물티슈', 4, '출고', stock, {'물티슈': '물티슈'}, hi_resolver('물티슈'))
        self.assertEqual(res['stock']['물티슈']['현재수량'], 6)
        self.assertEqual(res['ledger'][0]['유형'], '출고')

    def test_adjust_stocktake_sets_absolute(self):
        stock = {'A4용지': {'품목': 'A4용지', '카테고리': '사무용품', '현재수량': 99,
                           '최소수량': 5, '단위': '', '비고': ''}}
        res = self.m.process_adjust('A4용지', 3, '실사', stock, {'a4용지': 'A4용지'}, hi_resolver('A4용지'))
        self.assertEqual(res['stock']['A4용지']['현재수량'], 3)        # 절대값 설정
        self.assertEqual(res['ledger'][0]['유형'], '실사')
        self.assertTrue(any('저재고' in a for a in res['alerts']))     # 3 < 5

    def test_adjust_outbound_below_zero_warns(self):
        res = self.m.process_adjust('없는것', 2, '출고', {}, {}, hi_resolver('없는것'))
        self.assertEqual(res['stock']['없는것']['현재수량'], -2)
        self.assertTrue(any('신규' in a for a in res['alerts']))

    def test_inbound_distinct_specs_not_merged(self):
        # resolver가 입력명을 그대로 canonical로 → 16온스 vs 180ml 분리 유지
        def keep(raw, known): return {'canonical': raw, 'category': '다과·음료', 'confidence': 'high'}
        rows = [{'품명': '16온스 종이컵, 1000개', '수량': 5, '부서': 'A'},
                {'품명': '종이컵 180ml, 2000개', '수량': 2, '부서': 'A'}]
        res = self.m.process_inbound(rows, {}, {}, keep)
        self.assertEqual(res['stock']['16온스 종이컵']['현재수량'], 5000)
        self.assertEqual(res['stock']['종이컵 180ml']['현재수량'], 4000)


if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

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

if __name__ == '__main__':
    unittest.main()

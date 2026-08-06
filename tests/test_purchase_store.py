import unittest, importlib.util, tempfile
from pathlib import Path
import openpyxl
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/purchase_store.py'
def load():
    spec = importlib.util.spec_from_file_location('pstore_uut', SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class LogIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load()
    def test_create_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'비품 구매기록_자동.xlsx'
            recs=[{'월':'8월','일자':'2026-08-06','부서':'재무회계','용도':'사내비품','카테고리':'IT·전자',
                   '품목':'무선마우스','수량':2,'단가':49900,'금액':99800,'블록ID':'202608#1'}]
            n = self.m.append_purchase_log(out, recs)
            self.assertEqual(n, 1)
            wb = openpyxl.load_workbook(out); ws = wb[self.m.LOG_SHEET]
            self.assertEqual([c.value for c in ws[1]], self.m.LOG_HEADERS)   # 헤더 r1
            self.assertEqual([c.value for c in ws[2]][:10],
                ['8월','2026-08-06','재무회계','사내비품','IT·전자','무선마우스',2,49900,99800,'202608#1'])
            wb.close()
    def test_append_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'log.xlsx'
            self.m.append_purchase_log(out, [{'월':'8월','품목':'a','금액':1,'블록ID':'202608#1'}])
            self.m.append_purchase_log(out, [{'월':'8월','품목':'b','금액':2,'블록ID':'202608#2'}])
            rows = self.m.read_purchase_log(out)
            self.assertEqual([r['품목'] for r in rows], ['a','b'])   # 누적
    def test_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'log.xlsx'
            self.m.append_purchase_log(out, [
                {'월':'8월','일자':'2026-08-06','부서':'인사총무','용도':'사내비품','카테고리':'사무용품',
                 '품목':'볼펜','수량':1,'단가':1000,'금액':1000,'블록ID':'202608#1'}])
            rows = self.m.read_purchase_log(out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0].keys()), set(self.m.LOG_HEADERS))
            self.assertEqual(rows[0]['금액'], 1000)
    def test_read_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.m.read_purchase_log(Path(d)/'nope.xlsx'), [])

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

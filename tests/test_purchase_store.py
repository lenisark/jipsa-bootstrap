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

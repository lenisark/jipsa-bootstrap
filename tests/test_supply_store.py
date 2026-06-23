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

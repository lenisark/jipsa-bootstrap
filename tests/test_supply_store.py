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
        unlocked = self.dir / '비품재고_현황_unlocked.xlsx'
        unlocked.touch()
        self.assertFalse(self.m.is_locked(unlocked))

    def test_alias_roundtrip(self):
        self.m.write_stock(self.stock, {})           # 파일 생성(재고 시트)
        self.m.write_aliases(self.stock, {
            '복사용지 a4': {'canonical': 'A4용지', '출처': 'list',
                          '확신도': 'high', '결정방식': 'auto', '결정시각': '2026-06-23'}})
        amap = self.m.read_aliases(self.stock)
        self.assertEqual(amap['복사용지 a4'], 'A4용지')   # 조회맵은 원문→canonical

    def test_alias_full_preserves_metadata(self):
        self.m.write_stock(self.stock, {})
        self.m.write_aliases(self.stock, {
            '복사용지 a4': {'canonical': 'A4용지', '출처': 'list',
                          '확신도': 'high', '결정방식': 'auto', '결정시각': '2026-06-23'}})
        full = self.m.read_aliases_full(self.stock)
        self.assertEqual(full['복사용지 a4']['canonical'], 'A4용지')
        self.assertEqual(full['복사용지 a4']['출처'], 'list')
        self.assertEqual(full['복사용지 a4']['확신도'], 'high')

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


if __name__ == '__main__':
    unittest.main()

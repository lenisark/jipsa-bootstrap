"""구매표 날짜 칸 인식 + 주문 캡처 판독 결과 → 구매행."""
import tempfile, unittest, importlib.util
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa'


def load(name):
    spec = importlib.util.spec_from_file_location(f'{name}_uut2', BASE / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class DateCellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.s = load('supply')

    def test_formats(self):
        t = date(2026, 9, 29)
        for raw in ('9월 17일', '9월17일', '9/17', '2026. 9. 17', '2026-09-17', '9.17'):
            self.assertEqual(self.s.parse_date_cell(raw, t), '2026-09-17', raw)
        self.assertIsNone(self.s.parse_date_cell('192,895', t))
        self.assertIsNone(self.s.parse_date_cell('13월 1일', t))

    def test_year_rolls_back_in_january(self):  # 1월에 12월분 입력
        self.assertEqual(self.s.parse_date_cell('12월 30일', date(2027, 1, 3)), '2026-12-30')

    def test_table_with_dates_and_carry_forward(self):
        rows = self.s.parse_purchase_table(
            '날짜 | 품명 | 수량 | 금액 | 부서\n'
            '9월 9일 | 보리차 | 1 | 21,660 | 인사총무\n'
            ' | CO2 장갑걸이 | 1 | 9,900 | 인사총무\n'       # 날짜 빈 칸 → 윗줄 날짜
            '9/15 | 각티슈 | 2 | 20,000 | 전부서', date(2026, 10, 2))
        self.assertEqual([(r['품명'], r['날짜'], r['부서']) for r in rows],
                         [('보리차', '2026-09-09', '인사총무'), ('CO2 장갑걸이', '2026-09-09', '인사총무'),
                          ('각티슈', '2026-09-15', '전부서')])

    def test_table_without_dates_unchanged(self):
        rows = self.s.parse_purchase_table('볼펜\t3\t3,000\t전부서')
        self.assertEqual(rows, [{'품명': '볼펜', '수량': 3, '금액': 3000, '부서': '전부서', '날짜': ''}])


class RecordMonthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load('purchase')

    def test_row_date_decides_month_and_blockid(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = {'folder': str(d), 'purchase_log': 'log.xlsx', 'classify_cache': str(d / 'c.json'),
                   'known_depts': ['전부서 공통'], 'dept_aliases': {}}
            fake = lambda p: '[{"품목":"볼펜","용도":"사내비품","카테고리":"사무용품"}]'
            self.m.apply_purchase_record(cfg, [{'품명': '볼펜', '수량': 1, '금액': 1000, '부서': '전부서'}],
                                         fake, '2026-09-29')
            res = self.m.apply_purchase_record(cfg, [
                {'품명': '볼펜', '수량': 1, '금액': 1000, '부서': '전부서', '날짜': '2026-09-17'},
                {'품명': '볼펜', '수량': 1, '금액': 1000, '부서': '전부서', '날짜': ''},
            ], fake, '2026-10-02')                     # 10월에 입력한 9월분 + 날짜 없는 행
            got = [(r['월'], r['일자'], r['블록ID']) for r in res['records']]
            self.assertEqual(got, [('9월', '2026-09-17', '202609#2'), ('10월', '2026-10-02', '202610#1')])


class CaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m, cls.s = load('purchase'), load('supply')

    def test_parse_capture_json(self):
        out = ('```json\n[{"품명":"탐사 종이컵 185ml 2000개입","수량":4,"단가":"24,990","날짜":"2026-09-17"},'
               '{"품명":"큐센 마우스","수량":2,"단가":5230,"날짜":""},'
               '{"품명":"수량없음","수량":0,"단가":100,"날짜":""}]\n```')
        rows = self.m.parse_capture_json(out, '전부서')
        self.assertEqual(rows, [
            {'품명': '탐사 종이컵 185ml 2000개입', '수량': 4, '금액': 99960, '부서': '전부서', '날짜': '2026-09-17'},
            {'품명': '큐센 마우스', '수량': 2, '금액': 10460, '부서': '전부서', '날짜': ''}])
        self.assertEqual(self.m.parse_capture_json('못 읽음'), [])

    def test_preview_table_round_trips_through_paste_parser(self):
        rows = [{'품명': '탐사 종이컵', '수량': 4, '금액': 99960, '부서': '전부서', '날짜': '2026-09-17'},
                {'품명': '큐센 마우스', '수량': 2, '금액': 10460, '부서': '인사총무', '날짜': ''}]
        back = self.s.parse_purchase_table(self.m.rows_to_table(rows), date(2026, 9, 29))
        # 날짜 없는 행은 윗줄 날짜를 이어받는다(붙여넣기 규칙과 같음)
        self.assertEqual([(r['품명'], r['수량'], r['금액'], r['부서'], r['날짜']) for r in back],
                         [('탐사 종이컵', 4, 99960, '전부서', '2026-09-17'),
                          ('큐센 마우스', 2, 10460, '인사총무', '2026-09-17')])


class MonthEndTest(unittest.TestCase):
    def test_last_business_day(self):
        r = load('reminders')
        self.assertEqual(r.effective_notify_date(2026, 9, 31), date(2026, 9, 30))   # 9월은 30일 수
        self.assertEqual(r.effective_notify_date(2026, 10, 31), date(2026, 10, 30))  # 10/31 토 → 금


if __name__ == '__main__':
    unittest.main()

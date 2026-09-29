"""휴일 처리(주간 알림 다음 영업일·평일 알림 휴일 건너뜀) + 분류 가이드/용도 규칙(opt-in)."""
import tempfile, unittest, importlib.util
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa'


def load(name):
    spec = importlib.util.spec_from_file_location(f'{name}_uut3', BASE / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class HolidayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.r = load('reminders')

    def fires(self, rule, days):
        return [d for d in days if self.r._due_today(rule, d)]

    def test_weekly_monday_holiday_moves_to_tuesday(self):  # 2026-10-05 개천절 대체휴일
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(12)]
        self.assertEqual(self.fires({'freq': 'weekly', 'weekday': 0}, days),
                         [date(2026, 10, 6), date(2026, 10, 12)])

    def test_weekdays_skip_weekday_holidays(self):  # 10/5 대체휴일, 10/9 한글날
        days = [date(2026, 10, 5) + timedelta(days=i) for i in range(5)]
        self.assertEqual(self.fires({'freq': 'weekdays', 'weekdays': [0, 1, 2, 3, 4]}, days),
                         [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)])

    def test_weekend_rules_untouched(self):
        days = [date(2026, 10, 3), date(2026, 10, 4)]           # 토(개천절)·일
        self.assertEqual(self.fires({'freq': 'weekdays', 'weekdays': [5, 6]}, days), days)
        self.assertEqual(self.fires({'freq': 'weekly', 'weekday': 5}, days), [date(2026, 10, 3)])


class UseRuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = load('purchase')

    def test_decide_use(self):
        d = self.m.decide_use
        shared = ['매입부', '제휴매입파트']
        self.assertEqual(d('공용(사내·외부 혼재)', '인사총무', None), '공용(사내·외부 혼재)')  # 미설정 → LLM 그대로
        self.assertEqual(d('공용(사내·외부 혼재)', '인사총무', shared), '사내비품')
        self.assertEqual(d('사내비품', '매입부', shared), '공용(사내·외부 혼재)')
        self.assertEqual(d('재판매·대고객', '인사총무', shared), '재판매·대고객')           # 품목 기준 유지

    def test_guide_sheet_loaded(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as t:
            wb = openpyxl.Workbook(); ws = wb.active; ws.title = '분류_가이드'
            ws.append(['카테고리', '예시']); ws.append(['다과·음료', '종이컵·뚜껑'])
            wb.save(Path(t) / '260907-HGA-비품주문분석-v1.10.xlsx')
            g = self.m.load_guide_text({'folder': t, 'analysis_prefix': '비품주문분석'})
            self.assertEqual(g, '카테고리 | 예시\n다과·음료 | 종이컵·뚜껑')
            self.assertEqual(self.m.load_guide_text({'folder': t + '/없음', 'analysis_prefix': 'x'}), '')


if __name__ == '__main__':
    unittest.main()

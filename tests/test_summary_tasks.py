"""일일 요약 기간 + 요약 → 미결 과제 (요약 결과의 TODO 블록·만료 알림·✅ 완료)."""
import os, tempfile, unittest, importlib.util
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa'


def load(name):
    spec = importlib.util.spec_from_file_location(f'{name}_uut', BASE / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SummaryWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = load('reminders')

    def test_default_is_previous_business_day(self):
        self.assertEqual(self.r.summary_window(date(2026, 9, 29), None), (date(2026, 9, 28), date(2026, 9, 29)))
        self.assertEqual(self.r.summary_window(date(2026, 9, 14), None), (date(2026, 9, 11), date(2026, 9, 14)))  # 월 → 금부터

    def test_skips_holidays(self):  # 2026 추석 연휴(9/24~27) 뒤 월요일 → 연휴 전 영업일부터
        self.assertEqual(self.r.summary_window(date(2026, 9, 28), None), (date(2026, 9, 23), date(2026, 9, 28)))

    def test_cursor_continues_and_caps_7_days(self):
        self.assertEqual(self.r.summary_window(date(2026, 9, 30), date(2026, 9, 28)), (date(2026, 9, 28), date(2026, 9, 30)))
        self.assertEqual(self.r.summary_window(date(2026, 9, 30), date(2026, 9, 1)), (date(2026, 9, 23), date(2026, 9, 30)))

    def test_today_mode_ends_tomorrow(self):  # '오늘 대화 요약' → end_day=내일
        self.assertEqual(self.r.summary_window(date(2026, 9, 30), None), (date(2026, 9, 29), date(2026, 9, 30)))


class TodoBlockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load('tasks')

    def test_split(self):
        body, items = self.t.split_todo_block(
            '*요약*\n- a\n<<<TODO\n[{"title":"도면 공유","owner":"김영진","due":""}]\nTODO>>>')
        self.assertEqual(body, '*요약*\n- a')
        self.assertEqual(items, [{'title': '도면 공유', 'owner': '김영진', 'due': ''}])

    def test_missing_or_broken_block(self):
        self.assertEqual(self.t.split_todo_block('요약만'), ('요약만', []))
        self.assertEqual(self.t.split_todo_block('요약\n<<<TODO\n깨짐\nTODO>>>'), ('요약', []))
        self.assertEqual(self.t.split_todo_block('요약\n<<<TODO\n```json\n[]\n```\nTODO>>>'), ('요약', []))

    def test_expiry_title(self):
        self.assertEqual(
            self.t.expiry_title('[계약 만료 임박] A · 기타 · *화재보험* (KB) 종료 2026-09-22 경과 <https://x|계약서>'),
            '[만료 경과] 화재보험 (KB) — 포털에서 갱신/종료 처리')
        self.assertEqual(
            self.t.expiry_title('[차량 보험 만료 임박] A · *12가3456* 레이 · 보험 만료 2026-10-16 경과 <https://x|서류>'),
            '[만료 경과] 12가3456 보험 만료 — 포털에서 갱신/종료 처리')
        self.assertIsNone(self.t.expiry_title('[계약 만료 임박] A · *리스* (B) 종료 2026-10-26 D-30'))
        self.assertIsNone(self.t.expiry_title('비품신청: A4 경과'))


class OpenItemsTest(unittest.TestCase):
    def setUp(self):
        self.t = load('tasks')
        self.t.DB_PATH = Path(tempfile.mkdtemp()) / 'jipsa.db'
        self.t.init_db()

    def test_dedupe_msg_lookup_and_close(self):
        made = self.t.add_open_items('C1', [{'title': '도면 공유', 'owner': '김영진', 'due': ''},
                                            {'title': '도면  공유', 'owner': '', 'due': ''}], 'test')
        self.assertEqual(len(made), 1)                                  # 공백만 다른 중복 제거
        self.t.add_msg_ts(made[0]['id'], '111.1')
        hit = self.t.find_open_by_msg('C1', '111.1')
        self.assertEqual(hit['id'], made[0]['id'])
        self.assertIsNone(self.t.find_open_by_msg('C1', '999'))
        self.assertTrue(self.t.close_task(hit['id']))                   # 대기 → 진행 → 완료
        self.assertEqual(self.t.get_task(hit['id'])['state'], '완료')
        self.assertIsNone(self.t.find_open_by_msg('C1', '111.1'))
        self.assertEqual(self.t.add_open_items('C1', [{'title': '도면 공유', 'owner': '', 'due': ''}], 'test'), [])  # 닫힌 과제 되살리지 않음


if __name__ == '__main__':
    unittest.main()

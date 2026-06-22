import unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/reminders.py'


def load_reminders():
    spec = importlib.util.spec_from_file_location('reminders_uut', SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RemindersActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = load_reminders()

    def test_normal_reminder_has_no_action(self):
        it = self.r.parse_intent('매월 25일 10시에 정산 알려줘')
        self.assertEqual(it.get('freq'), 'monthly')
        self.assertFalse(it.get('action'))   # 일반 알림은 action 없음

    def test_action_reminder_detected(self):
        txt = '매일 9시에 어제 미처리 FAQ 요약해서 올려줘'
        self.assertTrue(self.r.looks_like_reminder(txt))
        it = self.r.parse_intent(txt)
        self.assertEqual(it.get('freq'), 'daily')
        self.assertTrue(it.get('action'))
        self.assertIn('요약', it['action'])   # 작업 지시 보존

    def test_action_marker_forces_action(self):
        txt = '매일 8시에 자동 백업 상태 점검해서 보고해줘'
        self.assertTrue(self.r.looks_like_reminder(txt))
        it = self.r.parse_intent(txt)
        self.assertTrue(it.get('action'))

    def test_action_extract_strips_schedule_keeps_verb(self):
        act = self.r._extract_action('매주 월요일 9시에 지난주 일정 정리해서 공유해줘')
        self.assertIn('정리', act)
        self.assertNotIn('매주', act)
        self.assertNotIn('9', act)

    def test_plain_ping_not_action(self):
        # 작업동사 없이 보고동사만 → 액션 아님
        self.assertFalse(self.r._is_action_text('매일 9시에 회의 있다고 알려줘'))

    def _run_handle(self, text):
        """handle 전체 경로를 가짜 web/임시저장소로 실행 → 게시된 텍스트 목록 반환."""
        import tempfile
        from pathlib import Path as _P
        self.r.REMINDERS_FILE = _P(tempfile.mkdtemp()) / 'r.json'
        self.r.set_executor(lambda ch, p: 'ok')
        posts = []

        class FakeWeb:
            def chat_postMessage(self, **k): posts.append(k.get('text', '')); return {'ts': '1'}
            def reactions_add(self, **k): pass
            def reactions_remove(self, **k): pass
            def users_info(self, **k): return {'user': {'profile': {'display_name': 't'}}}
        self.r.handle(FakeWeb(), 'C1', 'U1', text, '1')
        return posts

    def test_daily_reminder_registers(self):
        # 기존 버그 회귀: daily 가 _handle_add else 로 빠져 항상 err 나던 문제
        posts = self._run_handle('매일 18시에 일일보고 알려줘')
        joined = '\n'.join(posts)
        self.assertIn('등록 완료', joined)
        self.assertNotIn('이해 못했어요', joined)

    def test_daily_action_reminder_registers(self):
        posts = self._run_handle('매일 9시에 어제 대화 요약해서 올려줘')
        joined = '\n'.join(posts)
        self.assertIn('등록 완료', joined)
        self.assertIn('능동 작업', joined)        # 🤖 능동 작업 표시
        self.assertNotIn('이해 못했어요', joined)


if __name__ == '__main__':
    unittest.main()

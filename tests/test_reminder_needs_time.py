"""시간이 안 적힌 문장은 알림으로 등록하지 않고 시간을 되묻는다(예전엔 9시로 채워 오인식 등록)."""
import unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/reminders.py'


class FakeWeb:
    def __init__(self): self.posts = []
    def chat_postMessage(self, **kw): self.posts.append(kw['text'])


class NeedsTimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('reminders_time_uut', SRC)
        cls.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.r)
        cls.r.set_logger(lambda *a: None)

    def test_sentence_without_time_is_not_registered(self):
        text = ('로그인 완료 했어요 진행 못한 작업 확인해서 진행해줄래요 '
                '(자동 작업을 끝내지 못했어요: 이 채널의 대화내용 요약 + 미결 과제 등록(평일))')
        it = self.r.parse_intent(text)
        self.assertIsNone(it['hour'])
        web, saved = FakeWeb(), []
        self.r.add_reminder = lambda *a, **k: saved.append(a)
        self.r._handle_add(web, 'C1', 'U1', it)
        self.assertEqual(saved, [])
        self.assertIn('시간*이 필요해요', web.posts[0])

    def test_with_time_still_parsed(self):
        self.assertEqual(self.r.parse_intent('매주 월요일 9시에 주간회의 알려줘')['hour'], 9)
        it = self.r.parse_intent('평일 18시 30분에 일일보고 알려줘')
        self.assertEqual((it['hour'], it['minute'], it['freq']), (18, 30, 'weekdays'))


if __name__ == '__main__':
    unittest.main()

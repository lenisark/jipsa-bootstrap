"""실패한 능동 작업(예: Claude 로그인 만료) 30분 간격 재시도 — 파일 저장이라 데몬 재시작에도 이어감."""
import tempfile, unittest, importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/reminders.py'


class FakeWeb:
    def __init__(self): self.posts = []
    def chat_postMessage(self, **kw): self.posts.append(kw['text'])


class RetryTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('reminders_retry_uut', SRC)
        self.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.r)
        self.r.RETRY_FILE = Path(tempfile.mkdtemp()) / 'action_retry.json'
        self.r.set_logger(lambda *a: None)
        self.web = FakeWeb()
        self.results = []                              # 실행기가 차례로 돌려줄 값
        self.r.set_executor(lambda ch, action: self.results.pop(0))
        self.rem = {'id': 'rmd_x', 'channel': 'C1', 'message': '대화 요약', 'action': '요약해줘'}

    def test_fail_then_retry_until_success(self):
        t0 = 1_800_000_000.0
        self.results = [None]
        self.r._run_action(self.web, self.rem)               # 첫 실패 → 안내 1번 + 재시도 등록
        self.assertEqual(len(self.web.posts), 1)
        self.assertIn('30분마다 다시 시도', self.web.posts[0])
        self.assertFalse(self.r.schedule_retry(self.rem))    # 같은 날 같은 알림은 한 번만

        d = self.r._load_retries(); key = next(iter(d)); d[key]['next'] = t0 + 1800; d[key]['until'] = t0 + 43200
        self.r._save_retries(d)
        self.r.retry_tick(self.web, now=t0 + 60, run_async=False)       # 30분 전 → 시도 안 함
        self.assertEqual(self.r._load_retries()[key]['tries'], 0)

        self.results = [None]
        self.r.retry_tick(self.web, now=t0 + 1800, run_async=False)     # 또 실패 → 남아 있음, 추가 안내 없음
        self.assertEqual(self.r._load_retries()[key]['tries'], 1)
        self.assertEqual(len(self.web.posts), 1)

        self.results = ['*요약 결과*']
        self.r.retry_tick(self.web, now=t0 + 3600, run_async=False)     # 성공 → 결과 게시 + 목록에서 제거
        self.assertEqual(self.web.posts[-1], '*요약 결과*')
        self.assertEqual(self.r._load_retries(), {})

    def test_executor_posted_itself(self):                    # 일일 요약은 직접 게시하고 '' 반환
        t0 = 1_800_000_000.0
        self.assertTrue(self.r.schedule_retry(self.rem, now=t0))
        self.results = ['']
        self.r.retry_tick(self.web, now=t0 + 1800, run_async=False)
        self.assertEqual((self.web.posts, self.r._load_retries()), ([], {}))

    def test_give_up_after_12_hours(self):
        t0 = 1_800_000_000.0
        self.r.schedule_retry(self.rem, now=t0)
        self.r.retry_tick(self.web, now=t0 + 12 * 3600 + 1, run_async=False)
        self.assertEqual(self.r._load_retries(), {})
        self.assertIn('다시 시도를 멈췄어요', self.web.posts[-1])


if __name__ == '__main__':
    unittest.main()

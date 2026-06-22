import os, time, tempfile, unittest, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa'


def _load(name, db_path):
    spec = importlib.util.spec_from_file_location(name + '_uut', ROOT / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = Path(db_path)
    return mod


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db = os.path.join(self.tmp, 'jipsa.db')
        self.t = _load('tasks', db); self.t.init_db()
        self.a = _load('approval', db)   # 같은 DB 공유

    def test_request_creates_pending(self):
        tok = self.a.request_approval('T1', 'C1', 'rm -rf ./build 실행',
                                      approvers=['U_OWNER'], timeout_min=15)
        row = self.a.get_approval(tok)
        self.assertEqual(row['status'], '대기')
        self.assertEqual(row['channel_id'], 'C1')
        self.assertGreater(row['expires_at'], int(time.time()))

    def test_approve_by_authorized(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_OWNER'], timeout_min=15)
        self.assertEqual(self.a.decide(tok, 'U_OWNER'), '승인')
        self.assertEqual(self.a.get_approval(tok)['status'], '승인')
        self.assertEqual(self.a.get_approval(tok)['approver_id'], 'U_OWNER')

    def test_reject_by_unauthorized(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_OWNER'], timeout_min=15)
        self.assertEqual(self.a.decide(tok, 'U_STRANGER'), '권한없음')
        self.assertEqual(self.a.get_approval(tok)['status'], '대기')  # 변동 없음

    def test_single_use_no_double_decide(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_A', 'U_B'], timeout_min=15)
        self.assertEqual(self.a.decide(tok, 'U_A'), '승인')
        self.assertEqual(self.a.decide(tok, 'U_B'), '이미처리')  # 두 번째 클릭 무효

    def test_explicit_reject(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_A'], timeout_min=15)
        self.assertEqual(self.a.decide(tok, 'U_A', approve=False), '거부')
        self.assertEqual(self.a.get_approval(tok)['status'], '거부')

    def test_expiry_sweep(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_A'], timeout_min=0)  # 즉시만료
        time.sleep(0.01)
        self.a.expire_stale()
        self.assertEqual(self.a.get_approval(tok)['status'], '만료')
        self.assertEqual(self.a.decide(tok, 'U_A'), '만료')  # 만료 후 클릭 무효

    def test_build_card_has_token_buttons(self):
        tok = self.a.request_approval('T1', 'C1', 'rm -rf', approvers=['U_A'], timeout_min=15)
        blocks = self.a.build_card(tok, 'rm -rf')
        dumped = str(blocks)
        self.assertIn(tok, dumped)
        self.assertIn('승인', dumped)
        self.assertIn('거부', dumped)

    def test_build_card_mentions_approvers(self):
        tok = self.a.request_approval('T1', 'C1', 'x', approvers=['U_HR'], timeout_min=60)
        dumped = str(self.a.build_card(tok, 'x', mentions=['U_HR']))
        self.assertIn('<@U_HR>', dumped)        # escalate: 승인자 멘션
        plain = str(self.a.build_card(tok, 'x'))
        self.assertNotIn('<@', plain)            # 기본 모드는 멘션 없음


if __name__ == '__main__':
    unittest.main()

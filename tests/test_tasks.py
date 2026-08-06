import os, tempfile, unittest, importlib.util
from pathlib import Path

# 템플릿 디렉토리에서 tasks.py 를 직접 로드 (설치 없이 테스트)
SRC = Path(__file__).resolve().parents[1] / 'templates/scripts/slack-jipsa/tasks.py'


def load_tasks(db_path):
    spec = importlib.util.spec_from_file_location('tasks_under_test', SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = Path(db_path)          # 테스트 격리: 임시 DB로 교체
    mod.init_db()
    return mod


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = load_tasks(os.path.join(self.tmp, 'jipsa.db'))

    def test_create_and_get(self):
        tid = self.t.create_task('C1', '정산 정리', body='6월분', direction='h2a')
        row = self.t.get_task(tid)
        self.assertEqual(row['title'], '정산 정리')
        self.assertEqual(row['state'], '대기')
        self.assertEqual(row['channel_id'], 'C1')
        self.assertEqual(row['direction'], 'h2a')
        self.assertEqual(row['assignee'], 'agent')

    def test_list_filters_by_channel_and_state(self):
        a = self.t.create_task('C1', 'A'); b = self.t.create_task('C1', 'B')
        self.t.create_task('C2', 'C')
        self.t.set_state(b, '진행'); self.t.set_state(b, '완료')  # 대기→진행→완료
        open_c1 = self.t.list_tasks('C1', states=('대기', '진행', '막힘'))
        self.assertEqual({r['id'] for r in open_c1}, {a})

    def test_valid_transition(self):
        tid = self.t.create_task('C1', 'X')
        self.assertTrue(self.t.set_state(tid, '진행'))
        self.assertTrue(self.t.set_state(tid, '완료'))
        self.assertEqual(self.t.get_task(tid)['state'], '완료')

    def test_invalid_transition_rejected(self):
        tid = self.t.create_task('C1', 'X')          # 대기
        self.assertFalse(self.t.set_state(tid, '완료'))  # 대기→완료 금지
        self.assertEqual(self.t.get_task(tid)['state'], '대기')

    def test_terminal_state_is_frozen(self):
        tid = self.t.create_task('C1', 'X')
        self.t.set_state(tid, '취소')
        self.assertFalse(self.t.set_state(tid, '진행'))

    def test_blocked_roundtrip(self):
        tid = self.t.create_task('C1', 'X')
        self.t.set_state(tid, '진행'); self.t.set_state(tid, '막힘')
        self.assertTrue(self.t.set_state(tid, '진행'))   # 해소


if __name__ == '__main__':
    unittest.main()

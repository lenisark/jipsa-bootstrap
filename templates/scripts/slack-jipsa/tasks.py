"""jipsa 2.0 작업 객체(Task Layer) — stdlib sqlite3 단일 파일 store.

- 휘발성 슬랙 대화를 상태 가진 task로 승격(대기/진행/막힘/완료/취소).
- 승인 게이트(approval.py)의 랑데부 지점이라 원자성 필수 → SQLite(WAL).
- daemon.py 의 형제 모듈. `import tasks` 로 로드(reminders.py 와 동일 패턴).
"""
from __future__ import annotations

import json
import time
import uuid
import sqlite3
from pathlib import Path

BASE = Path.home() / '.claude/scripts/slack-jipsa'
DB_PATH = BASE / 'jipsa.db'          # 테스트는 이 전역을 임시경로로 교체

STATES = ('대기', '진행', '막힘', '완료', '취소')
DIRECTIONS = ('h2a', 'a2h')          # 사람→에이전트 / 에이전트→사람

# 상태 기계 (설계 A-3). 종결 상태(완료·취소)는 빈 집합 → 동결.
_TRANSITIONS = {
    '대기': {'진행', '막힘', '취소'},
    '진행': {'완료', '막힘', '취소'},
    '막힘': {'진행', '취소'},
    '완료': set(),
    '취소': set(),
}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')     # 데몬+훅+sweeper 동시쓰기 안전
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, body TEXT,
            state TEXT, direction TEXT, assignee TEXT, thread_ts TEXT,
            created_at INTEGER, updated_at INTEGER, meta TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS approvals (
            token TEXT PRIMARY KEY, task_id TEXT, channel_id TEXT,
            action_desc TEXT, status TEXT, approver_id TEXT,
            approvers TEXT, requested_at INTEGER, decided_at INTEGER,
            expires_at INTEGER, thread_ts TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tasks_ch ON tasks(channel_id, state)')


def create_task(channel_id: str, title: str, body: str = '', direction: str = 'h2a',
                assignee: str = 'agent', thread_ts: str = '', meta: dict | None = None) -> str:
    tid = str(uuid.uuid4())
    now = int(time.time())
    with _conn() as c:
        c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  (tid, channel_id, title, body, '대기', direction, assignee,
                   thread_ts, now, now, json.dumps(meta or {}, ensure_ascii=False)))
    return tid


def get_task(task_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    return dict(r) if r else None


def list_tasks(channel_id: str, states: tuple[str, ...] | None = None) -> list[dict]:
    q = 'SELECT * FROM tasks WHERE channel_id=?'
    args: list = [channel_id]
    if states:
        q += ' AND state IN (%s)' % ','.join('?' * len(states))
        args += list(states)
    q += ' ORDER BY created_at DESC'
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def set_state(task_id: str, state: str) -> bool:
    """상태 전이. 허용된 전이만 수행하고 성공 여부 반환."""
    if state not in STATES:
        return False
    cur = get_task(task_id)
    if not cur:
        return False
    if state not in _TRANSITIONS.get(cur['state'], set()):
        return False
    with _conn() as c:
        c.execute('UPDATE tasks SET state=?, updated_at=? WHERE id=?',
                  (state, int(time.time()), task_id))
    return True


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k}=?' for k in fields)
    with _conn() as c:
        c.execute(f'UPDATE tasks SET {cols}, updated_at=? WHERE id=?',
                  (*fields.values(), int(time.time()), task_id))

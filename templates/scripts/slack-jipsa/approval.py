"""jipsa 2.0 승인 게이트 — 토큰 발급·검증, Block Kit 카드, verdict CAS 적용.

검증된 패턴(사직서/수습평가 봇의 버튼→UUID토큰→승인자검증→상태전진)을
데몬 안에서 Python으로 재구현. tasks.py 와 같은 jipsa.db 를 공유한다.
"""
from __future__ import annotations

import json
import time
import uuid
import sqlite3
from pathlib import Path

BASE = Path.home() / '.claude/scripts/slack-jipsa'
DB_PATH = BASE / 'jipsa.db'          # 테스트가 교체하는 전역


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def request_approval(task_id: str, channel_id: str, action_desc: str,
                     approvers: list[str], timeout_min: int, thread_ts: str = '') -> str:
    """approvals 행을 status=대기 로 INSERT 하고 단일사용 토큰 반환."""
    token = str(uuid.uuid4())
    now = int(time.time())
    with _conn() as c:
        c.execute('INSERT INTO approvals (token,task_id,channel_id,action_desc,status,'
                  'approver_id,approvers,requested_at,decided_at,expires_at,thread_ts) '
                  'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  (token, task_id, channel_id, action_desc, '대기', '',
                   json.dumps(approvers, ensure_ascii=False), now, 0,
                   now + max(0, int(timeout_min)) * 60, thread_ts))
    return token


def get_approval(token: str) -> dict | None:
    with _conn() as c:
        r = c.execute('SELECT * FROM approvals WHERE token=?', (token,)).fetchone()
    return dict(r) if r else None


def decide(token: str, approver_id: str, approve: bool = True) -> str:
    """버튼 클릭 verdict 적용(compare-and-set).

    반환: '승인' | '거부' | '권한없음' | '이미처리' | '만료' | '없음'
    """
    now = int(time.time())
    with _conn() as c:
        r = c.execute('SELECT * FROM approvals WHERE token=?', (token,)).fetchone()
        if not r:
            return '없음'
        if r['status'] != '대기':
            return '이미처리' if r['status'] in ('승인', '거부') else r['status']
        if now >= r['expires_at']:
            c.execute('UPDATE approvals SET status=? WHERE token=? AND status=?',
                      ('만료', token, '대기'))
            return '만료'
        allowed = set(json.loads(r['approvers'] or '[]'))
        if approver_id not in allowed:
            return '권한없음'
        verdict = '승인' if approve else '거부'
        # CAS: status=대기 일 때만 갱신 → 동시 클릭 시 첫 verdict만 채택
        cur = c.execute('UPDATE approvals SET status=?, approver_id=?, decided_at=? '
                        'WHERE token=? AND status=?',
                        (verdict, approver_id, now, token, '대기'))
        if cur.rowcount == 0:
            return '이미처리'
    return verdict


def expire_stale() -> int:
    """기한 지난 대기 행을 만료 처리. 만료된 건수 반환(요청자 알림용)."""
    now = int(time.time())
    with _conn() as c:
        cur = c.execute('UPDATE approvals SET status=? WHERE status=? AND expires_at <= ?',
                        ('만료', '대기', now))
        return cur.rowcount


def list_expired_since(since_ts: int) -> list[dict]:
    """sweeper가 알림 보낼 대상: 최근 만료된 행."""
    with _conn() as c:
        rows = c.execute('SELECT * FROM approvals WHERE status=? AND expires_at >= ?',
                         ('만료', since_ts)).fetchall()
    return [dict(r) for r in rows]


def build_card(token: str, action_desc: str) -> list:
    """슬랙 Block Kit 승인 카드. 버튼 value 에 token 바인딩."""
    return [
        {'type': 'section', 'text': {'type': 'mrkdwn',
            'text': f'🔐 *승인 요청*\n```{action_desc[:500]}```\n_승인하면 실행, 거부하면 차단합니다._'}},
        {'type': 'actions', 'block_id': f'gate_{token}', 'elements': [
            {'type': 'button', 'style': 'primary', 'text': {'type': 'plain_text', 'text': '✅ 승인'},
             'action_id': 'gate_approve', 'value': token},
            {'type': 'button', 'style': 'danger', 'text': {'type': 'plain_text', 'text': '⛔ 거부'},
             'action_id': 'gate_reject', 'value': token},
        ]},
    ]

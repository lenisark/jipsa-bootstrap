"""jipsa 2.0 작업 객체(Task Layer) — stdlib sqlite3 단일 파일 store.

- 휘발성 슬랙 대화를 상태 가진 task로 승격(대기/진행/막힘/완료/취소).
- 승인 게이트(approval.py)의 랑데부 지점이라 원자성 필수 → SQLite(WAL).
- daemon.py 의 형제 모듈. `import tasks` 로 로드(reminders.py 와 동일 패턴).
"""
from __future__ import annotations

import json
import re
import time
import uuid
import sqlite3
from pathlib import Path
from contextlib import closing

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
    with closing(_conn()) as c, c:
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
    with closing(_conn()) as c, c:
        c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  (tid, channel_id, title, body, '대기', direction, assignee,
                   thread_ts, now, now, json.dumps(meta or {}, ensure_ascii=False)))
    return tid


def get_task(task_id: str) -> dict | None:
    with closing(_conn()) as c, c:
        r = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    return dict(r) if r else None


def list_tasks(channel_id: str, states: tuple[str, ...] | None = None) -> list[dict]:
    q = 'SELECT * FROM tasks WHERE channel_id=?'
    args: list = [channel_id]
    if states:
        q += ' AND state IN (%s)' % ','.join('?' * len(states))
        args += list(states)
    q += ' ORDER BY created_at DESC'
    with closing(_conn()) as c, c:
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
    with closing(_conn()) as c, c:
        c.execute('UPDATE tasks SET state=?, updated_at=? WHERE id=?',
                  (state, int(time.time()), task_id))
    return True


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k}=?' for k in fields)
    with closing(_conn()) as c, c:
        c.execute(f'UPDATE tasks SET {cols}, updated_at=? WHERE id=?',
                  (*fields.values(), int(time.time()), task_id))


# ── 요약 → 미결 과제 (2026-09 업그레이드) ──────────────────────────
TODO_OPEN, TODO_CLOSE = '<<<TODO', 'TODO>>>'


def split_todo_block(text: str) -> tuple[str, list[dict]]:
    """요약 출력에서 <<<TODO [json] TODO>>> 블록을 떼어 (본문, 과제목록) 반환.
    블록이 없거나 깨졌으면 과제는 빈 목록(본문은 블록만 제거)."""
    i = text.find(TODO_OPEN)
    if i < 0:
        return text.strip(), []
    j = text.find(TODO_CLOSE, i)
    raw = text[i + len(TODO_OPEN):j if j >= 0 else len(text)]
    body = (text[:i] + (text[j + len(TODO_CLOSE):] if j >= 0 else '')).strip()
    raw = raw.strip().strip('`').removeprefix('json').strip()
    try:
        items = json.loads(raw)
    except Exception:
        return body, []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and str(it.get('title', '')).strip():
            out.append({'title': str(it['title']).strip()[:120],
                        'owner': str(it.get('owner') or '').strip()[:30],
                        'due': str(it.get('due') or '').strip()[:20]})
    return body, out


def _norm(s: str) -> str:
    return re.sub(r'\s+', '', s or '')


def add_open_items(channel_id: str, items: list[dict], source: str) -> list[dict]:
    """제목 기준 중복(상태 무관 — 닫힌 과제도 되살리지 않음)을 거르고 새 과제 등록.
    등록된 것만 [{'id','title','owner','due'}] 로 반환."""
    seen = {_norm(r['title']) for r in list_tasks(channel_id)}
    made = []
    for it in items:
        key = _norm(it['title'])
        if not key or key in seen:
            continue
        seen.add(key)
        tid = create_task(channel_id, it['title'], direction='a2h',
                          assignee=it.get('owner') or '',
                          meta={'source': source, 'due': it.get('due', ''), 'msg_ts': []})
        made.append({'id': tid, **it})
    return made


def add_msg_ts(task_id: str, ts: str) -> None:
    t = get_task(task_id)
    if not t:
        return
    meta = json.loads(t.get('meta') or '{}')
    meta.setdefault('msg_ts', []).append(ts)
    update_task(task_id, meta=json.dumps(meta, ensure_ascii=False))


def find_open_by_msg(channel_id: str, ts: str) -> dict | None:
    """과제 안내 메시지(ts)에 ✅ 눌렀을 때 해당 열린 과제 찾기."""
    for r in list_tasks(channel_id, states=('대기', '진행', '막힘')):
        if ts in (json.loads(r.get('meta') or '{}').get('msg_ts') or []):
            return r
    return None


def close_task(task_id: str) -> bool:
    """대기/막힘 → 진행 → 완료 (상태기계 규칙 그대로 두고 두 단계로)."""
    t = get_task(task_id)
    if not t:
        return False
    if t['state'] in ('대기', '막힘'):
        set_state(task_id, '진행')
    return set_state(task_id, '완료')


_EXPIRY_HEAD = re.compile(r'^\[(계약 만료 임박|차량 .{1,12} 임박)\]')


def expiry_title(text: str) -> str | None:
    """알리미 봇의 '[계약 만료 임박] … 경과' 메시지 → 과제 제목. 경과가 아니면 None."""
    t = text or ''
    if not _EXPIRY_HEAD.match(t) or '경과' not in t:
        return None
    t = re.sub(r'<[^>|]+\|([^>]+)>', '', t)          # 슬랙 링크 제거
    name = re.search(r'\*([^*]+)\*\s*(\([^)]*\))?', t)
    what = name.group(1).strip() if name else t[:40]
    if t.startswith('[차량'):
        kind = re.search(r'·\s*(보험 만료|정기검사)', t)
        what += f" {kind.group(1)}" if kind else ''
    elif name and name.group(2):
        what += f" {name.group(2)}"
    return f"[만료 경과] {what} — 포털에서 갱신/종료 처리"

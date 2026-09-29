#!/usr/bin/env python3
"""Claude Code Stop hook — 세션 턴 종료 시 Slack 보고 (Windows용 Python 버전, slack-session-summary.sh 와 같은 동작).

노션 적재는 미포함(필요하면 .sh 의 Notion 부분을 옮길 것). 후속 턴 스레드 묶기·잡음 거르기는 .sh 와 동일.
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

if os.environ.get('CLAUDE_SKIP_SUMMARY') == '1' or os.environ.get('CLAUDE_SKIP_HOOKS') == '1':
    sys.exit(0)
if os.environ.get('SLACK_HOOK_RUNNING'):
    sys.exit(0)
os.environ['SLACK_HOOK_RUNNING'] = '1'

SECRETS = Path.home() / '.claude/secrets/slack-jipsa.env'
env = {}
if SECRETS.exists():
    for line in SECRETS.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()

SLACK_URL = os.environ.get('SLACK_SESSION_WEBHOOK') or env.get('SLACK_SESSION_WEBHOOK', '')
NOTION_TOKEN = os.environ.get('NOTION_API_TOKEN') or env.get('NOTION_API_TOKEN', '')
NOTION_DB = os.environ.get('NOTION_SESSION_DB') or env.get('NOTION_SESSION_DB', '')
# 봇 토큰 + 채널이 있으면 chat.postMessage 로 보내 같은 요청의 후속 보고를 스레드로 묶는다(없으면 웹훅).
BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN') or env.get('SLACK_BOT_TOKEN', '')
SESSION_CH = (os.environ.get('SLACK_SESSION_CHANNEL') or env.get('SLACK_SESSION_CHANNEL', '')
              or env.get('SLACK_CHANNEL', ''))
THREAD_DIR = Path.home() / '.claude/scripts/slack-jipsa/session_threads'

if not SLACK_URL and not (BOT_TOKEN and SESSION_CH) and not NOTION_TOKEN:
    sys.exit(0)

try:
    stdin_data = sys.stdin.read()
    stdin_json = json.loads(stdin_data)
except Exception:
    sys.exit(0)

session_id = stdin_json.get('session_id', '')
cwd = stdin_json.get('cwd', '') or str(Path.cwd())
if not session_id:
    sys.exit(0)

project_name = Path(cwd).name

projects_dir = Path.home() / '.claude/projects'
transcript_files = list(projects_dir.glob(f'**/{session_id}.jsonl'))
if not transcript_files:
    sys.exit(0)
transcript = transcript_files[0]

def get_text_content(content):
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        return '\n'.join(
            item.get('text', '')
            for item in content
            if isinstance(item, dict) and item.get('type') == 'text'
        )
    return ''

def is_real_user(entry):
    if entry.get('type') != 'user':
        return False
    txt = get_text_content(entry.get('message', {}).get('content', '')).strip()
    if not txt:
        return False
    if txt.startswith('<task-notification>'):
        return False
    return True

entries = []
try:
    for line in transcript.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
except Exception:
    sys.exit(0)

ua_entries = [e for e in entries if e.get('type') in ('user', 'assistant')]
last_user_idx = None
for i, e in enumerate(ua_entries):
    if is_real_user(e):
        last_user_idx = i

if last_user_idx is None:
    sys.exit(0)

turn = ua_entries[last_user_idx:]
user_prompt = get_text_content(turn[0].get('message', {}).get('content', ''))[:200]

tool_names = []
assistant_texts = []
for e in turn:
    if e.get('type') == 'assistant':
        content = e.get('message', {}).get('content', [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get('type') == 'tool_use':
                        tool_names.append(item.get('name', '?'))
                    elif item.get('type') == 'text':
                        t = item.get('text', '')
                        if t:
                            assistant_texts.append(t)
        elif isinstance(content, str) and content:
            assistant_texts.append(content)

tool_counts = {}
tool_order = []
for name in tool_names:
    if name not in tool_counts:
        tool_order.append(name)
        tool_counts[name] = 0
    tool_counts[name] += 1

actions_md = ', '.join(
    f'{n} x{tool_counts[n]}' if tool_counts[n] > 1 else n
    for n in tool_order
) or '(도구 없음)'

result_txt = (assistant_texts[-1] if assistant_texts else '')[:200]
tool_count = len(tool_names)

if not user_prompt and tool_count == 0:
    sys.exit(0)

# 잡음 거르기 — 다른 세션·서브에이전트가 보낸 메시지로 돈 턴, 도구 없이 짧게 답한 턴("네, 알려드릴게요")
if user_prompt.startswith('Another Claude session sent a message') or '<agent-message' in user_prompt:
    sys.exit(0)
if tool_count == 0 and len(assistant_texts[-1] if assistant_texts else '') < 200:
    sys.exit(0)

LOG_PATH = Path(os.environ.get('HOOK_LOG', str(Path(tempfile.gettempdir()) / 'slack-session-summary.log')))
KST = timezone(timedelta(hours=9))
now_kst = datetime.now(KST)
ts_hm = now_kst.strftime('%H:%M')

def log(msg):
    try:
        with open(str(LOG_PATH), 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass

log(f'hook start session={session_id} project={project_name} tool_count={tool_count}')

slack_body = (
    f"🎯 *시킨 일*\n{user_prompt}\n\n"
    f"📝 *한 일*\n{actions_md}\n\n"
    f"🧠 *결과*\n{result_txt}\n\n"
    f"⚠️ *확인 필요*\n없음"
)
payload = {
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": f"🤖 {project_name}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"⏰ {ts_hm} KST  ·  세션 `{session_id[:8]}`"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": slack_body}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"📁 `{cwd}`"}]},
        {"type": "divider"}
    ],
    "text": f"Claude 턴 · {project_name}"
}


def slack_api(method, body):
    req = urllib.request.Request(
        f'https://slack.com/api/{method}',
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8',
                 'Authorization': f'Bearer {BOT_TOKEN}'},
        method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b'{}')


# 같은 사용자 요청(= 마지막 실제 사용자 입력)에서 이어진 턴이면 첫 보고의 스레드로. 세션당 파일 1개라 동시 세션끼리 안 겹침.
turn_key = turn[0].get('uuid') or user_prompt
state_file = THREAD_DIR / f'{session_id}.json'
if BOT_TOKEN and SESSION_CH:
    try:
        state = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    except Exception:
        state = {}
    try:
        if state.get('key') == turn_key and state.get('ts'):
            n = int(state.get('n', 0)) + 1
            slack_api('chat.postMessage', {
                'channel': SESSION_CH, 'thread_ts': state['ts'],
                'text': f"🔁 *후속 보고 {n}* · ⏰ {ts_hm}\n📝 {actions_md}\n🧠 {result_txt}"})
            # 채널에서도 최신 결과가 보이게 첫 보고 아래 한 줄 갱신
            blocks = state['blocks'] + [{"type": "context", "elements": [{"type": "mrkdwn",
                     "text": f"🔁 후속 {n}건 · 마지막 {ts_hm} — {result_txt[:120]}"}]}]
            slack_api('chat.update', {'channel': SESSION_CH, 'ts': state['ts'],
                                      'blocks': blocks, 'text': payload['text']})
            state['n'] = n
            log(f'slack thread reply n={n}')
        else:
            res = slack_api('chat.postMessage', {'channel': SESSION_CH, **payload})
            if not res.get('ok'):
                raise RuntimeError(res.get('error'))
            state = {'key': turn_key, 'ts': res['ts'], 'blocks': payload['blocks'], 'n': 0}
            log('slack posted (bot)')
        THREAD_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        for old in THREAD_DIR.glob('*.json'):          # 7일 지난 세션 상태 정리
            if old.stat().st_mtime < now_kst.timestamp() - 7 * 86400:
                old.unlink(missing_ok=True)
        sys.exit(0)
    except Exception as e:
        log(f'slack bot error: {e} — 웹훅으로 대신 보냄')

if SLACK_URL:
    try:
        req = urllib.request.Request(
            SLACK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f'slack sent status={r.status}')
    except Exception as e:
        log(f'slack error: {e}')

sys.exit(0)

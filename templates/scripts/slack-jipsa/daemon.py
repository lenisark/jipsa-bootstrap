#!/usr/bin/env python3
"""
Slack ↔ Claude Code daemon (Agent Bootstrap)

흐름:
1. Socket Mode로 슬랙 채널 메시지 실시간 수신
2. 사용자 메시지면 → ⏳ reaction → claude --print --resume <session_id> 호출
3. 응답 → 채널 메인에 post → ⏳ 제거 + ✅ reaction
4. (옵션) 한 턴을 노션 'Claude Code 턴 로그' DB에 적재

세션 유지:
- 채널별 session_id를 ~/.claude/scripts/slack-jipsa/sessions/{channel}.txt 에 저장
- 첫 메시지: --session-id <uuid> 로 새 세션 시작
- 이후: --resume <session_id> 로 같은 세션 이어감

검증된 코드를 일반 배포용으로 다음 항목만 환경변수화:
- NOTION_SESSION_DB / NOTION_DAILY_DB (env, 비어있으면 노션 적재 skip)
- USER_SLACK_ID (구 MIRI_USER_ID alias 지원)
- USER_NAME (시스템 프롬프트, 기본 '사용자')
- SLACK_BOT_NAME (노션 프로젝트 컬럼명, 기본 '슬랙 비서')
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import subprocess
import threading
from pathlib import Path

# fcntl은 Unix 전용. Windows에선 None.
try:
    import fcntl  # type: ignore
except ImportError:
    fcntl = None  # type: ignore

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# 알리미 (매월 반복 알림). 로드 실패해도 채팅은 동작하도록 방어.
try:
    import reminders as rmd
except Exception:
    rmd = None

# 작업 객체(Task Layer, jipsa 2.0). 로드 실패해도 채팅은 동작하도록 방어.
try:
    import tasks as tsk
    tsk.init_db()
except Exception:
    tsk = None

# 비품관리(jipsa supply). 설정 없으면 비활성.
try:
    import supply as sply
except Exception:
    sply = None

SECRETS = Path.home() / '.claude/secrets/slack-jipsa.env'
SESSIONS_DIR = Path.home() / '.claude/scripts/slack-jipsa/sessions'
LOGS_DIR = Path.home() / '.claude/scripts/slack-jipsa/logs'
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Notion DB IDs — 사용자 .env에서 받음. 비어있으면 노션 적재 skip.
NOTION_SESSION_DB = ''   # set after load_env()
NOTION_DAILY_DB = ''     # set after load_env() (optional)
sys.path.insert(0, str(Path.home() / '.claude/scripts'))

# 공유 대화 버퍼 (클코 + 코덱스 둘 다 read/write)
SHARED_DIR = Path.home() / '.claude/scripts/slack-jipsa-shared'
SHARED_DIR.mkdir(parents=True, exist_ok=True)
SHARED_BUFFER_LIMIT = 30


def shared_buffer_path(channel: str, thread_ts: str = '') -> Path:
    key = f'slack_{channel}_{thread_ts or "root"}'
    return SHARED_DIR / f'{key}.jsonl'


def load_shared(channel: str, thread_ts: str = '') -> list[dict]:
    p = shared_buffer_path(channel, thread_ts)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines()[-SHARED_BUFFER_LIMIT:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def append_shared(channel: str, thread_ts: str, who: str, text: str, msg_ts: str = '') -> None:
    """공유 버퍼에 추가. msg_ts는 Slack event/post ts이며 중복 방지 키다."""
    p = shared_buffer_path(channel, thread_ts)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'ts': time.time(),
        'msg_ts': msg_ts,
        'who': who,
        'text': text[:2000],
    }
    with p.open('a+', encoding='utf-8') as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if msg_ts:
                f.seek(0)
                for line in f.read().splitlines()[-50:]:
                    try:
                        if json.loads(line).get('msg_ts') == msg_ts:
                            return
                    except Exception:
                        pass
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_env() -> dict[str, str]:
    env = {}
    for line in SECRETS.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
BOT_TOKEN = ENV['SLACK_BOT_TOKEN']
APP_TOKEN = ENV['SLACK_APP_TOKEN']
CHANNEL = ENV['SLACK_CHANNEL']
CHANNEL_DIALOG = ENV.get('SLACK_CHANNEL_DIALOG', '')  # 두 봇 대화 채널 (옵션)
# USER_SLACK_ID = 봇이 응답할 대상 사용자. (구 변수명 MIRI_USER_ID alias 지원)
MIRI = ENV.get('USER_SLACK_ID') or ENV.get('MIRI_USER_ID', '')
BOT = ENV['BOT_USER_ID']
USER_NAME = ENV.get('USER_NAME', '사용자')
BOT_NAME = ENV.get('SLACK_BOT_NAME', '슬랙 비서')
DIALOG_TURN_LIMIT = 6  # 대화 채널에서 봇 자기 응답 최대 N턴 (무한루프 방지)

# Notion DB IDs from .env (set globals declared above)
NOTION_SESSION_DB = ENV.get('NOTION_SESSION_DB', '')
NOTION_DAILY_DB = ENV.get('NOTION_DAILY_DB', '')

# 단톡 토론 모드 트리거: 사용자 발화에 매치되면 봇끼리 자유 응답 허용
DISCUSSION_TRIGGER = re.compile(
    r'(토론|비교|반박|의견\s*(나눠|줘|얘기|교환)|각자\s*의견|둘이|서로\s*의견)',
    re.IGNORECASE,
)
# 토론 종료 신호
DISCUSSION_STOP = re.compile(
    r'(\b그만\b|\b종료\b|\bstop\b|\b끝\b|\b정리\b|\b중단\b|토론\s*그만|토론\s*종료)',
    re.IGNORECASE,
)

# 대화 요약 요청 트리거 (팀 협업)
SUMMARY_TRIGGER = re.compile(
    r'(대화|채팅|채널|오늘|회의|스레드|논의).{0,10}(요약|정리)'
    r'|요약\s*(해줘|해|좀)'
)

# 도움말 트리거
HELP_TRIGGER = re.compile(r'(도움말|사용법|명령어|help|뭐\s*할\s*수\s*있|어떻게\s*써)', re.IGNORECASE)

# 투표/설문 트리거 + 번호 이모지
POLL_TRIGGER = re.compile(r'(투표|설문)')
POLL_EMOJI = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣']
POLL_NAME = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']
POLLS_FILE = Path.home() / '.claude/scripts/slack-jipsa/polls.json'

web = WebClient(token=BOT_TOKEN)
sock = SocketModeClient(app_token=APP_TOKEN, web_client=web)


def log(msg: str) -> None:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    today = time.strftime('%Y-%m-%d')
    (LOGS_DIR / f'{today}.log').open('a', encoding='utf-8').write(line + '\n')


# ── 다채널 설정 (channels.json) ──────────────────────────────────────
# 채널별로 모델·접근정책·작업폴더·접근허용 디렉토리를 분리한다.
#   model:    opus | sonnet | haiku  (claude --model 값)
#   access:   'owner' = 소유자(USER_SLACK_ID)만, 'all' = 채널의 모든 사람(팀)
#   cwd:      claude 실행 디렉토리 (이 폴더의 CLAUDE.md = 채널 페르소나/규칙)
#   add_dirs: 추가로 접근 허용할 디렉토리 목록. 비우면 cwd만 접근 가능(샌드박스).
CHANNELS_CONFIG_FILE = Path.home() / '.claude/scripts/slack-jipsa/channels.json'


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


# 런타임 오버라이드 (슬랙 `저장폴더` 명령으로 바뀐 add_dirs 등). channels.json 과 분리 보관.
OVERRIDES_FILE = Path.home() / '.claude/scripts/slack-jipsa/channel_overrides.json'


def _load_overrides() -> dict:
    try:
        if OVERRIDES_FILE.exists():
            return json.loads(OVERRIDES_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_channel_override(channel: str, patch: dict) -> None:
    d = _load_overrides()
    d.setdefault(channel, {}).update(patch)
    OVERRIDES_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')


def load_channels() -> dict[str, dict]:
    """channels.json 로드. 없거나 비면 legacy 단일 채널(opus/owner/홈)로 fallback."""
    default_cwd = str(Path.home() / '.claude/scripts/slack-jipsa')
    if CHANNELS_CONFIG_FILE.exists():
        try:
            data = json.loads(CHANNELS_CONFIG_FILE.read_text(encoding='utf-8'))
            out = {}
            for ch, cfg in (data or {}).items():
                out[ch] = {
                    'label': cfg.get('label', ch),
                    'model': cfg.get('model') or 'opus',
                    'access': cfg.get('access') or 'owner',
                    'cwd': _expand(cfg.get('cwd') or default_cwd),
                    'add_dirs': [_expand(d) for d in (cfg.get('add_dirs') or [])],
                    'disallowed_tools': list(cfg.get('disallowed_tools') or []),
                    'require_mention': bool(cfg.get('require_mention', False)),
                    'mission': cfg.get('mission', ''),                       # 2.0: 페르소나
                    'tasks_enabled': bool(cfg.get('tasks_enabled', False)),  # 2.0: 작업 객체
                    'gate': cfg.get('gate') or {},                          # 2.0: 승인 게이트(비면 off)
                    # 2.0: `저장폴더` 명령으로 설정 가능한 출력 폴더의 허용 상위 루트
                    'output_roots': [_expand(d) for d in (cfg.get('output_roots') or [])],
                    # 2.0: 기본 저장 위치(시스템 프롬프트로 주입). `저장폴더` 명령으로 갱신.
                    'output_dir': _expand(cfg.get('output_dir') or ''),
                }
            # 런타임 오버라이드 병합 (슬랙에서 바꾼 add_dirs/output_dir — 재시작에도 유지)
            for ch, patch in _load_overrides().items():
                if ch in out and isinstance(patch, dict):
                    if patch.get('add_dirs'):
                        out[ch]['add_dirs'] = [_expand(d) for d in patch['add_dirs']]
                    if patch.get('output_dir'):
                        out[ch]['output_dir'] = _expand(patch['output_dir'])
            if out:
                return out
            log('channels.json 비어있음 — legacy 단일 채널로 fallback')
        except Exception as e:
            log(f'channels.json 파싱 실패 ({e}) — legacy 단일 채널로 fallback')
    return {CHANNEL: {'label': '개인 비서', 'model': 'opus', 'access': 'owner',
                      'cwd': default_cwd, 'add_dirs': [str(Path.home())],
                      'disallowed_tools': [], 'mission': '',
                      'tasks_enabled': False, 'gate': {}, 'output_roots': [],
                      'output_dir': ''}}


CHANNELS = load_channels()
log('채널 설정 로드: ' + ', '.join(
    f'{c}({v["model"]}/{v["access"]})' for c, v in CHANNELS.items()))


def session_path(channel: str) -> Path:
    return SESSIONS_DIR / f'{channel}.txt'


def get_or_create_session(channel: str) -> tuple[str, bool]:
    """채널의 session_id 반환. 없으면 새로 생성. (id, is_new)"""
    p = session_path(channel)
    if p.exists():
        sid = p.read_text(encoding='utf-8').strip()
        if sid: return sid, False
    sid = str(uuid.uuid4())
    p.write_text(sid)
    return sid, True


def reset_session(channel: str) -> str:
    """세션 리셋. 새 session_id 생성."""
    sid = str(uuid.uuid4())
    session_path(channel).write_text(sid)
    return sid


SYSTEM_PROMPT = f"""당신은 {USER_NAME}님의 슬랙 집사 '{BOT_NAME}'입니다.

**필수**: cwd `~/.claude/scripts/slack-jipsa/`의 CLAUDE.md를 절대 규칙으로 따르세요.
페르소나, 슬랙 mrkdwn, 도구 호출 제한, 일정/가계부/캘린더 필터 규칙 모두 거기 있습니다.

규칙 어기면 {USER_NAME}님이 직접 지적합니다. 같은 실수 반복 금지."""


def _run_claude(prompt: str, session_id: str, is_new: bool, timeout: int,
                model: str = 'opus', cwd: str | None = None,
                add_dirs: list[str] | None = None,
                disallowed_tools: list[str] | None = None,
                gate: dict | None = None, gate_channel: str = '',
                gate_thread: str = '', output_dir: str = '') -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if not gate:
        env['CLAUDE_SKIP_HOOKS'] = '1'          # 기존 동작: 훅 전부 끔
    else:
        # 게이트 채널: PreToolUse 훅 켜고(.claude/settings.json) 게이트 컨텍스트 주입
        env.pop('CLAUDE_SKIP_HOOKS', None)
        env['JIPSA_GATE_CHANNEL'] = gate_channel or ''
        env['JIPSA_GATE_THREAD'] = gate_thread or ''
        env['JIPSA_GATE_APPROVERS'] = ','.join(gate.get('approvers', []))
        env['JIPSA_GATE_TIMEOUT_MIN'] = str(gate.get('timeout_min', 15))
        env['JIPSA_GATE_MODE'] = gate.get('mode', 'block')   # block(개인) | escalate(부서)
    sysprompt = SYSTEM_PROMPT
    if output_dir:
        sysprompt += (f"\n\n[저장 위치] 이 채널에서 새로 만드는 파일은 특별한 경로 "
                      f"지정이 없으면 `{output_dir}` 폴더 안에 저장하세요.")
    cmd = [
        'claude', '--print',
        '--permission-mode', 'bypassPermissions',
        '--dangerously-skip-permissions',
        '--output-format', 'text',
        '--model', model,
        '--append-system-prompt', sysprompt,
    ]
    # 채널별 접근 허용 디렉토리. 비우면 cwd만 접근 가능(샌드박스).
    for d in (add_dirs or []):
        cmd.extend(['--add-dir', d])
    # 채널별 비활성 도구 (부서 채널: Bash/Write/Edit 차단 → Q&A·읽기 전용).
    if disallowed_tools:
        cmd.extend(['--disallowedTools'] + list(disallowed_tools))
    cmd.extend(['--session-id', session_id] if is_new else ['--resume', session_id])
    # cwd → 해당 폴더의 CLAUDE.md 자동 로드 → 채널별 페르소나/규칙 적용
    run_cwd = cwd or str(Path.home() / '.claude/scripts/slack-jipsa')
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          encoding='utf-8', env=env, cwd=run_cwd, timeout=timeout)


def call_claude(prompt: str, channel: str, timeout: int = 900, thread_ts: str = '') -> str:
    """클로드 코드 호출. resume 실패 시 자동으로 새 session 재시도."""
    sid, is_new = get_or_create_session(channel)
    cfg = CHANNELS.get(channel, {})
    model = cfg.get('model', 'opus')
    cwd = cfg.get('cwd')
    add_dirs = cfg.get('add_dirs')
    disallowed = cfg.get('disallowed_tools')
    # 승인 게이트: 개인 차단(block) + 부서 권한상승(escalate) 둘 다 지원.
    gate_cfg = cfg.get('gate') or {}
    use_gate = bool(gate_cfg.get('enabled'))
    gate_arg = gate_cfg if use_gate else None
    # escalate: 평소 disallowed인 민감 도구를 풀어 호출 가능케 하되 게이트로 통제.
    if use_gate and gate_cfg.get('mode') == 'escalate':
        sensitive = set(gate_cfg.get('sensitive_tools') or [])
        disallowed = [t for t in (disallowed or []) if t not in sensitive]
    out_dir = cfg.get('output_dir', '')
    try:
        r = _run_claude(prompt, sid, is_new, timeout, model, cwd, add_dirs, disallowed,
                        gate=gate_arg, gate_channel=channel, gate_thread=thread_ts,
                        output_dir=out_dir)
        # resume 실패 (jsonl 없음) → 새 session으로 재시도
        if r.returncode != 0 and not is_new and 'No conversation found' in (r.stderr or ''):
            log(f'  resume fail, fallback to new session')
            new_sid = reset_session(channel)
            r = _run_claude(prompt, new_sid, True, timeout, model, cwd, add_dirs, disallowed,
                            gate=gate_arg, gate_channel=channel, gate_thread=thread_ts,
                            output_dir=out_dir)
    except subprocess.TimeoutExpired:
        return f'⏱️ 타임아웃 ({timeout}초). 작업이 너무 길어요.'
    if r.returncode != 0:
        # 슬랙에 fail 메시지 보내지 않음 (잡음). stderr는 로그로만.
        log(f'  claude fail rc={r.returncode}: {(r.stderr or "")[-300:]}')
        return '__SILENT_FAIL__'
    return (r.stdout or '').strip()


# 스케줄 작업이 '채널 대화 요약' 의도인지 (그러면 히스토리 주입 필요)
_CONV_SUMMARY = re.compile(r'(대화|채팅|채널|회의|스레드|메시지|논의).{0,8}(요약|정리|분석|브리핑|정리)')
# '주간/지난주 요약' 의도인지 (그러면 최근 7일치를 정확히 수집)
_WEEK_SUMMARY = re.compile(r'(지난\s*주|저번\s*주|한\s*주|일주일|이번\s*주|주간|최근\s*7\s*일|7일)')


def _is_reply(m: dict) -> bool:
    """스레드 답글이면 True (부모 메시지는 thread_ts == ts)."""
    tt = m.get('thread_ts')
    return bool(tt and tt != m.get('ts'))


def _expand_with_replies(channel: str, messages: list) -> list:
    """conversations_history 결과(최신순)를 과거→현재 순으로 뒤집고,
    각 스레드 부모 뒤에 답글(replies)을 시간순으로 끼워넣어 평면화한 리스트 반환.

    conversations.history는 top-level/부모 메시지만 주므로, 답글을 포함하려면
    부모마다 conversations.replies를 별도 호출해야 한다(호출 수 = 답글 달린 부모 수).
    """
    out = []
    for m in reversed(messages):          # 과거→현재
        out.append(m)
        rc = m.get('reply_count') or 0
        ts = m.get('ts')
        if rc and ts and m.get('thread_ts', ts) == ts:
            try:
                rr = web.conversations_replies(channel=channel, ts=ts, limit=200)
            except Exception as e:
                log(f'  replies fetch fail ({ts}): {e}')
                continue
            for rm in rr.get('messages', []):
                if rm.get('ts') == ts:    # 첫 항목은 부모(중복) → 제외
                    continue
                out.append(rm)
    return out


def _collect_channel_text(channel: str, limit: int = 300, since_days: int | None = None) -> str:
    """채널 최근 메시지를 '[MM-DD HH:MM] 이름: 내용' 형식으로 수집(시각 포함).
    스레드 답글은 부모 뒤에 시간순으로 '↳' 표시와 함께 포함.

    since_days 지정 시: 최근 N일(오늘 기준 00:00 KST부터)을 페이지네이션으로 정확히 수집
    (주간 요약용 — 최근 300개 한도에 걸려 앞부분이 잘리는 문제 방지)."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    kst = _tz(_td(hours=9))
    try:
        if since_days:
            now = _dt.now(kst)
            oldest_dt = (now - _td(days=since_days)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            oldest = f'{oldest_dt.timestamp():.6f}'
            raw = []
            cursor = None
            for _ in range(12):          # 최대 12페이지(≈2400건) 안전장치
                kw = {'channel': channel, 'limit': 200, 'oldest': oldest}
                if cursor:
                    kw['cursor'] = cursor
                resp = web.conversations_history(**kw)
                raw.extend(resp.get('messages', []))
                cursor = (resp.get('response_metadata') or {}).get('next_cursor')
                if not cursor:
                    break
        else:
            resp = web.conversations_history(channel=channel, limit=limit)
            raw = resp.get('messages', [])
    except Exception as e:
        log(f'  history fetch fail: {e}')
        return ''
    lines = []
    for m in _expand_with_replies(channel, raw):
        if m.get('subtype') or m.get('user') == BOT:
            continue
        txt = (m.get('text') or '').strip()
        if not txt:
            continue
        nm = _resolve_name(m.get('user') or m.get('bot_id') or '?')
        mark = '↳ ' if _is_reply(m) else ''
        try:
            stamp = _dt.fromtimestamp(float(m.get('ts', '0')), kst).strftime('%m-%d %H:%M')
            lines.append(f'[{stamp}] {mark}{nm}: {txt}')
        except Exception:
            lines.append(f'{mark}{nm}: {txt}')
    cap = 20000 if since_days else 8000
    return '\n'.join(lines)[-cap:]


def run_scheduled_action(channel: str, prompt: str) -> str | None:
    """알리미 2.0 실행기 — 스케줄된 작업을 새 세션으로 실행하고 결과 반환.

    - 새 세션(대화 오염 방지) · 게이트 없음(스케줄은 알림 생성 시 사전승인된 것).
    - 채널의 disallowed_tools 유지 → 부서 채널은 읽기전용(요약 등), 개인은 풀권한.
    - '대화 요약' 의도면 채널 히스토리를 가져와 프롬프트에 주입(claude는 슬랙 접근 불가).
    """
    cfg = CHANNELS.get(channel, {})
    effective = prompt
    if _CONV_SUMMARY.search(prompt):
        days = 7 if _WEEK_SUMMARY.search(prompt) else None
        convo = _collect_channel_text(channel, since_days=days)
        if convo:
            effective = (
                "다음은 이 슬랙 채널의 최근 대화 기록입니다(시각은 KST). "
                "이를 근거로 아래 지시를 수행하세요. 기록에 없는 내용은 지어내지 마세요.\n\n"
                f"[대화 기록]\n{convo}\n\n[지시]\n{prompt}")
        else:
            effective = (f"{prompt}\n\n"
                         "(참고: 이 채널의 최근 대화 기록을 가져오지 못했습니다 — "
                         "히스토리 읽기 권한이 없을 수 있어요. 그 사실만 간단히 보고하세요.)")
    try:
        r = _run_claude(effective, str(uuid.uuid4()), True, 600,
                        cfg.get('model', 'opus'), cfg.get('cwd'),
                        cfg.get('add_dirs'), cfg.get('disallowed_tools'),
                        output_dir=cfg.get('output_dir', ''))
    except Exception as e:
        log(f'  scheduled action fail: {e}')
        return None
    if r.returncode != 0:
        log(f'  scheduled action rc={r.returncode}: {(r.stderr or "")[-200:]}')
        return None
    out = (r.stdout or '').strip()
    if not out:
        return None
    try:
        sys.path.insert(0, str(Path.home() / '.claude/scripts'))
        from lib.slack_mrkdwn import to_mrkdwn
        return to_mrkdwn(out)
    except Exception:
        return out


# ── 대화 요약 (팀 협업) ────────────────────────────────────────────
_name_cache: dict[str, str] = {}


def _resolve_name(uid: str) -> str:
    if not uid:
        return '?'
    if uid in _name_cache:
        return _name_cache[uid]
    name = uid
    try:
        u = web.users_info(user=uid)['user']
        pr = u.get('profile', {})
        name = pr.get('display_name') or pr.get('real_name') or u.get('name') or uid
    except Exception:
        pass
    _name_cache[uid] = name
    return name


def summarize_channel(channel: str) -> str | None:
    """채널 최근 대화를 가져와 claude로 요약. 텍스트 반환(게시는 호출부)."""
    cfg = CHANNELS.get(channel, {})
    try:
        resp = web.conversations_history(channel=channel, limit=100)
    except Exception as e:
        log(f'  summary history fail: {e}')
        return None
    msgs = _expand_with_replies(channel, resp.get('messages', []))  # 과거→현재, 답글 포함
    lines = []
    for m in msgs:
        if m.get('subtype'):          # join/leave/bot_message 등 제외
            continue
        if m.get('user') == BOT:      # 봇 자기 메시지 제외
            continue
        txt = (m.get('text') or '').strip()
        if not txt:
            continue
        nm = _resolve_name(m.get('user') or m.get('bot_id') or '?')
        mark = '↳ ' if _is_reply(m) else ''  # 스레드 답글 표시
        lines.append(f'{mark}{nm}: {txt}')
    convo = '\n'.join(lines)[-6000:]
    if not convo.strip():
        return '요약할 최근 대화가 없어요.'
    prompt = ("다음은 이 슬랙 채널의 최근 대화입니다. 핵심을 한국어 불릿으로 간결히 요약하세요. "
              "주요 논의·결정·할 일 위주로 5줄 이내. 사족 없이 요약만.\n\n" + convo)
    notools = ['Bash', 'Write', 'Edit', 'NotebookEdit',
               'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'Task']
    try:
        r = _run_claude(prompt, str(uuid.uuid4()), True, 300,
                        cfg.get('model', 'opus'), cfg.get('cwd'),
                        cfg.get('add_dirs'), notools)
    except Exception as e:
        log(f'  summary claude fail: {e}')
        return None
    if r.returncode != 0:
        log(f'  summary rc={r.returncode}: {(r.stderr or "")[-200:]}')
        return None
    return (r.stdout or '').strip() or None


# ── 투표/설문 (팀 협업) ────────────────────────────────────────────
def _load_polls() -> dict:
    try:
        if POLLS_FILE.exists():
            return json.loads(POLLS_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_polls(d: dict) -> None:
    POLLS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')


def handle_poll(channel: str, text: str) -> None:
    if re.search(r'(집계|결과)', text):
        _tally_poll(channel)
        return
    after = re.sub(r'^.*?(투표|설문)\s*', '', text, count=1).strip()
    title, optstr = '투표', after
    if ':' in after:
        t, optstr = after.split(':', 1)
        title = t.strip() or '투표'
    opts = [s.strip() for s in optstr.split('/') if s.strip()][:9]
    if len(opts) < 2:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            "투표 형식: `투표 제목: 옵션1 / 옵션2 / 옵션3` (옵션 2~9개)\n"
            "예) `투표 점심: 김밥 / 국밥 / 파스타`  ·  집계: `투표 집계`"))
        return
    lines = [f"📊 *투표: {title}*"]
    for i, o in enumerate(opts):
        lines.append(f"{POLL_EMOJI[i]} {o}")
    lines.append("_이모지를 눌러 투표하세요. 집계는 `투표 집계`_")
    res = web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)
    pts = res.get('ts')
    for i in range(len(opts)):
        try:
            web.reactions_add(channel=channel, timestamp=pts, name=POLL_NAME[i])
        except Exception:
            pass
    polls = _load_polls()
    polls[channel] = {'ts': pts, 'title': title, 'options': opts}
    _save_polls(polls)
    log(f'poll created ch={channel} opts={len(opts)}')


def _tally_poll(channel: str) -> None:
    p = _load_polls().get(channel)
    if not p:
        web.chat_postMessage(channel=channel, text=(
            "집계할 투표가 없어요. 먼저 `투표 제목: a / b` 로 만들어주세요."))
        return
    try:
        r = web.reactions_get(channel=channel, timestamp=p['ts'])
        reactions = (r.get('message', {}) or {}).get('reactions', [])
    except Exception as e:
        log(f'  poll tally fail: {e}')
        web.chat_postMessage(channel=channel, text="집계 실패 (reactions:read 권한 필요).")
        return
    counts = {rc.get('name'): rc.get('count', 0) for rc in reactions}
    tally = [(o, max(0, counts.get(POLL_NAME[i], 0) - 1))  # 봇 시드 1표 제외
             for i, o in enumerate(p['options'])]
    total = sum(c for _, c in tally)
    lines = [f"📊 *투표 결과: {p['title']}*  (총 {total}표)"]
    for o, c in sorted(tally, key=lambda x: -x[1]):
        lines.append(f"• {o}: *{c}표* {'█' * c}")
    web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)


# ── 작업 객체 명령 (jipsa 2.0) ─────────────────────────────────────
def _tasks_enabled(channel: str) -> bool:
    return bool(tsk and CHANNELS.get(channel, {}).get('tasks_enabled'))


def handle_task_command(channel: str, text: str) -> bool:
    """작업 관련 슬랙 명령 처리. 처리했으면 True(=이후 claude 호출 skip)."""
    if not _tasks_enabled(channel):
        return False
    t = text.strip()
    if t in ('작업목록', '작업 목록'):
        rows = tsk.list_tasks(channel, states=('대기', '진행', '막힘'))
        if not rows:
            web.chat_postMessage(channel=channel, text='열린 작업이 없어요.')
            return True
        lines = ['*📋 열린 작업*']
        icon = {'대기': '🕒', '진행': '🔧', '막힘': '⛔'}
        for r in rows:
            lines.append(f"{icon.get(r['state'], '•')} `{r['id'][:8]}` {r['title']}  _({r['state']})_")
        web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)
        return True
    m = re.match(r'^작업\s+([0-9a-f]{6,})\s+(진행|막힘|완료|취소)$', t)
    if m:
        prefix, state = m.group(1), m.group(2)
        rows = [r for r in tsk.list_tasks(channel) if r['id'].startswith(prefix)]
        if not rows:
            web.chat_postMessage(channel=channel, text=f'`{prefix}` 작업을 못 찾았어요.')
            return True
        ok = tsk.set_state(rows[0]['id'], state)
        if ok:
            web.chat_postMessage(channel=channel,
                                 text=f"`{rows[0]['id'][:8]}` → *{state}*", mrkdwn=True)
        else:
            web.chat_postMessage(channel=channel,
                                 text=f"`{rows[0]['id'][:8]}` 는 현재 *{rows[0]['state']}* 라 *{state}* 로 못 바꿔요.",
                                 mrkdwn=True)
        return True
    return False


# ── 저장 폴더 변경 (jipsa 2.0, 소유자 전용 + 허용루트 제한) ─────────
OUTPUT_DIR_TRIGGER = re.compile(r'^저장\s*폴더')


def _under_roots(path: str, roots: list[str]) -> bool:
    """path 가 허용 루트(roots) 중 하나의 하위인가? (심볼릭/대소문자/드라이브 안전)"""
    rp = os.path.realpath(os.path.expanduser(path))
    for r in roots:
        rr = os.path.realpath(os.path.expanduser(r))
        try:
            if os.path.commonpath([rp, rr]) == rr:
                return True
        except ValueError:                      # 다른 드라이브(Windows) 등
            continue
    return False


def handle_output_dir_command(channel: str, user: str, text: str) -> bool:
    """`저장폴더` / `저장폴더 <경로>` 처리. 처리했으면 True."""
    t = text.strip()
    if not OUTPUT_DIR_TRIGGER.match(t):
        return False
    cfg = CHANNELS.get(channel, {})
    arg = OUTPUT_DIR_TRIGGER.sub('', t, count=1).strip().strip('"\'' )
    if not arg:                                  # 현재 저장 폴더 표시
        cur = cfg.get('add_dirs') or []
        roots = cfg.get('output_roots') or []
        msg = f"📁 현재 저장 폴더: {cur[0] if cur else '(샌드박스 — 작업폴더 내부만)'}"
        if roots:
            msg += f"\n변경(소유자만): `저장폴더 <경로>` · 허용 루트: {', '.join(roots)}"
        web.chat_postMessage(channel=channel, mrkdwn=True, text=msg)
        return True
    # 변경은 소유자만
    if user != MIRI:
        try:
            web.chat_postEphemeral(channel=channel, user=user,
                                   text='저장 폴더 변경은 소유자만 가능해요.')
        except Exception:
            pass
        return True
    roots = cfg.get('output_roots') or []
    if not roots:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            '이 채널은 저장 폴더 변경이 꺼져 있어요. '
            '`channels.json` 의 `output_roots` 에 허용 폴더를 먼저 지정하세요.'))
        return True
    if not _under_roots(arg, roots):
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            f"허용된 폴더 밖이에요. 허용 루트 하위로만 지정할 수 있어요:\n• " +
            '\n• '.join(roots)))
        return True
    path = os.path.realpath(os.path.expanduser(arg))
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        web.chat_postMessage(channel=channel, text=f'폴더를 만들지 못했어요: {e}')
        return True
    CHANNELS[channel]['add_dirs'] = [path]        # 핫리로드 (다음 호출부터 적용)
    CHANNELS[channel]['output_dir'] = path        # 기본 저장 위치(시스템 프롬프트 주입)
    _save_channel_override(channel, {'add_dirs': [path], 'output_dir': path})  # 재시작에도 유지
    web.chat_postMessage(channel=channel, mrkdwn=True,
                         text=f"✅ 저장 폴더를 `{path}` 로 바꿨어요. (즉시 적용)")
    log(f'output dir set ch={channel} -> {path} by {user}')
    return True


# ── 비품관리 슬랙 명령 (jipsa 2.0 P2) ──────────────────────────────
def _supply_match_claude(prompt: str, cfg: dict) -> str:
    """품목 매칭/표파싱용 1회성 claude 호출(새 세션, 도구 없음)."""
    notools = ['Bash', 'Write', 'Edit', 'NotebookEdit',
               'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'Task']
    try:
        r = _run_claude(prompt, str(uuid.uuid4()), True, 220, 'sonnet',
                        cfg.get('cwd'), [], notools)
        if r.returncode != 0:
            log(f'  supply claude rc={r.returncode}: {(r.stderr or "")[-200:]}')
            return ''
        return r.stdout or ''
    except subprocess.TimeoutExpired:
        log('  supply claude TIMEOUT')
        return ''
    except Exception as e:
        log(f'  supply claude exc: {e}')
        return ''


_inbound_wait: dict = {}   # (channel, user) -> 만료 epoch. `입고등록`만 보낸 뒤 표 대기.


def _do_inbound_register(channel: str, body: str, cfg: dict) -> bool:
    """구매표(body) 파싱 → 입고 반영 → 결과 게시. 처리했으면 True."""
    rows = sply.parse_purchase_table(body)
    used_llm = False
    log(f'입고등록 처리: body_len={len(body)} 규칙파서={len(rows)}건')
    if not rows:                              # 칸 깨진 붙여넣기 → LLM 파싱 폴백
        rows = sply.parse_purchase_table_llm(body, lambda p: _supply_match_claude(p, cfg))
        used_llm = bool(rows)
        log(f'  LLM 폴백 파싱={len(rows)}건')
    if not rows:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            "표를 못 읽었어요. 품명·수량이 있는 표를 붙여넣어 주세요.\n"
            "(엑셀/그룹웨어에서 복사해 붙여도 됩니다)"))
        return True
    res = sply.apply_inbound(cfg, rows, lambda p: _supply_match_claude(p, cfg))
    hdr = f'입고등록 {len(rows)}행 처리' + (' (AI 표 인식)' if used_llm else '')
    _supply_reply(channel, res, header=hdr)
    return True


def handle_supply_command(channel: str, user: str, text: str) -> bool:
    """비품 명령: 재고 / 재고 <품목> / 비품현황 / 입고 <품목> <수량> / 입고등록 <표>.
    조회는 누구나, 입고(쓰기)는 담당자(managers + 소유자)만. 처리했으면 True."""
    if sply is None:
        return False
    cfg = _load_supply_cfg()
    if not cfg:
        return False
    t = text.strip()
    first = t.split('\n', 1)[0].strip()
    managers = set(cfg.get('managers', [])) | ({MIRI} if MIRI else set())
    key = (channel, user)

    # `입고등록`만 먼저 보낸 담당자가 이어서 표를 붙여넣은 경우 → 이 메시지를 표로 처리
    if key in _inbound_wait:
        if time.time() > _inbound_wait.get(key, 0):
            _inbound_wait.pop(key, None)
        elif first not in ('입고등록', '입고 등록', '재고', '비품현황', '재고현황') \
                and not re.match(r'^(입고|재고|출고|실사)\s', first):
            _inbound_wait.pop(key, None)
            return _do_inbound_register(channel, t, cfg)

    def deny():
        try:
            web.chat_postEphemeral(channel=channel, user=user,
                                   text='입고 등록은 담당자만 할 수 있어요.')
        except Exception:
            pass
        return True

    # 비품현황 / 재고 전체
    if first in ('비품현황', '재고현황', '재고'):
        inv = sply.read_inventory(cfg)
        if not inv:
            web.chat_postMessage(channel=channel, text='등록된 비품 재고가 없어요.')
            return True
        low = [r for r in inv.values() if int(r['현재수량']) < int(r.get('최소수량', 0) or 0)]
        lines = [f'📦 *비품 현황* (품목 {len(inv)}종)']
        for r in sorted(inv.values(), key=lambda x: x['품목'])[:40]:
            mark = ' ⚠️' if int(r['현재수량']) < int(r.get('최소수량', 0) or 0) else ''
            lines.append(f"• {r['품목']}: *{r['현재수량']}*{(' '+r['단위']) if r.get('단위') else ''}{mark}")
        if low:
            lines.append('\n⚠️ 저재고: ' + ', '.join(r['품목'] for r in low))
        web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)
        return True

    # 재고 <품목>
    m = re.match(r'^재고\s+(.+)$', first)
    if m:
        q = m.group(1).strip()
        inv = sply.read_inventory(cfg)
        hits = [r for k, r in inv.items() if q.lower() in k.lower()]
        if hits:
            lines = ['📦 *재고 조회*'] + [
                f"• {r['품목']}: *{r['현재수량']}*{(' '+r['단위']) if r.get('단위') else ''} "
                f"(최소 {r.get('최소수량', 0)})" for r in hits[:15]]
            web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)
        else:
            web.chat_postMessage(channel=channel, text=f"'{q}' 품목을 재고표에서 못 찾았어요.")
        return True

    # 입고 <품목> <수량>  (단건, 담당자만 — 팩 표기 있으면 곱셈)
    m = re.match(r'^입고\s+(.+?)\s+(\d+)$', first)
    if m:
        if user not in managers:
            return deny()
        rows = [{'품명': m.group(1).strip(), '수량': int(m.group(2)), '부서': ''}]
        res = sply.apply_inbound(cfg, rows, lambda p: _supply_match_claude(p, cfg))
        _supply_reply(channel, res)
        return True

    # 출고 <품목> <수량> (차감) / 실사 <품목> <수량> (현재고를 그 값으로 설정). 담당자만.
    m = re.match(r'^(출고|실사)\s+(.+?)\s+(\d+)$', first)
    if m:
        if user not in managers:
            return deny()
        mode, item, qty = m.group(1), m.group(2).strip(), int(m.group(3))
        res = sply.apply_adjust(cfg, item, qty, mode, lambda p: _supply_match_claude(p, cfg))
        _supply_reply(channel, res)
        return True

    # 입고등록 (담당자만). 표가 같은 메시지에 있으면 바로, 없으면 다음 메시지 대기.
    if first in ('입고등록', '입고 등록'):
        if user not in managers:
            return deny()
        body = t.split('\n', 1)[1] if '\n' in t else ''
        if body.strip():
            return _do_inbound_register(channel, body, cfg)
        _inbound_wait[key] = time.time() + 180          # 3분 내 다음 메시지를 표로
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            "📥 이어서 *구매표를 붙여넣어* 주세요 (3분 내, 이 채널에).\n"
            "엑셀/그룹웨어에서 복사해 붙여도 돼요. 품명·수량만 있으면 됩니다."))
        return True

    return False


def _supply_reply(channel: str, res: dict, header: str = '') -> None:
    if res.get('error') == 'locked':
        web.chat_postMessage(channel=channel,
                             text='⚠️ 비품 엑셀이 열려 있어요. 닫고 다시 시도해 주세요.')
        return
    lines = ([f'*{header}*'] if header else []) + (res.get('alerts') or ['처리할 내용이 없어요.'])
    web.chat_postMessage(channel=channel, text='\n'.join(lines[:40]), mrkdwn=True)


# ── 도움말 ─────────────────────────────────────────────────────────
def handle_help(channel: str) -> None:
    p = '@집사 ' if CHANNELS.get(channel, {}).get('require_mention') else ''
    text = (
        "🤖 *집사 사용법*\n\n"
        "*⏰ 알림*\n"
        f"• `{p}매월 25일 10시에 정산 알려줘`\n"
        f"• `{p}매주 월요일 9시에 회의 알려줘`\n"
        f"• `{p}매일 18시에 일일보고 알려줘`\n"
        f"• `{p}6월 20일 14시에 워크숍 알려줘`  _(1회성)_\n"
        "• 사전알림 `… 3일 전에도` · 담당자호출 `… @이름 에게`\n"
        f"• `{p}알림 목록` · `{p}2번 알림 삭제` · `{p}2번 알림 16시로 바꿔줘`\n\n"
        "*✅ 완료체크*  알림 메시지에 ✅ 누르기\n"
        f"*🗳️ 투표*  `{p}투표 점심: 김밥 / 국밥` → `{p}투표 집계`\n"
        "*📌 위키수집*  메시지에 📌 누르면 위키에 저장\n"
        f"*📝 요약*  `{p}오늘 대화 요약해줘`\n"
        f"*📚 FAQ*  `{p}연차 어떻게 신청해?`"
    )
    if _tasks_enabled(channel):
        text += (
            "\n\n*📋 작업*\n"
            f"• `{p}작업목록`  _(열린 작업 보기)_\n"
            f"• `{p}작업 ab12cd34 진행|막힘|완료|취소`  _(상태 변경)_"
        )
    web.chat_postMessage(channel=channel, text=text, mrkdwn=True)


_dialog_self_turn_count = 0  # 대화 채널에서 내 연속 응답 카운트 (무한루프 방지)
_discussion_mode: dict[str, bool] = {}  # channel → 토론 모드 ON/OFF

# discussion 상태 공유 파일 (proactive cron 스크립트가 read 함)
DISCUSSION_STATE_FILE = SHARED_DIR / 'discussion_state.json'


def _write_discussion_state() -> None:
    """현재 _discussion_mode 상태를 JSON 파일로 저장."""
    try:
        DISCUSSION_STATE_FILE.write_text(json.dumps({
            'mode': dict(_discussion_mode),
            'ts': time.time(),
        }))
    except Exception:
        pass


def notion_log_turn(channel: str, event_ts: str, user_text: str, reply_text: str,
                    session_id: str, model: str = 'opus') -> None:
    """슬랙 ↔ 클코 한 턴을 노션 'Claude Code 턴 로그' DB에 적재.

    daemon이 claude --print headless 모드라 Stop hook 발동 안 함 → 직접 적재.

    NOTION_SESSION_DB가 비어있으면 skip (옵션 기능).
    """
    if not NOTION_SESSION_DB:
        return
    try:
        import json as _json
        import urllib.request as _urlreq
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from lib.notion import upsert_by_external_id

        kst = _tz(_td(hours=9))
        now = _dt.now(kst)
        ts_iso = now.isoformat()
        date_str = now.date().isoformat()

        def _trim(s: str, n: int = 1900) -> str:
            return (s or '')[:n]

        # NOTION_API_TOKEN 우선 (NOTION_TOKEN/notion-token.txt는 legacy fallback)
        token = (
            os.environ.get('NOTION_API_TOKEN')
            or os.environ.get('NOTION_TOKEN')
            or ''
        )
        if not token:
            legacy = os.path.expanduser('~/.claude/secrets/notion-token.txt')
            if os.path.exists(legacy):
                token = open(legacy).read().strip()
        if not token:
            log('  notion log skip: no NOTION_API_TOKEN')
            return
        headers = {'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28',
                   'Content-Type': 'application/json'}

        def _http(method, url, data=None):
            if data is not None:
                data = _json.dumps(data).encode()
            req = _urlreq.Request(url, data=data, headers=headers, method=method)
            with _urlreq.urlopen(req, timeout=15) as r:
                return _json.loads(r.read() or b'{}')

        # NOTION_DAILY_DB 비어있으면 _http에서 즉시 실패 → except로 빠지고 daily_id=None 유지
        daily_id = None
        try:
            r = _http('POST', f'https://api.notion.com/v1/databases/{NOTION_DAILY_DB}/query',
                {'filter': {'property': '날짜', 'date': {'equals': date_str}}, 'page_size': 1})
            if r.get('results'):
                daily_id = r['results'][0]['id']
            else:
                p = _http('POST', 'https://api.notion.com/v1/pages', {
                    'parent': {'database_id': NOTION_DAILY_DB},
                    'properties': {
                        '이름': {'title': [{'text': {'content': f'{date_str} 일일 통합'}}]},
                        '날짜': {'date': {'start': date_str}},
                        '상태': {'status': {'name': '진행 중'}},
                        'external_id': {'rich_text': [{'text': {'content': f'daily:{date_str}'}}]},
                    },
                })
                daily_id = p.get('id')
        except Exception:
            pass

        properties = {
            '프로젝트': {'title': [{'text': {'content': f'{BOT_NAME} (슬랙)'}}]},
            '시각': {'date': {'start': ts_iso}},
            '세션 ID': {'rich_text': [{'text': {'content': session_id}}]},
            '작업 디렉토리': {'rich_text': [{'text': {'content': str(Path.home() / '.claude/scripts/slack-jipsa')}}]},
            '시킨 일': {'rich_text': [{'text': {'content': _trim(user_text)}}]},
            '한 일': {'rich_text': [{'text': {'content': _trim(reply_text)}}]},
            '결과': {'rich_text': [{'text': {'content': _trim(reply_text)}}]},
            '모델': {'select': {'name': model}},
            '도구 호출 수': {'number': 0},
            '전체 요약': {'rich_text': [{'text': {'content': _trim(user_text + ' → ' + reply_text)}}]},
        }
        if daily_id:
            properties['📊 일일 통합'] = {'relation': [{'id': daily_id}]}

        ext_id = f'jipsa:{channel}:{event_ts}'
        upsert_by_external_id(NOTION_SESSION_DB, ext_id, properties)
    except Exception as e:
        log(f'  notion log fail: {e}')


def _cell_text(node) -> str:
    """리치텍스트 노드(셀)에서 모든 text를 이어붙여 추출."""
    out = []

    def walk(e):
        if isinstance(e, dict):
            if e.get('type') in ('text', 'raw_text'):   # 헤더=text, 데이터셀=raw_text
                out.append(e.get('text', ''))
            elif e.get('type') == 'link':
                out.append(e.get('text') or e.get('url', ''))
            for v in e.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(e, list):
            for x in e:
                walk(x)
    walk(node)
    return ''.join(out).strip()


def _extract_event_text(event: dict) -> str:
    """event['text'] 우선. 비면 붙여넣은 표(attachments/blocks의 type=table)를
    탭구분 텍스트로 변환해 반환(슬랙 리치 테이블 붙여넣기 대응)."""
    txt = (event.get('text') or '').strip()
    containers = []
    for att in (event.get('attachments') or []):
        containers += att.get('blocks') or []
    containers += event.get('blocks') or []
    lines = []
    for b in containers:
        if isinstance(b, dict) and b.get('type') == 'table':
            for row in (b.get('rows') or []):
                if isinstance(row, list):
                    lines.append('\t'.join(_cell_text(c) for c in row))
                elif isinstance(row, dict):           # 행이 dict 형태일 때
                    cells = row.get('cells') or row.get('elements') or []
                    lines.append('\t'.join(_cell_text(c) for c in cells))
    table_txt = '\n'.join(ln for ln in lines if ln.strip())
    if table_txt.strip():
        return (txt + '\n' + table_txt).strip() if txt else table_txt
    return txt


def handle_message(event: dict) -> None:
    """사용자 메시지 처리 + (대화 채널이면) 다른 봇 메시지에도 반응."""
    global _dialog_self_turn_count
    text = _extract_event_text(event)
    channel = event.get('channel', '')
    ts = event.get('ts', '')
    user = event.get('user', '')
    bot_id = event.get('bot_id', '')

    if not text: return
    if channel not in CHANNELS and channel != CHANNEL_DIALOG: return

    global _discussion_mode
    is_dialog = (channel == CHANNEL_DIALOG)
    is_miri = (user == MIRI)
    is_self = (user == BOT or bot_id == BOT)
    # is_other_bot: 대화(dialog) 채널에서 '다른 봇'을 가려내는 용도.
    # 일반/팀 채널의 사람 팀원과 혼동되지 않도록 dialog 채널에서만 판정.
    is_other_bot = (is_dialog and not is_miri and not is_self
                    and user.startswith('U') and user != MIRI)

    if is_self: return  # 자기 자신 무시

    # 채널별 접근 정책: 'owner'=소유자(USER_SLACK_ID)만, 'all'=채널의 모든 사람(팀)
    ch_cfg = CHANNELS.get(channel, {})
    access = ch_cfg.get('access', 'owner')
    if not is_dialog and access == 'owner' and not is_miri:
        return  # 개인 채널은 소유자만 응답

    # 입고등록 표 대기 중인 담당자는 멘션 없이도 다음 메시지(표)를 이어받는다.
    if not is_dialog and (channel, user) in _inbound_wait:
        try:
            if handle_supply_command(channel, user, text):
                return
        except Exception as e:
            log(f'  supply wait err: {e}')

    # @멘션 게이트: require_mention 채널(부서 공용)은 봇이 멘션됐을 때만 응답
    if not is_dialog and ch_cfg.get('require_mention'):
        mention_re = re.compile(r'<@' + re.escape(BOT) + r'(\|[^>]+)?>')
        if not mention_re.search(text):
            return  # 봇 멘션 없으면 무시 (팀 잡담에 끼어들지 않음)
        text = mention_re.sub(' ', text).strip() or '안녕하세요'

    # 단톡 토론 모드 관리 (사용자 발화 기준 ON/OFF)
    if is_dialog and is_miri:
        if DISCUSSION_STOP.search(text):
            _discussion_mode[channel] = False
            _write_discussion_state()
            log(f'  discussion mode OFF (stop keyword)')
        elif DISCUSSION_TRIGGER.search(text):
            _discussion_mode[channel] = True
            _dialog_self_turn_count = 0
            _write_discussion_state()
            log(f'  discussion mode ON (trigger keyword)')
        else:
            # 사용자의 일반 발화 = 새 주제 = 토론 종료
            was_on = _discussion_mode.get(channel, False)
            _discussion_mode[channel] = False
            _dialog_self_turn_count = 0
            if was_on:
                _write_discussion_state()
                log(f'  discussion mode OFF (new topic from user)')

    # 다른 봇 발화: discussion 모드가 켜져있을 때만 응답 허용
    if is_other_bot:
        thread_ts_only = event.get('thread_ts', '')
        try:
            append_shared(channel, thread_ts_only, '코덱스', text, msg_ts=ts)
        except Exception:
            pass
        if not _discussion_mode.get(channel):
            log(f'  other-bot message, discussion OFF — skip response')
            return
        if _dialog_self_turn_count >= DIALOG_TURN_LIMIT:
            log(f'  dialog turn limit ({DIALOG_TURN_LIMIT}) — auto-stop discussion')
            _discussion_mode[channel] = False
            _write_discussion_state()
            return
        log(f'  discussion ON — respond to other-bot (turn {_dialog_self_turn_count}/{DIALOG_TURN_LIMIT})')

    # 명령어: '리셋' / '새세션' / 'reset' (단독 키워드)
    if text.strip().lower() in ('리셋', '새세션', '새 세션', 'reset', '!reset', '!리셋'):
        new_sid = reset_session(channel)
        web.chat_postMessage(channel=channel, text=f'🔄 새 세션 시작 (`{new_sid[:8]}`)')
        return

    # 저장 폴더 변경 명령 (소유자 전용). 처리되면 claude 호출 skip.
    try:
        if handle_output_dir_command(channel, user, text):
            return
    except Exception as e:
        log(f'  output dir cmd err: {e}')

    # 비품관리 명령 (재고/비품현황/입고/입고등록). 처리되면 claude 호출 skip.
    try:
        if handle_supply_command(channel, user, text):
            return
    except Exception as e:
        log(f'  supply cmd err: {e}')

    # 작업 객체 명령 (tasks_enabled 채널만). 처리되면 claude 호출 skip.
    try:
        if handle_task_command(channel, text):
            return
    except Exception as e:
        log(f'  task cmd err: {e}')

    # 도움말
    if HELP_TRIGGER.search(text):
        log('help req')
        try:
            handle_help(channel)
        except Exception as e:
            log(f'  help err: {e}')
        return

    # 알리미(매월 반복) 의도면 전용 처리 — daemon이 직접 저장/조회/삭제.
    # (부서 채널 봇은 읽기전용이라 claude로는 저장 불가 → 여기서 처리)
    if rmd is not None and rmd.looks_like_reminder(text):
        log(f'reminder msg: {text[:80]}')
        rmd.handle(web, channel, user, text, ts)
        return

    # 대화 요약 요청이면 채널 히스토리를 가져와 요약 (팀 협업)
    if SUMMARY_TRIGGER.search(text):
        log(f'summary req: {text[:60]}')
        try:
            web.reactions_add(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
        except Exception:
            pass
        s = summarize_channel(channel)
        if s:
            web.chat_postMessage(channel=channel, text='📝 *대화 요약*\n' + s, mrkdwn=True)
        else:
            web.chat_postMessage(channel=channel, mrkdwn=True, text=(
                '대화를 요약하지 못했어요. (채널 히스토리 읽기 권한이 없을 수 있어요 — '
                '슬랙 앱에 `channels:history`/`groups:history` 스코프 필요)'))
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
            web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
        except Exception:
            pass
        return

    # 투표/설문 요청 처리 (팀 협업)
    if POLL_TRIGGER.search(text):
        log(f'poll req: {text[:60]}')
        try:
            handle_poll(channel, text)
        except Exception as e:
            log(f'  poll err: {e}')
        return

    log(f'msg: {text[:80]}')

    # ⏳ reaction
    try:
        web.reactions_add(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
    except Exception as e:
        log(f'  reaction add fail: {e}')

    thread_ts = event.get('thread_ts', '')
    # 공유 버퍼 적재 (사용자 또는 다른 봇 발화)
    if is_miri:
        who_label = USER_NAME
    elif is_other_bot:
        who_label = 'other-bot'
    elif user:
        who_label = f'팀원({user})'  # 팀 채널: 누가 말했는지 구분
    else:
        who_label = '?'
    append_shared(channel, thread_ts, who_label, text, msg_ts=ts)

    # 공유 버퍼 맥락을 prompt에 prefix로 추가 (단톡↔갠톡 cross-channel)
    shared = load_shared(channel, thread_ts)
    prompt_with_ctx = text
    if shared and len(shared) > 1:
        ctx_lines = [f'## 최근 대화 맥락 ({USER_NAME}·Claude·다른 봇 모두 포함)']
        for h in shared[-15:-1]:  # 마지막은 방금 들어온 거라 제외
            ctx_lines.append(f'[{h.get("who","?")}] {h.get("text","")[:400]}')
        ctx_lines.append('')
        ctx_lines.append(f'## 현재 메시지')
        ctx_lines.append(text)
        prompt_with_ctx = '\n'.join(ctx_lines)

    # 클로드 호출 (resume 실패 시 자동 fallback)
    reply = call_claude(prompt_with_ctx, channel, thread_ts=thread_ts)
    log(f'  reply: {reply[:80]}')

    # 자기가 응답할 차례가 아니라 판단 → 시스템이 SKIP 출력 → post 안 함
    if reply.strip().upper().startswith('SKIP'):
        log(f'  SKIP — 다른 봇이 응답할 차례')
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
            web.reactions_add(channel=channel, timestamp=ts, name='eyes')
        except Exception:
            pass
        return

    # 빈 응답 또는 silent fail → 슬랙 푸시 안 함, reaction만
    if not reply.strip() or reply.strip() == '__SILENT_FAIL__':
        is_fail = reply.strip() == '__SILENT_FAIL__'
        log(f'  empty/fail reply — slack 미전송 (fail={is_fail})')
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
            web.reactions_add(channel=channel, timestamp=ts,
                              name='warning' if is_fail else 'speech_balloon')
        except Exception:
            pass
        return

    # 응답 마크다운 잔재 제거 (시스템 프롬프트로 못 막은 경우 안전망)
    sys.path.insert(0, str(Path.home() / '.claude/scripts'))
    try:
        from lib.slack_mrkdwn import to_mrkdwn
        reply_clean = to_mrkdwn(reply)
    except Exception:
        reply_clean = reply

    try:
        res = web.chat_postMessage(channel=channel, text=reply_clean, mrkdwn=True)
        if channel == CHANNEL_DIALOG:
            _dialog_self_turn_count += 1
        # 공유 버퍼에 클코 응답 적재
        append_shared(channel, thread_ts, '클코', reply_clean, msg_ts=str(res.get('ts', '') or ''))
    except Exception as e:
        log(f'  post fail: {e}')

    # ⏳ 제거, ✅ 추가
    try:
        web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
        web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
    except Exception as e:
        log(f'  reaction swap fail: {e}')

    # 노션 턴 로그 적재 (Stop hook 우회) — 비동기
    try:
        sid_for_log, _ = get_or_create_session(channel)
        threading.Thread(
            target=notion_log_turn,
            args=(channel, ts, text, reply_clean, sid_for_log, 'opus'),
            daemon=True,
        ).start()
    except Exception as e:
        log(f'  notion log thread fail: {e}')


# ── 승인 게이트 버튼 (jipsa 2.0) ───────────────────────────────────
def _audit_verdict(token: str, result: str, user: str, name: str) -> None:
    """승인 verdict 감사 로그 — 노션 세션 DB에 적재(있으면). 없으면 skip."""
    if not NOTION_SESSION_DB or tsk is None:
        return
    try:
        import approval as apv
        row = apv.get_approval(token) or {}
        threading.Thread(target=notion_log_turn, args=(
            row.get('channel_id', ''), f'gate:{token}',
            f'승인요청: {row.get("action_desc", "")}',
            f'{name}({user}) → {result}',
            f'gate:{token}', 'gate'), daemon=True).start()
    except Exception as e:
        log(f'  audit fail: {e}')


def handle_block_action(payload: dict) -> None:
    """승인 게이트 버튼 클릭 처리. token=button value, 누른 사람 검증 후 verdict."""
    if tsk is None:
        return
    try:
        import approval as apv
    except Exception as e:
        log(f'  approval import fail: {e}')
        return
    user = (payload.get('user') or {}).get('id', '')
    actions = payload.get('actions') or []
    if not actions:
        return
    act = actions[0]
    action_id = act.get('action_id', '')
    token = act.get('value', '')
    channel = (payload.get('channel') or {}).get('id', '')
    msg_ts = (payload.get('message') or {}).get('ts', '')
    if action_id not in ('gate_approve', 'gate_reject') or not token:
        return
    approve = (action_id == 'gate_approve')
    result = apv.decide(token, user, approve=approve)
    name = _resolve_name(user)
    if result in ('승인', '거부'):
        mark = '✅' if result == '승인' else '⛔'
        done = f"{mark} {name}님이 *{result}* 했습니다."
        try:  # 카드 메시지를 결과로 교체(버튼 제거)
            web.chat_update(channel=channel, ts=msg_ts, text=done,
                            blocks=[{'type': 'section',
                                     'text': {'type': 'mrkdwn', 'text': done}}])
        except Exception as e:
            log(f'  card update fail: {e}')
        log(f'gate {result} token={token[:8]} by={name}')
        try:
            _audit_verdict(token, result, user, name)
        except Exception:
            pass
    elif result == '권한없음':
        try:
            web.chat_postEphemeral(channel=channel, user=user,
                                   text='이 승인 요청을 처리할 권한이 없어요.')
        except Exception:
            pass
    else:  # 이미처리 / 만료 / 없음
        try:
            web.chat_postEphemeral(channel=channel, user=user,
                                   text=f'이미 처리됐거나 만료된 요청이에요. ({result})')
        except Exception:
            pass


def on_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    # Slack에 즉시 ACK (3초 이내 필수)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type == 'interactive':
        payload = req.payload or {}
        if payload.get('type') == 'block_actions':
            threading.Thread(target=handle_block_action, args=(payload,), daemon=True).start()
        return
    if req.type != 'events_api': return
    event = req.payload.get('event', {})
    if event.get('type') == 'message' and not event.get('subtype'):
        # subtype: 봇 메시지 / 채널 join 등 무시 (None인 일반 메시지만)
        # 비동기로 처리 (handler 블로킹 방지)
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()
    elif event.get('type') == 'reaction_added':
        threading.Thread(target=handle_reaction, args=(event,), daemon=True).start()


COMPLETE_REACTIONS = ('white_check_mark', 'heavy_check_mark', 'ballot_box_with_check')
PIN_EMOJI = 'pushpin'                                  # 📌 = 위키에 저장
WIKI_SAVED_FILE = Path.home() / '.claude/scripts/slack-jipsa/wiki_saved.json'


def handle_reaction(event: dict) -> None:
    """reaction_added 디스패처. ✅=완료기록(#3), 📌=위키 수집.
    (reaction_added 이벤트 구독 필요 — 현재 활성 확인됨)"""
    log(f"reaction recv: {event.get('reaction')} by {event.get('user')}")
    reaction = event.get('reaction')
    item = event.get('item', {})
    if item.get('type') != 'message':
        return
    ts, ch, user = item.get('ts'), item.get('channel'), event.get('user', '')
    if user == BOT:                                    # 봇 자기 반응 무시
        return
    if reaction in COMPLETE_REACTIONS:
        _handle_completion(ch, ts, user)
    elif reaction == PIN_EMOJI:
        _handle_pin(ch, ts, user)


def _handle_completion(ch: str, ts: str, user: str) -> None:
    if rmd is None:
        return
    name = _resolve_name(user)
    info = rmd.record_completion(ts, user, name)
    if not info:                                       # 알림 발사 메시지가 아니거나 이미 완료
        return
    try:
        web.chat_postMessage(channel=ch, thread_ts=ts,
                             text=f"✅ {name}님이 완료 처리했어요 — {info.get('message', '')}")
    except Exception as e:
        log(f'  completion post fail: {e}')
    log(f"completion id={info.get('id')} by={name}")


def _load_wiki_saved() -> set:
    try:
        if WIKI_SAVED_FILE.exists():
            return set(json.loads(WIKI_SAVED_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return set()


def _save_wiki_saved(s: set) -> None:
    try:
        WIKI_SAVED_FILE.write_text(json.dumps(sorted(s)[-500:], ensure_ascii=False),
                                   encoding='utf-8')
    except Exception:
        pass


def _clean_wiki_entry(s: str) -> str:
    """집사 정리 출력에서 코드펜스(```)·첫 '###' 이전 군더더기 제거."""
    lines = [ln for ln in s.splitlines() if not ln.strip().startswith('```')]
    s = '\n'.join(lines).strip()
    i = s.find('###')
    if i > 0:
        s = s[i:].strip()
    return s


def _handle_pin(channel: str, ts: str, user: str) -> None:
    """📌 반응 → 메시지를 집사가 정리해 해당 채널 docs/위키-수집.md 에 append."""
    cfg = CHANNELS.get(channel, {})
    cwd = cfg.get('cwd')
    if not cwd:
        return
    saved = _load_wiki_saved()
    if ts in saved:                                    # 같은 메시지 중복 저장 방지
        return
    try:
        m = web.reactions_get(channel=channel, timestamp=ts).get('message', {}) or {}
        msg_text = (m.get('text') or '').strip()
        author = _resolve_name(m.get('user') or m.get('bot_id') or '?')
    except Exception as e:
        log(f'  pin fetch fail: {e}')
        return
    if not msg_text:
        return
    notools = ['Bash', 'Write', 'Edit', 'NotebookEdit',
               'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'Task']
    prompt = ("다음 슬랙 메시지를 부서 위키 항목으로 정리하세요.\n"
              "출력 형식: 첫 줄 '### ' + 짧은 제목, 그 아래 핵심을 1~3개 불릿(`• `)으로.\n"
              "규칙: 코드블록(```)으로 감싸지 말 것. '###'부터 바로 시작. 인사·이모지·다른 설명 금지. "
              "정리할 내용이 없으면 원문을 한 줄로.\n\n"
              f"메시지(작성자 {author}):\n{msg_text}")
    entry = msg_text                                   # 정리 실패 시 원문 fallback
    try:
        rr = _run_claude(prompt, str(uuid.uuid4()), True, 180,
                         cfg.get('model', 'opus'), cwd, cfg.get('add_dirs'), notools)
        if rr.returncode == 0 and (rr.stdout or '').strip():
            entry = _clean_wiki_entry(rr.stdout.strip())
    except Exception as e:
        log(f'  pin organize fail: {e}')
    docs_dir = Path(cwd) / 'docs'
    wiki = docs_dir / '위키-수집.md'
    stamp = time.strftime('%Y-%m-%d %H:%M')
    try:
        docs_dir.mkdir(parents=True, exist_ok=True)
        new_file = (not wiki.exists()) or wiki.stat().st_size == 0
        with open(wiki, 'a', encoding='utf-8') as f:
            if new_file:
                f.write("# 위키 수집 (📌 자동 수집)\n\n"
                        "> 메시지에 📌 를 누르면 집사가 정리해 여기 모읍니다.\n")
            f.write(f"\n{entry}\n\n_— {author}, {stamp} 수집_\n")
    except Exception as e:
        log(f'  pin write fail: {e}')
        return
    saved.add(ts)
    _save_wiki_saved(saved)
    try:
        web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
        web.chat_postMessage(channel=channel, thread_ts=ts, text="📌 위키에 저장했어요.")
    except Exception:
        pass
    log(f'pin saved ts={ts} -> {wiki}')


SUPPLY_CONFIG_FILE = Path.home() / '.claude/scripts/slack-jipsa/supply.json'


def _load_supply_cfg() -> dict | None:
    if not SUPPLY_CONFIG_FILE.exists():
        return None
    try:
        return json.loads(SUPPLY_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log(f'supply.json 파싱 실패: {e}')
        return None


def _supply_poll_loop() -> None:
    if sply is None:
        return
    cfg = _load_supply_cfg()
    if not cfg:
        log('supply.json 없음 — 비품관리 비활성')
        return
    notify = cfg.get('notify_channel', '')
    dry = bool(cfg.get('dry_run', True))   # 기본 dry-run(안전)

    def post(msg: str) -> None:
        if notify:
            try:
                web.chat_postMessage(channel=notify, text=msg, mrkdwn=True)
            except Exception as e:
                log(f'  supply post fail: {e}')

    def run_claude_for_match(prompt: str) -> str:
        notools = ['Bash', 'Write', 'Edit', 'NotebookEdit',
                   'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'Task']
        try:
            r = _run_claude(prompt, str(uuid.uuid4()), True, 120, 'sonnet',
                            cfg.get('cwd'), [], notools)
            return r.stdout or '' if r.returncode == 0 else ''
        except Exception:
            return ''

    log(f'비품관리 폴링 시작 (every {cfg.get("poll_min",5)}분, dry_run={dry})')
    while True:
        try:
            sply.sync_once(web, cfg, run_claude_for_match, post, dry_run=dry)
        except Exception as e:
            log(f'  supply sync err: {e}')
        time.sleep(max(60, int(cfg.get('poll_min', 5)) * 60))


def _gate_sweeper() -> None:
    """주기적으로 만료된 승인 요청을 정리하고 막힌 task 요청자에게 알림."""
    if tsk is None:
        return
    try:
        import approval as apv
    except Exception:
        return
    last = int(time.time())
    while True:
        time.sleep(30)
        try:
            n = apv.expire_stale()
            if n:
                for row in apv.list_expired_since(last):
                    ch = row.get('channel_id'); th = row.get('thread_ts') or None
                    try:
                        web.chat_postMessage(channel=ch, thread_ts=th,
                            text=f"⏱️ 승인 요청이 만료돼 *거부* 처리됐어요: {row.get('action_desc', '')[:120]}")
                    except Exception:
                        pass
            last = int(time.time())
        except Exception as e:
            log(f'  sweeper err: {e}')


def main() -> None:
    log(f'=== {BOT_NAME} daemon 시작 (channel={CHANNEL[:6]}.., bot={BOT}) ===')
    sock.socket_mode_request_listeners.append(on_event)
    sock.connect()
    log('Socket Mode 연결됨. 메시지 대기 중...')
    # 알리미 스케줄러 스레드 시작
    if rmd is not None:
        rmd.set_logger(log)
        if hasattr(rmd, 'set_executor'):
            rmd.set_executor(run_scheduled_action)   # 알리미 2.0: 능동 작업 실행기
        threading.Thread(target=rmd.reminder_loop, args=(web,), daemon=True).start()
    else:
        log('reminders 모듈 로드 실패 — 알리미 비활성')
    # 승인 게이트 만료 sweeper (jipsa 2.0)
    threading.Thread(target=_gate_sweeper, daemon=True).start()
    # 비품관리 폴링 (supply.json 있을 때만 활성)
    threading.Thread(target=_supply_poll_loop, daemon=True).start()
    # 무한 대기
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()

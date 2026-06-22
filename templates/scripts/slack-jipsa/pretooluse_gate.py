#!/usr/bin/env python3
"""PreToolUse 게이트 훅 — 민감 도구 호출 직전 슬랙 승인 대기.

Claude Code 가 별도 프로세스로 실행. stdin=PreToolUse payload(JSON).
출력: {"hookSpecificOutput":{"hookEventName":"PreToolUse",
       "permissionDecision":"allow"|"deny","permissionDecisionReason":...}} (exit 0)

게이트 컨텍스트(채널/승인자/타임아웃)는 데몬이 env 로 주입:
  JIPSA_GATE_CHANNEL, JIPSA_GATE_THREAD, JIPSA_GATE_APPROVERS(쉼표구분),
  JIPSA_GATE_TIMEOUT_MIN
env 가 없으면(=비게이트 컨텍스트) 즉시 allow 로 통과(안전한 무동작).
"""
from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval  # 같은 디렉토리 형제 모듈
import tasks


def _allow(reason: str = '') -> None:
    out = {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
           'permissionDecision': 'allow'}}
    if reason:
        out['hookSpecificOutput']['permissionDecisionReason'] = reason
    print(json.dumps(out))
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
          'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))
    sys.exit(0)


def _describe(tool_name: str, tool_input: dict) -> str:
    if tool_name == 'Bash':
        return f"Bash: {tool_input.get('command', '')[:400]}"
    if tool_name in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
        return f"{tool_name}: {tool_input.get('file_path', '')}"
    return f"{tool_name}: {json.dumps(tool_input, ensure_ascii=False)[:300]}"


def _post_card(channel: str, thread_ts: str, token: str, desc: str) -> None:
    """데몬과 독립적으로 슬랙에 카드 게시(봇 토큰 직접 사용)."""
    from slack_sdk import WebClient
    secrets = Path.home() / '.claude/secrets/slack-jipsa.env'
    env = {}
    for line in secrets.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    web = WebClient(token=env['SLACK_BOT_TOKEN'])
    web.chat_postMessage(channel=channel, blocks=approval.build_card(token, desc),
                         text='🔐 승인 요청', thread_ts=thread_ts or None)


def main() -> None:
    channel = os.environ.get('JIPSA_GATE_CHANNEL', '')
    if not channel:
        _allow()                       # 게이트 컨텍스트 아님 → 통과
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _allow('payload 파싱 실패 — 안전 통과')
    tool_name = payload.get('tool_name', '')
    tool_input = payload.get('tool_input', {}) or {}
    desc = _describe(tool_name, tool_input)
    approvers = [a for a in os.environ.get('JIPSA_GATE_APPROVERS', '').split(',') if a]
    timeout_min = int(os.environ.get('JIPSA_GATE_TIMEOUT_MIN', '15') or '15')
    thread_ts = os.environ.get('JIPSA_GATE_THREAD', '')

    tasks.init_db()
    # 게이트 1건 = task(막힘) + approval(대기)
    tid = tasks.create_task(channel, f'승인대기: {desc[:60]}', body=desc,
                            direction='a2h', thread_ts=thread_ts,
                            meta={'gate': True})
    tasks.set_state(tid, '진행'); tasks.set_state(tid, '막힘')
    token = approval.request_approval(tid, channel, desc, approvers, timeout_min, thread_ts)
    try:
        _post_card(channel, thread_ts, token, desc)
    except Exception as e:
        _allow(f'카드 게시 실패({e}) — 운영중단 방지 위해 통과')  # fail-open: 게이트가 작업을 막지 않게

    # DB 폴링 랑데부 (훅 timeout 은 settings 에서 timeout_min*60+여유 로 설정)
    deadline = time.time() + timeout_min * 60 + 5
    while time.time() < deadline:
        row = approval.get_approval(token)
        st = row['status'] if row else '없음'
        if st == '승인':
            tasks.set_state(tid, '진행')
            _allow('슬랙 승인됨')
        if st in ('거부', '만료'):
            _deny(f'슬랙에서 {st} 처리됨')
        time.sleep(2)
    approval.expire_stale()
    _deny('승인 타임아웃 — 기본 거부')


if __name__ == '__main__':
    main()

# 모듈 9 — 승인 게이트 (Approval Gate · jipsa 2.0)

> 전제: 모듈 1·5·6·8 완료. **개인 채널**(`access: owner`)에 적용한다.
> 개인 채널은 Opus 풀권한이라 편하지만, 민감 도구(Bash/Write/Edit)를 **그냥 실행**하는 건 위험하다. 게이트를 켜면 민감 작업 직전 슬랙에 `[✅승인][⛔거부]` 카드가 뜨고, **누를 때까지 대기**한다. 자리를 비웠어도 폰으로 승인하면 이어서 진행된다.

## 어떻게 동작하나 (DB 랑데부)

PreToolUse 훅(`pretooluse_gate.py`)은 Claude Code가 민감 도구 호출 직전 띄우는 **별도 프로세스**다.

1. 훅이 `jipsa.db`의 `approvals` 행을 INSERT(상태=대기) + 슬랙 카드 게시.
2. 그 행의 상태가 바뀔 때까지 DB를 폴링.
3. 데몬은 버튼 클릭(block_actions)을 받아 행을 승인/거부로 UPDATE.
4. 훅이 변화를 감지 → 승인이면 도구 실행 허용, 거부/만료면 차단.

메모리 결합이 없어 **데몬이나 세션이 죽었다 살아나도 상태가 DB에 남아 복구된다**(새벽 3시 원칙).

## 켜는 법

### 1. channels.json에 `gate` 추가 (개인 채널)

```json
"C_PERSONAL_CHANNEL_ID": {
  "...": "...",
  "tasks_enabled": true,
  "gate": {
    "enabled": false,
    "sensitive_tools": ["Bash", "Write", "Edit"],
    "approvers": ["U_OWNER_SLACK_ID"],
    "timeout_min": 15,
    "on_timeout": "deny"
  }
}
```

- `enabled` — 켜기 전까지 `false`로 두면 1.x 동작 그대로. 검증 끝나면 `true`.
- `approvers` — 버튼을 누를 수 있는 사람의 slack_id 목록. 보통 본인 하나(`USER_SLACK_ID`와 동일).
- `timeout_min` — 무응답 시 자동 거부까지 분. 개인 채널 권장 15.
- `on_timeout` — 타임아웃 정책. 안전쪽으로 `deny` 고정 권장.

### 2. PreToolUse 훅 등록 (.claude/settings.json)

`templates/scripts/slack-jipsa/.claude/settings.json.tmpl`을 `~/.claude/scripts/slack-jipsa/.claude/settings.json`로 배치하고 `__PYTHON__`을 OS에 맞게 치환한다.

- Windows: `python`
- macOS / Linux: `python3`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/pretooluse_gate.py\"",
            "timeout": 1000 }
        ]
      }
    ]
  }
}
```

> `timeout`(초)은 게이트 `timeout_min` × 60 + 여유보다 커야 한다. 훅 기본 타임아웃은 600초(10분)라, 15분 게이트를 쓰려면 위처럼 늘려야 한다.

### 3. 슬랙 앱 — Interactivity 켜기

버튼 클릭(block_actions) 이벤트를 받으려면 슬랙 앱 설정에서 **Interactivity & Shortcuts**가 켜져 있어야 한다(Socket Mode 사용 시 URL 입력 불필요, 토글만 ON). 봇 스코프는 기존 `chat:write`면 충분하다.

## 단계적 롤아웃 (되돌릴 수 있게)

1. `gate.enabled: false`로 두고 배포 — 동작 무변.
2. 본인 slack_id를 `approvers`에 넣고 `enabled: true`.
3. 개인 채널에서 `test.txt 파일 하나 만들어줘` → 카드 등장 → 승인 → 파일 생성 확인.
4. 거부/무응답(타임아웃)도 한 번씩 확인.
5. 문제 생기면 `enabled: false`로 즉시 롤백.

## 안전 설계

- **토큰**: 버튼마다 단일사용 UUID. 승인자 집합에 바인딩 — 권한 없는 사람이 눌러도 무효.
- **동시 클릭**: compare-and-set으로 첫 verdict만 채택.
- **타임아웃**: 무응답이면 기본 거부(안전쪽). sweeper가 정리하고 요청자에게 알린다.
- **fail-open vs fail-closed**: 카드 게시 자체가 실패하면 *통과*(개인 비서 마비 방지), 사람이 못 본 타임아웃은 *거부*. 이 비대칭은 의도된 것.

## 트러블슈팅

- **카드가 안 뜬다** → ① 슬랙 Interactivity 미설정, ② `.claude/settings.json` 미배치/`__PYTHON__` 치환 누락, ③ `gate.enabled: false`. 데몬 로그에서 `gate` 관련 줄 확인.
- **승인해도 진행이 안 된다** → 훅 `timeout`이 `timeout_min`보다 짧을 수 있다. settings의 `timeout`을 늘려라.
- **노션 턴 로그가 중복으로 쌓인다** → 게이트 채널은 `CLAUDE_SKIP_HOOKS`를 해제하므로, 사용자 `~/.claude/settings.json`에 Stop 훅이 있으면 같이 발동할 수 있다. settings.json에 `"Stop": []`를 추가해 무력화하면 된다.

## 부서 채널은?

부서 escalate 게이트(차단 대신 승인요청)는 다음 단계(Phase C)다. 이 모듈은 **개인 채널 차단 게이트**까지만 다룬다. 부서 채널에는 `gate`를 켜지 말 것(`mode: escalate`는 아직 미구현이라 게이트가 작동하지 않음).

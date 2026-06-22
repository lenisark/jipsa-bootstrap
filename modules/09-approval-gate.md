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

`templates/scripts/slack-jipsa/.claude/settings.json.tmpl`을 `~/.claude/scripts/slack-jipsa/.claude/settings.json`로 배치하고 두 토큰을 치환한다.

- `__PYTHON__` — Windows `python` / macOS·Linux `python3`
- `__GATE_DIR__` — `pretooluse_gate.py`의 **절대경로 디렉토리** = `~/.claude/scripts/slack-jipsa`를 홈 전개한 실제 경로 (예: `/Users/이름/.claude/scripts/slack-jipsa`, Windows는 `C:\Users\이름\.claude\scripts\slack-jipsa`). 절대경로라 부서 채널(cwd=slack-team)에서도 같은 훅 파일을 가리킨다.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 \"/Users/이름/.claude/scripts/slack-jipsa/pretooluse_gate.py\"",
            "timeout": 1000 }
        ]
      }
    ]
  }
}
```

> `timeout`(초)은 게이트 `timeout_min` × 60 + 여유보다 커야 한다. 훅 기본 타임아웃은 600초(10분)라, 15분 게이트를 쓰려면 위처럼 늘려야 한다. 부서 60분 게이트면 `3700` 이상으로.

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

## 부서 채널 — 권한 상승(escalate) 게이트

부서 채널은 평소 `disallowed_tools`로 Write/Edit를 **하드 차단**한다(읽기·Q&A 전용). escalate 게이트는 "차단" 대신 **"승인 요청 → 지정 승인자(예: 인사총무) 멘션 → 승인되면 통제된 경로로 실행"**으로 바꾼다. 안전성은 유지하면서 부서 집사가 할 수 있는 일이 늘어난다.

### 동작 차이 (개인 block vs 부서 escalate)

| | 개인(block) | 부서(escalate) |
|---|---|---|
| 평소 민감 도구 | 호출 가능(풀권한) | `disallowed_tools`로 차단 |
| 게이트 켜면 | 호출 직전 본인 승인 | 민감 도구를 풀고, 호출 직전 **승인자 멘션** 후 대기 |
| 승인자 | 본인 | 부서 승인자(HR 등) |
| 카드 | 버튼만 | `<@승인자>` 멘션 + 버튼 |

내부적으로는 같은 PreToolUse 게이트를 쓴다. escalate면 데몬이 `gate.sensitive_tools`를 `disallowed_tools`에서 빼서 호출 가능케 하되, 그 도구는 반드시 게이트를 거친다.

### 부서 채널 셋업

1. `channels.json` 부서 항목에 `gate` 추가:

```json
"C_TEAM_CHANNEL_ID": {
  "...": "...",
  "disallowed_tools": ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"],
  "gate": {
    "enabled": false,
    "mode": "escalate",
    "sensitive_tools": ["Write", "Edit"],
    "approvers": ["U_HRADMIN_SLACK_ID"],
    "timeout_min": 60,
    "on_timeout": "deny"
  }
}
```

- `sensitive_tools` — 게이트로 풀어줄 도구(승인 시 실행). 여기 없는 도구(예: Bash)는 계속 하드 차단된다.
- `approvers` — 부서 승인 권한자(HR 등). 카드에서 이 사람들을 멘션한다.
- `timeout_min` — 부서는 사람이 늦게 볼 수 있으니 60 권장. 훅 `timeout`도 그에 맞게(>3700s).

2. **부서 채널 cwd에도 settings.json 배치** — `.claude/settings.json`을 `~/.claude/scripts/slack-team/.claude/settings.json`에도 같은 내용으로 둔다(`__GATE_DIR__`가 절대경로라 내용 동일). slack-team에는 `pretooluse_gate.py`를 복사할 필요 없음 — 훅은 slack-jipsa의 파일을 절대경로로 실행한다.

3. 슬랙 Interactivity ON(개인과 동일), `enabled: true`로 롤아웃.

### 부서 채널 저장 폴더 — 슬랙에서 변경 (소유자 전용)

부서 집사가 만드는 파일은 기본적으로 작업폴더(`cwd`=slack-team) 안에만 저장된다(샌드박스). 외부 폴더에 저장하게 하려면 `add_dirs`에 그 폴더를 넣으면 되는데, 이를 **슬랙에서 소유자가 직접** 바꿀 수 있다.

1. `channels.json` 부서 항목에 허용 상위 루트를 지정한다(이것만 PC에서 설정):

```json
"output_roots": ["~/Documents/부서공유"]
```

2. 슬랙에서(소유자만):
   - `저장폴더` → 현재 저장 폴더 표시
   - `저장폴더 ~/Documents/부서공유/A팀` → 그 폴더로 변경(없으면 생성), **즉시 적용**

가드레일:
- **소유자(`USER_SLACK_ID`)만** 변경 가능 — 일반 팀원은 ephemeral "권한 없음".
- **`output_roots` 하위 경로만** 허용 — 홈 전체·시스템 폴더·`..` 상위탈출 차단(realpath 정규화).
- 변경값은 `channel_overrides.json`에 저장돼 **재시작에도 유지**(channels.json 원본은 안 건드림).
- 부서 채널은 escalate 게이트도 같이 켜두면, 폴더가 열려 있어도 실제 쓰기마다 승인을 받는다(이중 안전).

### 부서 채널 주의 — 무한 승인요청 금지

부서 집사가 할 수 없는 일(권한 밖)을 자꾸 시도해 승인 요청을 남발하지 않도록, `slack-team/CLAUDE.md`에 "정말 필요할 때만 쓰기 작업을 시도하고, 애매하면 정중히 거절하라"는 가이드를 둔다. 게이트는 실제 도구 호출이 있을 때만 발동하므로, 페르소나가 1차 방어선이다.

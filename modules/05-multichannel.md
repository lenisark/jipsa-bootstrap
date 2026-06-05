# 모듈 5 — 멀티채널 권한·모델 분리 (channels.json)

> 전제: 모듈 1(슬랙 브릿지) 완료. 데몬(`daemon.py`)이 이미 돌고 있어야 함.
> 이 모듈은 **데몬 하나로 여러 슬랙 채널**을 굴리되, 채널마다 **모델·접근권한·작업폴더·샌드박스**를 다르게 주는 설정을 깐다.

## 무엇을 하는가

`templates/scripts/slack-jipsa/daemon.py`는 `~/.claude/scripts/slack-jipsa/channels.json`을 읽어 **채널별로 다르게 동작**한다. 이 파일이 없거나 비면 legacy 단일 채널(opus/owner/홈)로 fallback 하므로, 모듈 1만으로도 동작은 한다. 이 모듈은 거기에 채널 분리를 더한다.

## channels.json 구조

`templates/scripts/slack-jipsa/channels.json.example`을 복사해 채널ID만 실제 값으로 바꾼다.

```json
{
  "C_PERSONAL_CHANNEL_ID": {
    "label": "개인 비서",
    "model": "opus",
    "access": "owner",
    "cwd": "~/.claude/scripts/slack-jipsa",
    "add_dirs": ["~"]
  },
  "C_TEAM_CHANNEL_ID": {
    "label": "부서 공용 비서",
    "model": "sonnet",
    "access": "all",
    "cwd": "~/.claude/scripts/slack-team",
    "add_dirs": [],
    "disallowed_tools": ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"],
    "require_mention": true
  }
}
```

### 키 의미 (daemon.py `load_channels` 기준)

| 키 | 의미 | 비고 |
|----|------|------|
| (최상위 키) | 슬랙 채널 ID (`C0...`) | 슬랙 채널 우클릭 → 채널 세부정보 맨 아래 |
| `label` | 로그/표시용 이름 | |
| `model` | `opus` / `sonnet` / `haiku` | `claude --model` 값 |
| `access` | `owner`=소유자(USER_SLACK_ID)만 / `all`=채널의 모든 사람 | 개인=owner, 팀=all |
| `cwd` | claude 실행 디렉토리 | **이 폴더의 CLAUDE.md = 채널 페르소나/규칙** |
| `add_dirs` | 추가 접근 허용 디렉토리 | **비우면 `cwd`만 접근 = 샌드박스** |
| `disallowed_tools` | 비활성화할 도구 | 팀 채널은 Bash/Write/Edit 차단 → 읽기·Q&A 전용 |
| `require_mention` | true면 `@봇` 멘션해야 응답 | 팀 잡담에 안 끼어들게 |

## 셋업 순서 (AI가 진행)

1. **채널 만들기** — 운영자에게 슬랙에서 채널 2개(개인용 1, 부서공용 1)를 만들고 봇을 초대(`/invite @봇`)하게 안내.
2. **채널 ID 수집** — 각 채널 ID(`C0...`) 2개를 받는다.
3. **channels.json 작성** — `.example` 복사 후 채널ID 치환하여 `~/.claude/scripts/slack-jipsa/channels.json`에 Write. (이 실파일은 git에 안 올라감 — `.gitignore`)
4. **부서 채널용 슬랙 스코프 추가** — 부서 기능(요약·완료체크·투표·위키수집)을 위해 봇 앱에 다음 스코프가 필요:
   - `channels:history` / `groups:history` (대화 요약)
   - `reactions:read` / `reactions:write` (완료체크 ✅, 투표 집계, 위키 📌)
   - `chat:write`
   - Event Subscriptions에 `reaction_added`, `message.channels`/`message.groups` 구독
   스코프 추가 후 **앱 재설치** 필요.
5. **데몬 재시작** — channels.json 반영 위해 데몬 재시작 (Windows: 작업 스케줄러 task 재시작 / mac: `launchctl unload && load` / linux: `systemctl --user restart`).
6. **검증** — 데몬 로그에 `채널 설정 로드: C0..(opus/owner), C0..(sonnet/all)` 한 줄 떠야 함:
   - Windows: `Get-Content "$env:USERPROFILE\.claude\scripts\slack-jipsa\logs\$(Get-Date -f yyyy-MM-dd).log" -Tail 20`
   - mac/linux: `tail -20 ~/.claude/scripts/slack-jipsa/logs/$(date +%Y-%m-%d).log`

다음: 개인 채널 동작은 [모듈 6](06-personal-jipsa.md), 부서 채널 기능은 [모듈 7](07-team-jipsa.md).

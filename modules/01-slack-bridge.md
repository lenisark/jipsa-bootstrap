# 모듈 1: 슬랙 ↔ 클로드 코드 양방향

> AI는 이 문서를 읽고 사용자에게 1:1로 안내합니다. 사용자는 이 문서를 직접 읽지 않습니다.

## 무엇을 깔까

**슬랙 → 클로드 코드**: 슬랙 채널 메시지 → 백그라운드 daemon → `claude --print --resume <session_id>` → 응답을 채널에 post.

**클로드 코드 → 슬랙** (Stop hook): Claude Code 세션 끝나면 → 자동으로 채널에 세션 요약 post.

이 모듈은 운영 환경에서 매일 작동하는 검증된 코드를 그대로 사용합니다. `templates/` 안의 파일을 사용자 환경에 복사하면 됩니다.

---

## 사전 조건

- macOS / Windows / Linux (AI가 분기 처리)
- Claude Code 설치됨
- 슬랙 워크스페이스 (개인 무료 가능)
- Python 3.9+ (macOS 기본 `/usr/bin/python3`, Windows `py -3`, Linux `/usr/bin/python3`)

---

## 단계별 안내 (AI가 따라가는 순서)

### Step 1. 슬랙 앱 생성

사용자에게:
```
1) https://api.slack.com/apps 열어주세요
2) "Create New App" → "From scratch" 클릭
3) App Name 입력 (예: "내 비서") + Workspace 선택
4) "Create App" 클릭

완료되면 알려주세요!
```

### Step 2. Bot Token Scopes

사용자에게 — 왼쪽 메뉴 "OAuth & Permissions" → "Bot Token Scopes" 에 7개 추가:
```
chat:write
groups:history
groups:read
reactions:write
users:read
files:read
chat:write.public
```

### Step 3. Socket Mode + App-Level Token

왼쪽 메뉴 "Socket Mode" → ON → Token Name `socket-mode` + Scope `connections:write` → Generate → **xapp-... 토큰 사용자가 임시 저장**.

### Step 4. Event Subscriptions

"Event Subscriptions" → ON → "Subscribe to bot events" 에 `message.groups` 추가 → Save.

### Step 5. App Home

"App Home" → Display Name 설정 + "Messages Tab" ON + "Allow users to send messages" 체크.

### Step 6. Install App + Bot Token

"Install App" → "Install to Workspace" → 허용 → **xoxb-... 토큰 사용자가 임시 저장**.

### Step 7. 슬랙 채널 + 봇 초대 + ID 수집

사용자에게:
```
1) 새 비공개 채널 만들기 (예: #내-비서)
2) 채널 ⚙️ → Integrations → Add an App → 만든 봇 추가
3) 채널 이름 우클릭 → View channel details → 맨 아래 Channel ID 복사
4) 본인 프로필 → ⋯ → Copy member ID 복사
```

사용자한테 채널 ID (C0...) 와 본인 user ID (U0...) 받기.

### Step 8. AI 작업 — 폴더 + lib 의존성 카피

검증 코드는 `~/.claude/scripts/lib/` 의 helper 모듈을 사용합니다. 키트의 검증된 lib를 사용자 환경에 복사:

```bash
mkdir -p ~/.claude/secrets ~/.claude/scripts/lib ~/.claude/scripts/slack-jipsa/{logs,sessions} ~/.claude/hooks
touch ~/.claude/scripts/lib/__init__.py

# lib 카피 (절대 변경 금지 — 검증된 코드)
cp templates/lib/notion.py ~/.claude/scripts/lib/
cp templates/lib/slack_mrkdwn.py ~/.claude/scripts/lib/
cp templates/lib/md_to_notion.py ~/.claude/hooks/
```

### Step 9. AI 작업 — 시크릿 파일 작성

`.env.example` 을 base로 사용자 토큰 채워서 `~/.claude/secrets/slack-jipsa.env` 에 Write:

```env
SLACK_BOT_TOKEN=xoxb-...        # Step 6에서 받음
SLACK_APP_TOKEN=xapp-...        # Step 3에서 받음
SLACK_CHANNEL=C0...             # Step 7에서 받음
USER_SLACK_ID=U0...             # Step 7에서 받음 (본인)
BOT_USER_ID=                    # Step 11에서 자동 채움
USER_NAME=                      # 사용자에게 본인 호칭 묻기 (예: 철수, 영희)
SLACK_BOT_NAME=                 # 봇 이름 묻기 (예: 내 비서, 집사)
SLACK_SESSION_WEBHOOK=          # 선택, Incoming Webhook URL 있으면
NOTION_API_TOKEN=               # 모듈 4 진행 시 채움
NOTION_SESSION_DB=              # 모듈 4 진행 시 채움
NOTION_DAILY_DB=                # 모듈 4 옵션
```

권한:
```bash
chmod 600 ~/.claude/secrets/slack-jipsa.env
```

### Step 10. AI 작업 — slack_sdk 설치

```bash
/usr/bin/pip3 install --user slack_sdk
```

실패 시 venv:
```bash
python3 -m venv ~/.claude/scripts/slack-jipsa/venv
~/.claude/scripts/slack-jipsa/venv/bin/pip install slack_sdk
```

> venv를 쓴 경우, Step 13의 `run.sh` 마지막 줄 `/usr/bin/python3` 부분을 `~/.claude/scripts/slack-jipsa/venv/bin/python` 으로 바꿔야 합니다 (AI가 자동 처리).

### Step 11. AI 작업 — Bot user ID 자동 조회

```bash
set -a; source ~/.claude/secrets/slack-jipsa.env; set +a
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('user_id',''))"
```

결과를 `BOT_USER_ID` 에 채워 다시 Write.

### Step 12. AI 작업 — daemon.py 그대로 카피

```bash
cp templates/scripts/slack-jipsa/daemon.py ~/.claude/scripts/slack-jipsa/daemon.py
```

**수정 불필요** — 환경 의존 부분은 이미 환경변수로 분리되어 있음. `.env` 만 올바르면 그대로 작동.

### Step 13. AI 작업 — run.sh + launchd plist 생성

`~/.claude/scripts/slack-jipsa/run.sh`:
```bash
#!/bin/bash
set -euo pipefail
set -a
source "$HOME/.claude/secrets/slack-jipsa.env"
set +a
exec /usr/bin/python3 "$HOME/.claude/scripts/slack-jipsa/daemon.py"
```
```bash
chmod +x ~/.claude/scripts/slack-jipsa/run.sh
```

`templates/launchd-daemon.plist.tmpl` 읽고 `{USERNAME}` (=`whoami`) · `{HOME}` 치환 후 `~/Library/LaunchAgents/com.{USERNAME}.slack-jipsa.plist` 에 Write.

```bash
launchctl load ~/Library/LaunchAgents/com.{USERNAME}.slack-jipsa.plist
launchctl list | grep slack-jipsa
```

PID 가 보이면 정상.

### Step 14. 검증 1 — 슬랙 → 클코

사용자에게:
```
방금 만든 슬랙 채널에서 "안녕" 보내주세요.
5~30초 안에:
1) 봇이 ⏳ reaction
2) 응답 post
3) ✅ reaction

됐나요?
```

안 되면:
```bash
tail -30 ~/.claude/scripts/slack-jipsa/logs/$(date +%Y-%m-%d).log
```

### Step 15. AI 작업 — Stop hook 셋업 (클코→슬랙)

이게 양방향의 핵심. 어떤 Claude Code 세션이든 끝나면 자동으로 슬랙에 보고.

```bash
cp templates/hooks/slack-session-summary.sh ~/.claude/hooks/
cp templates/hooks/append_turn_raw.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/slack-session-summary.sh
```

`~/.claude/settings.json` Read → 다음 형태로 수정 (기존 키 보존):

```json
{
  "env": {
    "SLACK_SESSION_WEBHOOK": "https://hooks.slack.com/services/...",
    "NOTION_API_TOKEN": "",
    "NOTION_SESSION_DB": "",
    "NOTION_DAILY_DB": ""
  },
  "hooks": {
    "Stop": [
      { "type": "command", "command": "$HOME/.claude/hooks/slack-session-summary.sh" }
    ]
  }
}
```

> Stop hook은 슬랙 webhook (`SLACK_SESSION_WEBHOOK`) 또는 노션 둘 다 비어있으면 조용히 종료. 사용자가 webhook을 만들지 않았으면 일단 빈 값. (모듈 1 만으로는 슬랙 보고 안 됨. 모듈 4 또는 webhook 추가 필요.)

#### Incoming Webhook 만드는 법 (선택)

슬랙 앱 페이지 → "Incoming Webhooks" → ON → "Add New Webhook to Workspace" → 채널 선택 → URL 받기 → `SLACK_SESSION_WEBHOOK` 에 입력.

### Step 16. 검증 2 — 클코 → 슬랙

사용자에게:
```
새 터미널에서:
claude --print "1+1은?"

응답 후 슬랙 채널에 세션 보고 메시지 자동으로 오나요?
```

안 되면:
```bash
tail -30 /tmp/slack-session-summary.log
```

webhook 안 만들었으면 슬랙엔 안 옴. 노션 적재 (모듈 4) 진행 후 노션에 row 생기는지로 검증.

---

## 트러블슈팅

| 증상 | 해결 |
|------|------|
| `dispatch_failed` | Step 2 권한 누락 (`groups:history`). Install App 다시. |
| 봇 응답 없음 | `launchctl list | grep slack-jipsa` PID 확인 |
| `ModuleNotFoundError: lib.notion` | Step 8 lib 카피 누락. `ls ~/.claude/scripts/lib/notion.py` |
| `Permission denied` (.env) | `chmod 600 ~/.claude/secrets/slack-jipsa.env` |
| Stop hook 발동 안 함 | `~/.claude/settings.json` hooks.Stop 등록 확인 |

---

## OS별 분기 — Windows

위 단계의 macOS/Linux 부분을 Windows로 번역하면서 안내. AI 책임 — `templates/scripts/slack-jipsa/daemon.py` (Python 코드, OS 독립) 그대로 사용 가능. 다음만 PowerShell로:

- 폴더 만들기: `New-Item -ItemType Directory -Force -Path`
- 파일 카피: `Copy-Item`
- 시크릿 권한: `icacls ... /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F"`
- daemon 자동 시작: launchd 대신 Task Scheduler (`Register-ScheduledTask`, at logon trigger)
- run.sh 대신 run.ps1 (env 로드 후 daemon.py 실행)
- Stop hook command: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ~/.claude/hooks/slack-session-summary.ps1`
- AI 책임: `slack-session-summary.sh` (bash) 를 PowerShell로 번역해서 `slack-session-summary.ps1` 생성. 핵심 로직: stdin JSON 받기 → transcript 추출 → curl 대신 `Invoke-RestMethod` 로 슬랙 + 노션 post

## OS별 분기 — Linux

위 단계의 macOS 부분을 Linux로 번역. 거의 동일 (bash 그대로). 차이점:
- 자동 시작: launchd 대신 systemd user service (`templates/systemd-slack-jipsa.service.tmpl` 참고)
- `loginctl enable-linger $(whoami)` 로 로그아웃 후에도 작동
- 패키지: `apt install python3-pip jq curl` 또는 `dnf install`

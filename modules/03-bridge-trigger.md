# 모듈 3: 슬랙 + 폴더 합치기

> 모듈 1·2 모두 완료한 사용자에게 응용편을 안내합니다.

## 무엇을 합치는가

모듈 2의 폴더 트리거 처리 결과를 모듈 1의 슬랙 채널에 자동 post.

**시나리오**:
- 회의 녹음 파일을 폴더에 떨어뜨림 → STT → **요약을 슬랙으로 받음**
- PDF 보고서를 폴더에 떨어뜨림 → 분석 → **인사이트 슬랙으로 받음**
- 외주 결과물 폴더 → 자동 리뷰 → **이슈를 슬랙으로 받음**

핵심 가치: 컴퓨터 앞에 없어도 처리 완료 → 슬랙 모바일 알림.

---

## 사전 조건

- 모듈 1 완료 (슬랙 봇 동작 중, `~/.claude/secrets/slack-jipsa.env` 존재)
- 모듈 2 완료 (폴더 watcher 동작 중)

---

## 단계별 안내

### Step 1. 사용자에게 합치는 방식 선택

```
폴더 처리 결과를 슬랙에 어떻게 받을까요?

① 처리 시작·완료만 알림 ("PDF 분석 시작했어요" / "완료. 결과는 폴더에")
② 결과 본문을 슬랙에 직접 post (요약을 슬랙에서 바로 읽기)
③ 둘 다

번호 알려주세요.
```

### Step 2. AI 작업 — folder-watch/run.sh 수정

모듈 2에서 만든 `~/.claude/scripts/folder-watch/run.sh` 를 Read.

`claude --print` 호출 부분 앞뒤에 슬랙 post 추가:

```bash
#!/bin/bash
set -euo pipefail

# Load secrets
set -a
source "$HOME/.claude/secrets/slack-jipsa.env"
set +a

WATCH_DIR="{WATCH_FOLDER}"
PROCESSED_DIR="$WATCH_DIR/.processed"
LOG="$HOME/.claude/scripts/folder-watch/logs/$(date +%Y-%m-%d).log"

post_slack() {
    local TEXT="$1"
    curl -s -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d "$(jq -n --arg ch "$SLACK_CHANNEL" --arg t "$TEXT" '{channel:$ch, text:$t}')" \
        >> "$LOG" 2>&1
}

find "$WATCH_DIR" -maxdepth 1 -type f ! -name '.*' | while read -r FILE; do
    BASENAME=$(basename "$FILE")
    echo "[$(date '+%H:%M:%S')] Processing: $BASENAME" >> "$LOG"

    # 시작 알림 (시나리오 ①, ③)
    post_slack ":hourglass: \`$BASENAME\` 처리 시작"

    # Claude Code 호출 + 결과 캡처
    RESULT=$(claude --print --add-dir "$WATCH_DIR" "{SCENARIO_PROMPT} $FILE" 2>&1 | tee -a "$LOG")

    # 결과 알림 (시나리오 ②, ③)
    if [ -n "$RESULT" ]; then
        # 슬랙 메시지 길이 제한 (40000자), 안전하게 3500자로 자름
        SHORT=$(echo "$RESULT" | head -c 3500)
        post_slack ":white_check_mark: \`$BASENAME\` 처리 완료\n\`\`\`\n$SHORT\n\`\`\`"
    else
        post_slack ":white_check_mark: \`$BASENAME\` 처리 완료 (결과는 폴더 확인)"
    fi

    mv "$FILE" "$PROCESSED_DIR/$(date +%Y%m%d-%H%M%S)-$BASENAME"
done
```

> 시나리오 선택에 따라 `post_slack` 호출을 시작만/결과만/둘 다 중 골라서 활성화.

### Step 3. AI 작업 — jq 설치 확인

```bash
which jq || brew install jq
```

`jq` 없으면 자동 설치 (Homebrew 필요. 없으면 사용자 안내).

### Step 4. 검증

사용자에게:
```
테스트할게요!

폴더 ({WATCH_FOLDER})에 파일 하나 떨어뜨려 주세요.

5~30초 안에 슬랙 채널 두 메시지가 와야 합니다:
1) ⏳ "파일명 처리 시작"
2) ✅ "파일명 처리 완료" + (선택) 결과 본문

오나요?
```

---

## 트러블슈팅

| 증상 | 해결 |
|------|------|
| 슬랙 메시지 안 옴 | `source ~/.claude/secrets/slack-jipsa.env` 가 run.sh 안에 있는지 확인 |
| `jq: command not found` | `brew install jq` |
| 메시지가 너무 길어서 잘림 | head -c 값 줄이기 또는 결과를 첨부 파일로 업로드 |
| 같은 파일 두 번 처리 | 모듈 2의 `.processed/` 이동 로직 확인 |

---

## 활용 아이디어

| 패턴 | 폴더 이름 추천 | 시나리오 |
|------|--------------|---------|
| 회의 녹음 | `~/Documents/meetings/` | STT → 요약 → 슬랙 |
| 영수증 | `~/Documents/receipts/` | OCR → 카테고리 추정 → 슬랙 |
| 외주 결과물 | `~/Documents/deliverables/` | 리뷰 코멘트 → 슬랙 |
| 매일 일기 | `~/Documents/diary/` | 감정 분석 → 노션 |
| RSS 다운로드 | `~/Downloads/rss/` | 본문 요약 → 슬랙 |

---

## OS별 분기 — Windows 진행 방식

모듈 2의 Windows 버전(`folder-watch.ps1`)에서 `Process-File` 함수 안에 슬랙 post 추가:

```powershell
function Post-Slack($text) {
    $body = @{ channel = $env:SLACK_CHANNEL; text = $text } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://slack.com/api/chat.postMessage" `
        -Method Post `
        -Headers @{ Authorization = "Bearer $env:SLACK_BOT_TOKEN"; "Content-Type" = "application/json; charset=utf-8" } `
        -Body $body | Out-Null
}

function Process-File($filePath) {
    $basename = Split-Path $filePath -Leaf
    Post-Slack ":hourglass: ``$basename`` 처리 시작"

    $prompt = "{SCENARIO_PROMPT} $filePath"
    $result = & claude --print --add-dir $WatchDir $prompt 2>&1 | Out-String

    if ($result) {
        $short = if ($result.Length -gt 3500) { $result.Substring(0, 3500) } else { $result }
        Post-Slack ":white_check_mark: ``$basename`` 처리 완료`n````n$short`n```"
    } else {
        Post-Slack ":white_check_mark: ``$basename`` 처리 완료 (결과는 폴더 확인)"
    }

    $newName = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$basename"
    Move-Item $filePath (Join-Path $ProcessedDir $newName)
}
```

전제: ps1 시작부에 `slack-jipsa.env` 파싱하여 `$env:SLACK_BOT_TOKEN`, `$env:SLACK_CHANNEL` 환경변수로 로드.

### 환경변수 로드 (ps1 상단에 추가)
```powershell
Get-Content "$env:USERPROFILE\.claude\secrets\slack-jipsa.env" | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
    }
}
```

### 트러블슈팅 (Windows)
| 증상 | 해결 |
|------|------|
| `Invoke-RestMethod : invalid_auth` | 토큰 환경변수 로딩 확인. ps1 상단 정규식 점검 |
| 한글 깨짐 | ps1 파일 인코딩을 UTF-8 with BOM 으로 저장 |
| `ConvertTo-Json` 출력에 줄바꿈이 `\r\n` | 슬랙 메시지에는 문제 없음 (mrkdwn은 둘 다 허용) |

---

## OS별 분기 — Linux 진행 방식

모듈 2의 Linux `folder-watch.sh` 는 macOS 와 동일한 bash 라서 macOS 가이드의 합치기 스크립트가 그대로 작동합니다. `jq` 만 사전에 설치:

```bash
# Ubuntu/Debian
sudo apt install -y jq

# Fedora/RHEL
# sudo dnf install -y jq
```

`folder-watch.sh` 상단에 `source ~/.claude/secrets/slack-jipsa.env` 추가하고 macOS 절차 동일하게 진행.

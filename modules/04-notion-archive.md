# 모듈 4: 노션 자동 적재 (선택)

> 슬랙 ↔ 클코 한 턴 + Claude Code 어느 세션이든 끝나면 그 내용을 노션 DB에 자동 적재. 검색·회고·아카이브 가능.

## 무엇을 깔까

| 적재 대상 | 트리거 | 적재 위치 |
|----------|-------|----------|
| 슬랙 대화 한 턴 | daemon.py 메시지 처리 후 | `Claude Code 턴 로그` DB |
| Claude Code 세션 끝 | Stop hook (`slack-session-summary.sh`) | 동일 DB |
| (옵션) 일별 hub | 위 두 개가 자동 매칭 | `일일 통합` DB |

검증된 패턴 그대로. 모든 Claude Code 작업이 노션에 자동 누적되어 외부에서 다시 보고 검색·회고할 수 있는 거울 역할.

---

## 사전 조건

- 모듈 1 (슬랙 ↔ 클코) 이미 완료
- 노션 계정
- 노션 API integration 만들 수 있어야 (5분, 무료)

---

## 단계별 안내

### Step 1. 노션 Integration 만들기

사용자에게:
```
1) https://notion.so/my-integrations 접속
2) "New integration" 클릭
3) 이름: "Agent Bootstrap" (또는 원하는 이름)
4) Associated workspace: 본인 워크스페이스 선택
5) Type: Internal
6) "Submit" 클릭
7) "Internal Integration Secret" 값(secret_... 또는 ntn_... 으로 시작)을 복사한 뒤,
   다음 파일을 열어서 NOTION_API_TOKEN= 줄에 직접 붙여넣어 주세요:
   ~/.claude/secrets/slack-jipsa.env

붙여넣고 저장하셨으면 "저장했어요" 라고 답해주세요.
(토큰 자체는 저에게 보내지 마세요 — 제가 파일에서 직접 읽습니다.)
```

AI 후속 동작: 사용자가 답하면 Bash로 `~/.claude/secrets/slack-jipsa.env` 의 `NOTION_API_TOKEN` 값이 비어있지 않은지 확인. 비어있으면 다시 안내.

### Step 2. 노션 페이지 준비

사용자에게:
```
노션에서 빈 페이지 하나를 만들거나 기존 페이지를 선택해주세요.
(이 페이지 안에 제가 DB를 만들겠습니다.)

페이지 우상단 ⋯ → "Connections" → "Connect to" → 방금 만든 "Agent Bootstrap" 추가

그 다음 페이지 URL을 저에게 알려주세요. 예:
https://www.notion.so/내-워크스페이스/페이지제목-32자hex
```

URL에서 32자 hex (`{parent_page_id}`) 추출.

### Step 3. AI 작업 — DB 자동 생성

검증된 스키마로 DB 두 개 생성. 사용자 .env의 `NOTION_API_TOKEN`과 Step 2에서 받은 `{parent_page_id}` 사용.

**Notion API 버전**:
- DB 생성 (`databases.create`): `Notion-Version: 2022-06-28` (안정적, 컬럼 스키마가 단순)
- 런타임 upsert (helper `lib/notion.py` 사용): `2025-09-03` (data_source 지원, helper가 자동 처리)

curl 호출 시 `-H "Notion-Version: 2022-06-28"` 필수.

#### DB 1: Claude Code 턴 로그

POST `https://api.notion.com/v1/databases`:

```json
{
  "parent": { "type": "page_id", "page_id": "{parent_page_id}" },
  "title": [{ "type": "text", "text": { "content": "Claude Code 턴 로그" } }],
  "properties": {
    "프로젝트": { "title": {} },
    "시각": { "date": {} },
    "세션 ID": { "rich_text": {} },
    "작업 디렉토리": { "rich_text": {} },
    "시킨 일": { "rich_text": {} },
    "한 일": { "rich_text": {} },
    "결과": { "rich_text": {} },
    "확인 필요": { "rich_text": {} },
    "모델": { "select": { "options": [
      { "name": "opus", "color": "purple" },
      { "name": "sonnet", "color": "blue" },
      { "name": "haiku", "color": "green" },
      { "name": "unknown", "color": "gray" }
    ]}},
    "도구 호출 수": { "number": {} },
    "전체 요약": { "rich_text": {} },
    "external_id": { "rich_text": {} }
  }
}
```

생성된 `id`(`{NOTION_SESSION_DB}`)를 사용자 .env에 저장.

#### DB 2 (옵션): 일일 통합

사용자에게 물어보기:
```
"일일 통합" DB 패턴이 있습니다 — 매일 row 한 줄이 그날 모든 세션을 relation으로 묶음.
이것도 만들까요?
① 네, 만들어주세요 (일별 hub 패턴)
② 아니요, Claude 턴 로그만으로 충분합니다
```

①이면 두 번째 DB 생성:

```json
{
  "parent": { "type": "page_id", "page_id": "{parent_page_id}" },
  "title": [{ "type": "text", "text": { "content": "일일 통합" } }],
  "properties": {
    "이름": { "title": {} },
    "날짜": { "date": {} },
    "상태": { "status": { "options": [
      { "name": "진행 중" }, { "name": "완료" }, { "name": "보류" }
    ]}},
    "external_id": { "rich_text": {} }
  }
}
```

생성 후 Claude Code 턴 로그 DB에 relation 컬럼 "📊 일일 통합" 추가 — `databases.update`:
```json
{
  "properties": {
    "📊 일일 통합": {
      "relation": {
        "database_id": "{NOTION_DAILY_DB}",
        "type": "single_property",
        "single_property": {}
      }
    }
  }
}
```

생성된 `id`를 `NOTION_DAILY_DB` 환경변수에 저장.

### Step 4. AI 작업 — 환경변수 추가

`~/.claude/secrets/slack-jipsa.env` 에 추가:
```
NOTION_API_TOKEN=secret_...
NOTION_SESSION_DB={Step 3에서 받은 DB1 ID}
NOTION_DAILY_DB={Step 3에서 받은 DB2 ID, 옵션}
```

또는 별도 파일:
```
~/.claude/secrets/notion.env
```

### Step 5. AI 작업 — lib 의존성 카피

검증 코드는 슬랙·노션 helper를 사용. 키트의 `templates/lib/` 폴더 내용을 사용자 환경에 복사:

```bash
mkdir -p ~/.claude/scripts/lib
cp templates/lib/notion.py ~/.claude/scripts/lib/notion.py
cp templates/lib/slack_mrkdwn.py ~/.claude/scripts/lib/slack_mrkdwn.py
cp templates/lib/md_to_notion.py ~/.claude/hooks/md_to_notion.py
touch ~/.claude/scripts/lib/__init__.py
```

(macOS/Linux). Windows는 `Copy-Item` 으로 동일 동작.

### Step 6. AI 작업 — Stop hook 등록

`templates/hooks/slack-session-summary.sh` 와 `templates/hooks/append_turn_raw.py` 를 `~/.claude/hooks/` 로 복사:
```bash
cp templates/hooks/slack-session-summary.sh ~/.claude/hooks/
cp templates/hooks/append_turn_raw.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/slack-session-summary.sh
```

`~/.claude/settings.json` Read → `hooks.Stop` 배열에 append (기존 hook 보존):
```json
{
  "type": "command",
  "command": "$HOME/.claude/hooks/slack-session-summary.sh"
}
```

`env` 섹션에 추가:
```json
"env": {
  "SLACK_SESSION_WEBHOOK": "https://hooks.slack.com/...",
  "NOTION_API_TOKEN": "secret_...",
  "NOTION_SESSION_DB": "...",
  "NOTION_DAILY_DB": "..."
}
```

> 또는 hook 내부에서 `~/.claude/secrets/slack-jipsa.env` 를 직접 읽도록 수정. 권장은 settings.json env (Claude Code가 자동 주입).

### Step 7. 검증

사용자에게:
```
1) 새 터미널에서: claude --print "1+1"
2) 응답 후 약 5초 기다리기
3) 노션의 "Claude Code 턴 로그" DB 열어보기

새 row가 생성되었나요?
- 프로젝트: (현재 폴더 이름)
- 시킨 일: "1+1"
- 결과: Claude 응답

또 모듈 1의 슬랙 채널에서 메시지 보내고 "Claude Code 턴 로그" DB에 새 row가 생기는지 확인.
```

안 되면 진단:
```bash
tail -30 /tmp/slack-session-summary.log
```

---

## 트러블슈팅

| 증상 | 진단 | 해결 |
|------|------|------|
| 노션에 row 안 생김 | `tail -30 /tmp/slack-session-summary.log` | hook이 발동했는지 확인 |
| `ImportError: lib.notion` | `ls ~/.claude/scripts/lib/notion.py` | Step 5 다시 |
| Notion `validation_error` | 컬럼 스키마 불일치 | Step 3 DB 생성 페이로드와 실제 DB 컬럼명 일치 확인. 노션 GUI에서 컬럼 변경했을 가능성 |
| `unauthorized` (401) | Integration 권한 | 노션 페이지 → Connections → Integration 추가 (Step 2) |
| 일일 통합 relation 안 생김 | NOTION_DAILY_DB 환경변수 | Step 4 |

---

## 키트 범위

이 키트는 가장 **핵심 한 가지** — Claude Code 모든 세션을 노션에 누적 — 만 일반화했습니다. 검증 코드의 다른 확장(이메일·카톡·SMS 통합 hub, 일별 회고 cron 등)은 사용자가 같은 패턴으로 직접 추가 가능.

---

## 활용

노션 DB에 누적되면:
- **검색**: "지난주 무슨 작업 했지?" → 노션 검색
- **회고**: 일별 통합 row 보고 그날 패턴 파악
- **공유**: 특정 row만 골라서 외부 공유 가능
- **분석**: 모델별·프로젝트별 통계 (노션 view·formula)

자기 작업을 외부에서 다시 볼 수 있는 거울. 누적된 데이터가 클수록 검색·패턴 인식 가치가 커집니다.

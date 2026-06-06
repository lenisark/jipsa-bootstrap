# jipsa-bootstrap

> **AI가 직접 깔아주는 "개인 집사 + 부서 공용 집사" 멀티채널 슬랙 비서 셋업 키트**

이 키트를 Claude Code에 던지면, Claude가 1:1로 안내하며 **데몬 하나로 여러 슬랙 채널을 굴리는 멀티채널 비서**를 깔아줍니다. 개인용은 Opus 풀권한, 부서 공용은 Sonnet 샌드박스 — 채널마다 모델·권한·작업폴더가 분리됩니다.

[`agent-bootstrap`](https://github.com/orot-ai/agent-bootstrap)의 상위호환 키트입니다. 입문용 단일채널 브릿지에 더해, 실제 운영 중인 멀티채널 권한분리 + 부서 집사 기능 전체를 담았습니다.

---

## 무엇을 깔아주나요?

| 모듈 | 무엇 | 비고 |
|------|------|------|
| **1. 슬랙 ↔ 클로드 코드** | 슬랙 채널에서 클로드 코드와 대화. 메시지 → 즉시 응답 | 기반 |
| **2. 폴더 트리거 자동화** | 특정 폴더에 파일 떨어뜨리면 Claude가 자동 처리 | 선택 |
| **3. 슬랙 + 폴더 합치기** | 폴더 변화 → 슬랙 알림 + 자동 처리 | 선택 |
| **4. 노션 자동 적재** | 모든 세션·슬랙 대화를 노션 DB에 누적 | 선택 |
| **5. 멀티채널 권한분리** | `channels.json`으로 채널별 모델·권한·샌드박스 분리 | ★ 핵심 |
| **6. 개인 집사** | Opus · 홈 풀권한 · 본인 전용 | ★ |
| **7. 부서 공용 집사** | Sonnet · 샌드박스 · @멘션 · 알리미/완료체크/투표/위키/요약/FAQ | ★ |

### 부서 집사 기능

⏰ 알리미(매월·매주·매일·1회성 + 사전알림 + 담당자 멘션 + 주말·공휴일 직전영업일 이동 + 목록/삭제) · ✅ 완료체크 · 🗳️ 투표/설문 · 📌 위키수집 · 📝 대화요약 · 📚 사내 FAQ/위키 · 👋 온보딩 안내

---

## 사용법

### 1. 키트 다운로드

```bash
git clone https://github.jipsa-bootstrap.git
cd jipsa-bootstrap
```

### 2. Claude Code 실행

```bash
claude
```

### 3. 다음 한 줄을 Claude에게 보내기

```
이 폴더의 SKILL.md를 읽고 셋업을 시작해줘.
```

Claude가 자동으로 OS 확인 → 깔 모듈 선택 → 슬랙 앱/토큰 안내 → `channels.json` 작성 → 코드 배치 → 자동시작 등록 → 검증까지 끌고 갑니다. 운영자는 토큰 복붙·클릭·"됐어" 답변만.

---

## 필수 준비물

| 항목 | 비용 | 비고 |
|------|------|------|
| **Claude Code 구독** | $$ | 전체 |
| **슬랙 워크스페이스** | 무료 | 채널 2개(개인·부서) |
| **Python 3** + `slack_sdk` `holidays` | 무료 | 데몬 |
| **OS** | — | Windows / macOS / Linux |

> AI가 OS를 묻고 자동 분기합니다 (Windows=Task IÈ^vé^¯ãèÁêÒée, macOS=launchd, Linux=systemd).

---

## 멀티채널 구조 한눈에

```
데몬 1개 (slack-jipsa/daemon.py)
  └─ channels.json 으로 채널별 라우팅
       ├─ 개인 채널 :  opus   · owner만 · cwd=slack-jipsa · add_dirs=[~]   (풀권한)
       └─ 부서 채널 :  sonnet · 모두    · cwd=slack-team  · add_dirs=[]    (샌드박스)
                        + Bash/Write/Edit 차단 · @멘션 필수
```

---

## 디자인 원칙

1. **AI가 리드한다** — 사용자가 가이드를 읽는 게 아니라 AI가 읽고 끌고 감
2. **검증된 코드 그대로** — `templates/` 안은 운영 환경에서 매일 도는 실코드. 결합은 `.env`·`channels.json`으로만
3. **시크릿은 로컬에만** — 토큰은 `~/.claude/secrets/`, 채널ID는 `channels.json`(둘 다 git 제외)
4. **부서 채널은 샌드박스** — 전용 폴더 밖 접근 차단 + 읽기/Q&A 전용으로 공용 안전성 확보

---

## 폴더 구조

```
jipsa-bootstrap/
├── README.md              ← 지금 이 파일
├── SKILL.md               ← AI 가이드 본체 (Claude가 읽음)
├── .env.example           ← 토큰 템플릿
├── modules/               ← 모듈별 단계 안내 (AI가 읽음)
│   ├── 01-slack-bridge.md
│   ├── 02-folder-trigger.md
│   ├── 03-bridge-trigger.md
│   ├── 04-notion-archive.md
│   ├── 05-multichannel.md       ← channels.json 권한·모델 분리
│   ├── 06-personal-jipsa.md     ← 개인 집사
│   └── 07-team-jipsa.md         ← 부서 집사
└── templates/
    ├── lib/               ← 검증 라이브러리 (그대로 카피)
    ├── hooks/             ← Stop hook
    ├── scripts/
    │   ├── slack-jipsa/   ← 멀티채널 daemon.py + reminders.py + channels.json.example + CLAUDE.md
    │   └── slack-team/    ← 부서 작업폴더 (CLAUDE.md + docs/FAQ)
    ├── run.ps1.tmpl / run.sh.tmpl
    ├── win-task-*.xml.tmpl        ← Windows Task Scheduler
    ├── launchd-*.plist.tmpl       ← macOS
    └── systemd-*.tmpl             ← Linux
```

## 코드 출처

`templates/scripts/`, `templates/lib/`, `templates/hooks/` 안의 코드는 **운영 환경에서 검증된 실코드 그대로**입니다. 환경별 결합은 `.env`·`channels.json`·`.tmpl` 치환으로 처리되어 코드 자체는 수정 불필요합니다.

## 라이선스

MIT. 자유롭게 가져다 쓰세요.

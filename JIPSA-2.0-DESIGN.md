# jipsa-bootstrap 2.0 설계 문서

> **한 줄 정의:** "대시보드 없는 관제탑." AgentRQ가 웹 대시보드에서 하는
> *작업 객체 + 사람↔에이전트 양방향 + 승인 게이트*를, jipsa는 **슬랙 안에서**
> 한다. 로컬 단일 데몬·시크릿 로컬·비개발자 친화라는 1.x의 영혼은 그대로.

**Supersedes:** jipsa-bootstrap 1.x (멀티채널 권한분리 + 개인/부서 집사)
**스택 불변:** Python 3 · slack_sdk · 로컬 데몬 · Claude Code · MIT

---

## 0. 무엇이 바뀌나 (1.x → 2.0)

1.x의 한계는 모든 일이 **휘발성 대화**라는 점이다. 데몬이 재시작하면 진행 중이던
일이 사라지고, 에이전트는 사람에게 *먼저* 말을 걸 방법이 없다(사람→AI 단방향).

2.0은 세 가지를 더한다. 셋은 서로 맞물린다.

| 기둥 | 무엇 | 흡수 출처 |
|---|---|---|
| **A. 작업 객체** | 대화를 상태 가진 task로 승격 (대기/진행/막힘/완료) | AgentRQ task model |
| **B. 승인 게이트** | 민감 작업 직전 슬랙 `[승인][거부]` 후 대기 | AgentRQ `respondToTask` / approval_mode |
| **C. 2.0 모듈** | channels.json 확장 + 신규 모듈/파일 | AgentRQ workspace persona·supervisor |

**Non-goals (의도적으로 안 한다):** 웹 대시보드, MCP 전면 재설계, SaaS/멀티테넌트,
클라우드 저장. 전부 로컬·슬랙 네이티브 유지. DSAUTO HR 데이터 민감성과
"동료는 대시보드에 로그인 안 한다"는 현실 때문에 이건 타협 안 함.

---

## A. 작업 객체 (Task Layer)

### A-1. 저장소 선택 — SQLite (boring by default)

JSON 파일은 데몬과 스케줄러가 **동시에 쓸 때 깨질** 위험이 있다. 작업 객체는
승인 게이트의 랑데부 지점(아래 B-3)이라 원자성이 필수. 그래서 stdlib `sqlite3`
단일 파일 `~/.claude/jipsa/jipsa.db` 사용. 외부 의존성 0, 서버 0, 여전히 로컬 1파일.

### A-2. 스키마

```
tasks
─────────────────────────────────────────────────────────────
 id            TEXT  PK   (uuid4)
 channel_id    TEXT       (channels.json 의 채널)
 title         TEXT
 body          TEXT       (맥락/지시 원문)
 state         TEXT       대기 | 진행 | 막힘 | 완료 | 취소
 direction     TEXT       h2a (사람→에이전트) | a2h (에이전트→사람)
 assignee      TEXT       'agent' | <slack_user_id>
 thread_ts     TEXT       슬랙 스레드 앵커 (대화 연속성)
 created_at    INTEGER
 updated_at    INTEGER
 meta          TEXT       (json: reminder 연결, source action 등)

approvals  (게이트 1건 = 1행, task 와 1:1 또는 N:1)
─────────────────────────────────────────────────────────────
 token         TEXT  PK   (uuid4, 단일사용)
 task_id       TEXT  FK
 channel_id    TEXT
 action_desc   TEXT       "rm -rf ./build 실행" 처럼 사람이 읽을 설명
 status        TEXT       대기 | 승인 | 거부 | 만료
 approver_id   TEXT       실제 누른 사람 (검증 후 기록)
 requested_at  INTEGER
 decided_at    INTEGER
 expires_at    INTEGER
```

### A-3. 상태 기계

```
        생성
         │
         ▼
      ┌──────┐  착수    ┌──────┐  완료   ┌──────┐
      │ 대기 │────────▶│ 진행 │───────▶│ 완료 │
      └──────┘         └──────┘        └──────┘
         │                 │ 승인대기/외부블록
         │ 취소            ▼
         │             ┌──────┐ 해소  (다시 진행)
         └────────────▶│ 막힘 │──────────┘
                       └──────┘
```

### A-4. 인터페이스 (슬랙 명령 / 버튼)

- `작업목록` → 해당 채널 열린 task 카드 목록 (상태별)
- `작업 <id> 진행|막힘|완료` → 상태 변경 (또는 카드 버튼)
- a2h task 생성 시 → 담당자 멘션 + 스레드 자동 개설

> 1.x의 알리미·FAQ·투표도 점진적으로 이 task 위에 얹으면 현황이 한 곳에 모인다.
> (필수는 아님 — 신규 기능부터 붙이고 기존 기능은 그대로 둬도 됨. 최소 diff 원칙.)

---

## B. 승인 게이트 (Approval Gate) — 핵심

### B-1. 두 가지 발동 지점

**(1) 개인 채널 — 민감 작업 차단 게이트**
개인 채널은 Opus 풀권한이지만, Bash/Write/Edit/네트워크 등 민감 도구 호출 직전에
"그냥 실행"이 아니라 슬랙에 `[승인][거부]`를 띄우고 **대기**한다. 자리 비운 사이
폰으로 승인 → 이어서 진행. (AgentRQ가 웹에서 하던 걸 슬랙으로.)

**(2) 부서 채널 — 권한 상승 요청(escalation)**
지금은 부서 집사가 Write를 **하드 차단**당한다. 2.0은 "차단" 대신
"**승인 요청 a2h task 생성 → 지정 승인자(예: 인사총무 담당) 멘션**"으로 바꾼다.
승인되면 통제된 경로로 실행. → 안전성 유지하면서 부서 집사가 할 수 있는 일이 늘어난다.

### B-2. 슬랙 버튼 흐름 (검증된 패턴 재사용)

사직서 N단계 결재 / 수습평가 봇에서 쓴 **버튼 → UUID 토큰 → 승인자 검증 → 상태 전진**
패턴을 그대로 가져온다. (그쪽은 GAS, jipsa는 Python이라 *코드 이식이 아니라
같은 패턴을 데몬 안에서 재구현*.)

```
 [에이전트]            [PreToolUse 훅]         [jipsa.db]          [데몬]            [슬랙/사람]
     │ 민감 도구 호출       │                      │                 │                   │
     │────────────────────▶│ approvals 행 INSERT  │                 │                   │
     │                     │ (status=대기,token)  │────────────────▶│                   │
     │                     │ 슬랙 카드 게시 요청  │                 │ Block Kit 카드    │
     │                     │                      │                 │ [승인][거부]      │
     │                     │                      │                 │──────────────────▶│
     │                     │ ── DB 폴링(대기) ──  │                 │                   │ 클릭(token)
     │                     │                      │                 │◀──────────────────│
     │                     │                      │ 검증: 토큰유효?  │                   │
     │                     │                      │ 승인자권한?중복? │                   │
     │                     │                      │ status=승인/거부 │                   │
     │                     │ ◀── 상태변경 감지 ── │◀────────────────│ 스레드에 결과 게시│
     │ exit 0(허용)/block  │                      │                 │                   │
     │◀────────────────────│                      │                 │                   │
```

### B-3. 핵심 메커니즘 — DB를 랑데부로

PreToolUse 훅은 Claude Code가 띄우는 **별도 프로세스**다. 훅이:
1. `approvals` 행 INSERT(status=대기) + 슬랙 카드 게시,
2. 그 행의 status가 바뀔 때까지 **DB 폴링**(또는 짧은 sleep 루프),
3. 승인이면 `exit 0`(도구 실행 허용), 거부/만료면 차단 응답.

데몬은 버튼 클릭(block_actions) 이벤트를 받아 행을 UPDATE. **메모리 결합 없음 →
데몬이나 세션이 죽었다 살아나도 상태가 DB에 남아 복구된다.** (새벽 3시 원칙.)

### B-4. 보안·검증

- **토큰:** uuid4, 단일사용, `task_id + channel + 허용 승인자 집합`에 바인딩. 재사용 시 거부.
- **승인자 검증:** 누른 사람 slack_id ∈ `channels.json[채널].approvers` 인지 확인. 아니면 ephemeral "권한 없음".
- **타임아웃:** 무응답 N분 → **기본 거부**(안전쪽), task=막힘, 요청자에게 알림.
- **감사:** 모든 verdict(누가·언제·무엇을) 노션 아카이브(모듈 04)에 적재 → HR 컴플라이언스/문서관리 습관과 연결.

### B-5. 엣지 케이스 (두텁게)

| 상황 | 처리 |
|---|---|
| 승인자 둘이 동시에 클릭 | compare-and-set / UNIQUE → 첫 verdict만 채택, 나머지 "이미 처리됨" |
| 게이트 대기 중 데몬 재시작 | 상태가 DB에 있어 복구. 훅은 계속 폴링, 타임아웃은 `requested_at` 기준 유효 |
| 만료 후 뒤늦게 클릭 | status=만료면 버튼 무효 + 안내 |
| 부서원이 허용 도구 없는 요청 | 무한 승인요청 금지 — 정중히 답/거절, task 생성 안 함 |
| 토큰 리플레이 | 단일사용 소진 표시, 재시도 거부 |
| 서브에이전트/중첩 호출의 민감 작업 | PreToolUse는 전역 → 게이트 그대로 발동 |

---

## C. jipsa 2.0 모듈 구조

### C-1. channels.json 2.0 (필드 추가)

```jsonc
{
  "C0PERSONAL": {
    "model": "opus", "owner_only": true,
    "cwd": "slack-jipsa", "add_dirs": ["~"],
    "mission": "오롯이 내 개인 비서. 빌드/배포 보조.",   // ← 신규: 페르소나
    "tasks_enabled": true,                              // ← 신규
    "gate": {                                            // ← 신규: 승인 게이트
      "enabled": true,
      "sensitive_tools": ["Bash", "Write", "Edit"],
      "approvers": ["U_OWNER"],
      "timeout_min": 15,
      "on_timeout": "deny"
    }
  },
  "C0TEAM": {
    "model": "sonnet", "mention_required": true,
    "cwd": "slack-team", "add_dirs": [],
    "mission": "부서 공용 집사. 알리미/FAQ/투표/요약/온보딩.",
    "tasks_enabled": true,
    "gate": {
      "enabled": true,
      "mode": "escalate",                 // 차단 대신 승인요청
      "sensitive_tools": ["Bash", "Write", "Edit"],
      "approvers": ["U_HRADMIN"],
      "timeout_min": 60,
      "on_timeout": "deny"
    }
  }
}
```

> `gate.enabled: false`로 두면 1.x 동작 그대로 → **기능 플래그로 점진 롤아웃·즉시 롤백.**

### C-2. 신규/변경 파일

```
templates/scripts/slack-jipsa/
├── daemon.py            ← 변경: block_actions(버튼) 이벤트 핸들러 추가
├── lib/
│   ├── tasks.py         ← 신규: SQLite task store (create/list/update/state)
│   └── approval.py      ← 신규: 토큰 발급·검증, Block Kit 카드, verdict 적용
├── hooks/
│   ├── stop_hook.py     ← 기존
│   └── pretooluse_gate.py ← 신규: 민감 도구 게이트 훅 (DB 폴링 랑데부)
└── scheduler.py         ← reminders.py 진화: cron task를 에이전트가 실행+보고

modules/
├── 08-task-layer.md     ← 신규: 작업 객체 셋업 (AI 가이드)
├── 09-approval-gate.md  ← 신규: 승인 게이트 셋업
└── 10-active-scheduler.md ← 신규: 알리미 2.0 (능동 실행+보고)
```

### C-3. 알리미 2.0 (덤이지만 값어치 큼)

1.x 알리미는 사람한테 핑만 쏜다. 2.0은 *에이전트가 cron에 맞춰 직접 일하고 결과를
보고*하는 a2h task로 확장. 예: "매일 09:00 어제 미처리 FAQ 요약해 #부서 에 올려."
reminders.py에 실행+보고만 얹으면 됨(스케줄/공휴일이동 로직 재사용).

---

## D. 빌드 순서 (점진적·되돌릴 수 있게)

각 단계는 **독립적으로 출시 가능**하고, `gate.enabled` 플래그로 즉시 끌 수 있다.
한 번에 하나씩(구조 변경과 동작 변경을 동시에 안 함).

```
Phase A  작업 객체            lib/tasks.py + 작업목록/상태 명령
         (순수 추가, 동작 무변)  → 여기서 한 번 끊어서 써봄
              │
Phase B  개인 채널 게이트       pretooluse_gate.py + daemon 버튼 핸들러
         (블라스트 반경 최소)    + approval.py.  개인 채널만 켬
              │
Phase C  부서 escalate 게이트   부서 채널 gate.mode=escalate 확장
              │
Phase D  알리미 2.0            scheduler.py (능동 실행+보고)
```

**가장 임팩트 큰 A·B가 서로 맞물리고, 둘 다 이미 가진 부품(슬랙 버튼 결재 패턴)으로
시작 가능** — 이게 2.0의 척추다.

---

## E. 한 장 요약

- **왜:** 휘발성 대화 → 추적 가능한 작업 + 에이전트가 사람에게 먼저 보고/요청(양방향).
- **어떻게:** SQLite task 객체(A) 위에 슬랙 버튼 승인 게이트(B)를 얹고, channels.json
  플래그로 채널별 적용(C). 전부 로컬·슬랙 네이티브.
- **무엇을 안 하나:** 웹 대시보드·MCP 재설계·SaaS — jipsa의 영혼 사수.
- **재사용 자산:** N단계 슬랙 버튼 + UUID 토큰 결재 패턴(사직서/수습평가 봇)을 데몬에 재구현.
- **시작점:** Phase A(작업 객체) → Phase B(개인 채널 게이트).

# 비품관리 시스템 설계 (jipsa supply/inventory)

> **상태:** 설계 승인 대기 → 승인 후 writing-plans 로 구현 계획 작성.
> **한 줄:** 비품신청 **슬랙 리스트**를 jipsa가 주기적으로 읽어, 상태가 **수령완료**가 된 신청을 **품목 재고에서 수량만큼 차감**하고, **구글드라이브 .xlsx 재고/이력**을 갱신하며 슬랙으로 알린다. 입고(가산)는 명령/엑셀 편집.

## 1. 목표와 비목표

**목표**
- 비품신청의 출고 흐름(수령완료)을 자동으로 재고에 반영(차감).
- 품목별 현재 재고를 구글드라이브 .xlsx로 관리(팀이 보고/편집 가능).
- 저재고·신규품목·차감을 슬랙으로 알림.
- 지급 이력 누적.

**비목표 (이번 범위 밖)**
- 리스트에 상태를 되써넣기(jipsa는 리스트 **읽기 전용**. `lists:write`는 추후 옵션).
- 발주/구매 자동화, 결재 흐름.
- 비품신청 접수 자체(기존 슬랙 워크플로 유지).

## 2. 확정된 외부 사실 (실측)

- **리스트 ID:** `F0AGQLRGQKC` (워크스페이스 `T09KW78FQDR`)
- **API:** `slackLists.items.list`(읽기), `files.info`(스키마) — 봇 토큰 + `lists:read`로 동작 확인 완료(2026-06-23).
- **컬럼 매핑(실측):**
  | 의미 | column_id | 타입 |
  |---|---|---|
  | 품목명 | `Col0AG9D5TQTZ` | text |
  | 수량 | `Col0AGUEUTS7N` | number |
  | 총 금액 | `Col0AGUEZ0XQC` | number |
  | 사유 | `Col0AG9D8ACUX` | text |
  | 부서 | `Col0AGU94KCHJ` | text |
  | 상태 | `Col0AGJF67S83` | select |
  | 신청자 | `Col0AGMED767P` | created_by(user) |
  | 등록일 | `Col0AGJETNJRZ` | created_time |
- **상태 select 옵션ID → 라벨:**
  `OptHTH5FXVH=접수완료`, `OptZ5QEUGQT=배송요청`, `OptL941UNJ0=물품준비중`, `OptVTIM8OK4=배송중`, `OptAW3J5XY2=수령대기`, **`OptXGCU61KL=수령완료`**(차감 트리거), `OptYSI0Z9GS=반려`
- 레코드 구조: `item['id']`(=record_id), `item['fields']`(셀 배열; 각 셀 `{column_id, value, text/number/select/...}`).

## 3. 아키텍처

```
[슬랙 리스트 F0AGQLRGQKC]  ──(5분 폴링, lists:read)──▶  [supply.py]
                                                          │ 수령완료 신규 감지(record_id별 1회)
                                                          ▼
                          [G드라이브 비품재고.xlsx] ◀── 차감/가산(openpyxl, 원자적 쓰기)
                          [G드라이브 지급이력.xlsx] ◀── append
                          [supply_state.json]       ◀── 처리한 record_id·옵션ID맵 캐시(안전망)
                                                          │
                                                          ▼
                                              [슬랙 알림 채널]  차감/저재고/신규품목
```

- **`supply.py`** (daemon 형제, `reminders.py`와 동일 패턴): 폴링 루프 · 리스트 파싱 · 재고 차감/가산 · xlsx I/O · 알림. daemon이 `set_logger`/설정 주입 후 스레드 시작.
- **`xlsx 저장**: `openpyxl`로 read-modify-write. 임시파일→`os.replace` 원자적 교체. 엑셀 열림(`~$파일`) 감지 시 지연·재시도.
- **`supply_state.json`** (로컬, G드라이브 아님): `{counted: [record_id...], baseline_done: bool}` — 중복 차감 방지·재시작 복구.

## 4. 설정 (channels.json 와 별도: `supply.json` 또는 secrets)

```jsonc
{
  "list_id": "F0AGQLRGQKC",
  "poll_min": 5,
  "notify_channel": "C_부서채널ID",
  "stock_xlsx": "G:/공유 드라이브/인사총무팀_일반/97_집사의 생성물/비품재고.xlsx",
  "ledger_xlsx": "G:/공유 드라이브/인사총무팀_일반/97_집사의 생성물/지급이력.xlsx",
  "cols": {                       // 실측 매핑(변경 시 여기만 수정)
    "item": "Col0AG9D5TQTZ", "qty": "Col0AGUEUTS7N",
    "status": "Col0AGJF67S83", "dept": "Col0AGU94KCHJ",
    "amount": "Col0AGUEZ0XQC", "requester": "Col0AGMED767P"
  },
  "status_done_option": "OptXGCU61KL",   // 수령완료
  "default_min_qty": 1
}
```

## 5. 데이터 모델 — `비품재고.xlsx`

| 품목명 | 현재수량 | 최소수량 | 단위 | 비고 |
|---|---|---|---|---|
| (text, key) | number | number | text | text |

- **키 = 품목명(정확 일치)**. 리스트 품목명과 문자열이 같아야 매칭.
- 사람이 직접 입고/초기등록/오타수정 가능(양방향).

`지급이력.xlsx`: 일시 · 신청자 · 부서 · 품목명 · 수량 · 차감후잔여 · record_id

## 6. 동작 상세

**폴링 사이클(poll_min 분):**
1. `slackLists.items.list(list_id, limit=100, cursor...)` 전체 레코드 로드.
2. 각 레코드: `record_id, 품목, 수량, 상태옵션ID`.
3. `상태옵션ID == status_done_option(수령완료)` 그리고 `record_id ∉ counted` → **차감 대상**.
4. 차감: `비품재고.xlsx`에서 품목행 검색
   - 있으면 `현재수량 -= 수량`, 지급이력 append, `counted += record_id`, 슬랙 알림
     (`✅ {품목} {수량}개 지급(수령완료) — 잔여 {n}개`)
   - 없으면 **신규품목**: 재고표에 행 추가(현재수량=`-수량`, 최소수량=default)+ 알림
     (`⚠️ 재고표에 없는 품목 '{품목}' — 등록 필요`)
5. `현재수량 < 최소수량` → `⚠️ 저재고: {품목} {n}개 (최소 {m})`.

**최초 1회 baseline:** `baseline_done=false`면, 첫 폴링에서 현재 `수령완료`인 모든 record_id를 **차감 없이** `counted`에 등록(기존 이력은 이미 실물 반영됨) → `baseline_done=true`. 이후 신규 수령완료만 차감.

**입고(가산):** 슬랙 `입고 {품목} {수량}` (담당자만) → 재고표 `현재수량 += 수량` + 이력. 또는 사람이 xlsx 직접 편집.

## 7. 슬랙 명령 (notify_channel = 부서 채널에서 수신)

- `재고` / `재고 {품목}` — 현재 수량 조회 (누구나)
- `입고 {품목} {수량}` — 가산 (**담당자 한정** = `USER_SLACK_ID` + `supply.json.managers[]`)
- `비품현황` — 저재고 품목 요약 (누구나)

명령은 `notify_channel`에서만 처리(부서 채널). 권한 없는 사람이 `입고` 시 ephemeral "권한 없음".

## 8. 엣지 케이스 / 안전

| 상황 | 처리 |
|---|---|
| 같은 record 재폴링 | `counted` 집합으로 1회만 차감(멱등) |
| 데몬 재시작 | `supply_state.json` 복구, baseline 유지 |
| xlsx 엑셀로 열려있음 | `~$` 감지 → 지연·재시도, 실패 시 슬랙 경고(차감은 `counted`에 안 넣어 다음 폴링 재시도) |
| 동시 쓰기 | 임시파일+`os.replace` 원자적, 프로세스 내 락 |
| 품목명 불일치/오타 | 신규품목으로 처리 + 알림(사람이 통합) |
| 수령완료 후 되돌림/반려 | 이미 차감됨 — 자동 환원 안 함, `입고`로 수동 보정(알림에 안내) |
| 음수 재고 | 막지 않음(실물 출고 사실) + 경고 |
| 리스트 권한/네트워크 실패 | 로그 + 1회 슬랙 경고, 다음 주기 재시도 |
| 수량 비어있음/0 | 1로 보정하지 말고 스킵 + 경고(데이터 확인 요청) |

## 9. 테스트

- **단위(`tests/test_supply.py`)**: 가짜 리스트 레코드 + 임시 xlsx로
  - 수령완료 1건 차감 / 비수령완료 무차감 / record_id 중복 무차감 / baseline 무차감 / 신규품목 / 저재고 판정 / 입고 가산 / 수량0 스킵.
  - 리스트 파싱(컬럼ID→값) 매핑.
- **스모크**: 실제 리스트 1회 읽어 파싱 결과 출력(차감 없이 dry-run 모드).
- 슬랙/폴링 통합은 라이브 검증.

## 10. 단계 (phase)

- **P1 (핵심)**: 폴링 → 수령완료 차감 → 비품재고.xlsx + 지급이력.xlsx + 슬랙 알림 + baseline + dry-run 모드.
- **P2**: `재고`/`입고`/`비품현황` 명령 + 저재고/신규품목 경고 다듬기.
- **P3**: 일/주 재고 리포트(알리미 2.0 연동) → G드라이브 아카이브 + 슬랙 게시.

## 11. 전제(사전 준비)

1. ✅ 슬랙 앱 `lists:read`(+`lists:write`) — 완료.
2. Python **`openpyxl`** 설치(신규 의존성).
3. `비품재고.xlsx` 초기 등록(품목·현재고·최소수량) — G드라이브에. (없으면 baseline 후 신규품목으로 점차 채워지나, 정확하려면 초기 실사 권장.)
4. `supply.json` 작성(notify_channel 등).

## 12. 확인 필요 (사용자)

- **초기 재고 실사 데이터**가 있는지 — 있으면 `비품재고.xlsx`에 미리 채움(정확), 없으면 baseline 후 신규품목으로 점진 축적.
- `notify_channel`(알림/명령 받을 부서 채널) 확정.
- `입고` 담당자 목록(`managers`) — 기본은 본인(`USER_SLACK_ID`).
- 신청자 표시는 `created_by`(user) → 기존 `_resolve_name`으로 이름 변환.

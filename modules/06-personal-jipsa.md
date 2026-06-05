# 모듈 6 — 개인 집사 (Opus · 풀권한)

> 전제: 모듈 1·5 완료. channels.json에 개인 채널 항목이 있어야 함.
> 운영자 본인만 쓰는 개인 비서 채널. **Opus 모델 + 홈 디렉토리 풀 접근**으로, 일정·메모·파일 등 무엇이든 처리.

## 채널 정책

channels.json의 개인 채널 항목:

```json
"C_PERSONAL_CHANNEL_ID": {
  "label": "개인 비서",
  "model": "opus",
  "access": "owner",
  "cwd": "~/.claude/scripts/slack-jipsa",
  "add_dirs": ["~"]
}
```

- `access: owner` — `USER_SLACK_ID`(본인)만 응답. 다른 사람이 채널에 들어와도 무시.
- `add_dirs: ["~"]` — 홈 디렉토리 전체 접근 (풀권한). 개인 작업이므로 샌드박스 없음.
- `model: opus` — 가장 강력한 모델.
- `require_mention` 없음 — 멘션 없이 말 걸면 바로 응답.

## 페르소나 (CLAUDE.md)

`templates/scripts/slack-jipsa/CLAUDE.md`를 `~/.claude/scripts/slack-jipsa/CLAUDE.md`로 배치한다 (모듈 1에서 daemon과 함께 이미 복사됐을 수 있음). 이 파일이 개인 집사의 절대 규칙 — 페르소나, 슬랙 mrkdwn 형식, 도구 호출 제한, 알리미 안내 규칙을 담는다.

운영자가 원하면 이 CLAUDE.md를 본인 취향대로 수정하도록 안내 (호칭·말투·금지사항 등). 코드 수정이 아니라 규칙 파일 수정이므로 자유롭게.

## 부가 기능

개인 채널도 데몬 공통 기능(알리미·투표·위키수집·요약·도움말)을 그대로 쓴다 — 자세한 사용법은 [모듈 7](07-team-jipsa.md)과 동일. 단 개인 채널은 `require_mention`이 없어 `@집사` 없이 바로 명령하면 된다 (예: `매일 9시에 약 먹기 알려줘`).

## 셋업 순서 (AI가 진행)

1. **CLAUDE.md 확인/배치** — `~/.claude/scripts/slack-jipsa/CLAUDE.md` 존재 확인. 없으면 templates에서 복사.
2. **channels.json 개인 항목 확인** — 모듈 5에서 작성됨.
3. **검증** — 개인 채널에서 `안녕` → 집사가 응답. `오늘 날짜 알려줘` 같은 간단 요청으로 Opus 동작 확인.
4. **(선택) 풀권한 테스트** — `~/Documents 안에 파일 몇 개 있는지 알려줘` 등으로 add_dirs 접근 확인.

## 주의

- 개인 채널은 풀권한(`add_dirs:["~"]`)이라 **반드시 본인만 들어와야** 한다. `access: owner` 게이트가 1차 방어지만, 채널 자체를 비공개로 두고 본인만 멤버로 유지할 것.
- 토큰·시크릿은 `~/.claude/secrets/`에만. 개인 집사에게 "내 비번 뭐야" 같은 요청은 하지 말 것 (로그에 남음).

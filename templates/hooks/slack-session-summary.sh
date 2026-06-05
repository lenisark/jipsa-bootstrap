#!/usr/bin/env bash
# Claude Code Stop hook — 세션 1턴 종료 시 Slack + Notion 이중 적재.
#
# stdin: Claude Code Hook JSON { session_id, cwd, ... }
# env (~/.claude/settings.json 의 env 에 설정):
#   SLACK_SESSION_WEBHOOK   필수 (Slack 미전송 시 공란)
#   NOTION_API_TOKEN        옵션 (있으면 Notion 에도 append)
#   NOTION_SESSION_DB       옵션 (DB ID, NOTION_API_TOKEN 과 함께)
set -uo pipefail

# ══════════════════════════════════════════════════════════════════
# ★ 외부 배치 호출 가드 (카톡 브리핑 등)
# ══════════════════════════════════════════════════════════════════
# 배치 스크립트가 CLAUDE_SKIP_SUMMARY=1 또는 CLAUDE_SKIP_HOOKS=1 로 호출하면 즉시 종료.
if [[ "${CLAUDE_SKIP_SUMMARY:-0}" == "1" || "${CLAUDE_SKIP_HOOKS:-0}" == "1" ]]; then
  exit 0
fi

# ══════════════════════════════════════════════════════════════════
# ★ 무한 재귀 방지 가드 (CRITICAL)
# ══════════════════════════════════════════════════════════════════
# helper 스크립트나 향후 하위 프로세스가 Stop hook 을 다시 발동시켜도 한 번만 실행.
# 환경변수는 자식 프로세스로 상속되므로 한 번 세팅하면 모든 하위 훅 호출이 즉시 exit.
if [[ -n "${SLACK_HOOK_RUNNING:-}" ]]; then
  printf '[%s] recursion guard hit (pid=%d ppid=%d), exiting\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$$" "$PPID" \
    >> "${HOOK_LOG:-/tmp/slack-session-summary.log}" 2>/dev/null || true
  exit 0
fi
export SLACK_HOOK_RUNNING=1

SLACK_URL="${SLACK_SESSION_WEBHOOK:-}"
NOTION_TOKEN="${NOTION_API_TOKEN:-}"
NOTION_DB="${NOTION_SESSION_DB:-}"
NOTION_VERSION="${NOTION_VERSION:-2022-06-28}"

if [[ -z "$SLACK_URL" && -z "$NOTION_TOKEN" ]]; then
  exit 0  # 어떤 대상도 설정 없으면 조용히 종료
fi

STDIN_JSON=$(cat)
SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
CWD=$(printf '%s' "$STDIN_JSON" | jq -r '.cwd // empty' 2>/dev/null)
[[ -z "$SESSION_ID" ]] && exit 0
[[ -z "$CWD" ]] && CWD="$(pwd)"
PROJECT_NAME=$(basename "$CWD")

TRANSCRIPT=$(find "$HOME/.claude/projects" -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)
[[ ! -f "$TRANSCRIPT" ]] && exit 0

TURN_INDEX=$(jq -rs '
  def text_content:
    if type == "string" then .
    elif type == "array" then
      map(select(type == "object" and .type == "text") | .text // "")
      | join("\n")
    else "" end;
  def is_real_user:
    .type == "user"
    and ((.message.content | text_content) as $txt
      | (($txt | gsub("[[:space:]]"; "")) != "")
      and (($txt | test("^\\s*<task-notification>")) | not));
  map(select(.type == "user") | select(is_real_user)) | length
' "$TRANSCRIPT" 2>/dev/null || echo "0")
[[ -z "$TURN_INDEX" || "$TURN_INDEX" == "0" ]] && TURN_INDEX=1

# ── 이번 턴에서 실제로 사용된 모델 추출 ────────────────────────────
# 같은 턴에서 여러 모델 쓰였을 수 있음 (Task로 sub-agent 등). 가장 많이 쓰인 모델 선택.
SESSION_MODEL=$(jq -rs '
  def text_content:
    if type == "string" then .
    elif type == "array" then
      map(select(type == "object" and .type == "text") | .text // "")
      | join("\n")
    else "" end;
  def is_real_user:
    .type == "user"
    and ((.message.content | text_content) as $txt
      | (($txt | gsub("[[:space:]]"; "")) != "")
      and (($txt | test("^\\s*<task-notification>")) | not));
  map(select(.type == "user" or .type == "assistant"))
  | (. as $all
     | ([range(length) | select($all[.] | is_real_user)] | last) as $idx
     | if $idx == null then $all else $all[$idx:] end)
  | map(select(.type == "assistant" and .message.model != null) | .message.model)
  | if length == 0 then "unknown"
    else group_by(.) | map({m: .[0], c: length}) | sort_by(-.c) | .[0].m
    end
' "$TRANSCRIPT" 2>/dev/null || echo "unknown")

# 모델명 축약 (claude-opus-4-7 → opus, claude-sonnet-4-5 → sonnet, 등)
MODEL_SHORT=$(printf '%s' "$SESSION_MODEL" | sed -E '
  s/^claude-opus-[0-9.-]+.*$/opus/;
  s/^claude-sonnet-[0-9.-]+.*$/sonnet/;
  s/^claude-haiku-[0-9.-]+.*$/haiku/;
  s/^claude-3-5-sonnet.*$/sonnet-3.5/;
  s/^claude-3-5-haiku.*$/haiku-3.5/;
  s/^claude-3-opus.*$/opus-3/;
')
[[ -z "$MODEL_SHORT" ]] && MODEL_SHORT="unknown"

# ── 마지막 "진짜 user" 턴 이후 사실 기록 추출 ─────────────────────────
# Claude Code JSONL 의 .type=="user" 중 대부분은 tool_result. 실제 사용자 입력만 필터.
extract_turn_data() {
  jq -rs --arg model "$SESSION_MODEL" --arg cwd "$CWD" --arg user "${USER_NAME:-사용자}" '
    def text_content:
      if type == "string" then .
      elif type == "array" then
        map(select(type == "object" and .type == "text") | .text // "")
        | join("\n")
      else "" end;

    def is_real_user:
      .type == "user"
      and ((.message.content | text_content) as $txt
        | (($txt | gsub("[[:space:]]"; "")) != "")
        and (($txt | test("^\\s*<task-notification>")) | not));

    def epoch:
      sub("\\.[0-9]+Z$"; "Z")
      | try fromdateiso8601 catch null;

    def summarize_names($names):
      (reduce $names[] as $n (
        {order: [], counts: {}};
        if .counts[$n] then .counts[$n] += 1
        else (.order += [$n] | .counts[$n] = 1)
        end
      )) as $s
      | if ($s.order | length) == 0 then "(도구 없음)"
        else
          $s.order
          | map(. as $n
              | if $s.counts[$n] == 1 then $n
                else "\($n) x\($s.counts[$n])"
                end)
          | join(", ")
        end;

    map(select(.type == "user" or .type == "assistant"))
    | (. as $all
       | ([range(0; length) | select($all[.] | is_real_user)] | last) as $idx
       | if $idx == null then [] else $all[$idx:] end) as $turn
    | ($turn[0].message.content | text_content) as $user_prompt_full
    | ([
        $turn[]
        | select(.type == "assistant")
        | (.message.content // [])
        | if type == "array" then
            .[] | select(type == "object" and .type == "tool_use") | .name // "?"
          else empty end
      ]) as $tool_names
    | ([
        $turn[]
        | select(.type == "assistant")
        | (.message.content // "")
        | if type == "string" then .
          elif type == "array" then
            map(select(type == "object" and .type == "text") | .text // "")
            | join("\n")
          else "" end
        | select(. != "")
      ]) as $assistant_texts
    | ($assistant_texts | last // "") as $assistant_text_full
    | ($assistant_texts | join("\n\n")) as $assistant_text_all
    | ($turn | map(.timestamp // "" | epoch) | map(select(. != null))) as $times
    | {
        task: ($user_prompt_full | .[0:200]),
        actions: summarize_names($tool_names),
        result: ($assistant_text_full | .[0:200]),
        action_items: [],
        raw_full: (
          ($user // "사용자") + "\n" + ($user_prompt_full // "") +
          "\n\n도구 사용\n" + summarize_names($tool_names) +
          "\n\nClaude\n" + ($assistant_text_all // "")
        ),
        metadata: {
          model: $model,
          tool_count: ($tool_names | length),
          duration_sec: (if ($times | length) >= 2 then (($times[-1] - $times[0]) | floor) else 0 end),
          cwd: $cwd
        }
      }
  ' "$1" 2>/dev/null
}

TURN_DATA=$(extract_turn_data "$TRANSCRIPT")

[[ -z "$TURN_DATA" ]] && exit 0

TASK=$(printf '%s' "$TURN_DATA" | jq -r '.task // ""')
ACTIONS_MD=$(printf '%s' "$TURN_DATA" | jq -r '.actions // "(도구 없음)"')
RESULT_TXT=$(printf '%s' "$TURN_DATA" | jq -r '.result // ""')
ACTION_ITEMS_MD="없음"
RAW_FULL=$(printf '%s' "$TURN_DATA" | jq -r '.raw_full // ""')
TOOL_COUNT=$(printf '%s' "$TURN_DATA" | jq -r '.metadata.tool_count // 0')
DURATION_SEC=$(printf '%s' "$TURN_DATA" | jq -r '.metadata.duration_sec // 0')

if [[ -z "$TASK" && "${TOOL_COUNT:-0}" -eq 0 ]]; then
  exit 0
fi

# ── 디버그 로그 ────────────────────────────────────────────────────
HOOK_LOG="${HOOK_LOG:-/tmp/slack-session-summary.log}"
_log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$HOOK_LOG" 2>/dev/null || true; }
_log "hook start session=$SESSION_ID project=$PROJECT_NAME tool_count=$TOOL_COUNT duration_sec=$DURATION_SEC raw_full_bytes=${#RAW_FULL} slack=$([ -n "$SLACK_URL" ] && echo Y || echo N) notion=$([ -n "$NOTION_TOKEN" ] && echo Y || echo N)"

# ── Slack 전송 ─────────────────────────────────────────────────────
if [[ -n "$SLACK_URL" ]]; then
  TS_HM=$(TZ=Asia/Seoul date '+%H:%M')
  SHORT_SESSION="${SESSION_ID:0:8}"
  SLACK_BODY=$(cat <<SB
🎯 *시킨 일*
${TASK}

📝 *한 일*
${ACTIONS_MD}

🧠 *결과*
${RESULT_TXT}

⚠️ *확인 필요*
${ACTION_ITEMS_MD}
SB
)
  # GitHub markdown → Slack mrkdwn
  # 1) **bold** → *bold*
  # 2) # / ## ~ ###### Header → *Header*
  # 3) [text](url) → <url|text>
  # 4) ~~strike~~ → ~strike~
  SLACK_BODY=$(printf '%s' "$SLACK_BODY" \
    | sed -E 's/\*\*([^*]+)\*\*/*\1*/g' \
    | sed -E 's/^[[:space:]]*#{1,6}[[:space:]]+(.+)$/*\1*/' \
    | sed -E 's/\[([^][]+)\]\(([^)[:space:]]+)\)/<\2|\1>/g' \
    | sed -E 's/~~([^~]+)~~/~\1~/g')

  SLACK_PAYLOAD=$(jq -n \
    --arg project "$PROJECT_NAME" \
    --arg cwd "$CWD" \
    --arg ts "$TS_HM" \
    --arg session "$SHORT_SESSION" \
    --arg body "$SLACK_BODY" '
    {
      blocks: [
        { type: "header", text: { type: "plain_text", text: "🤖 \($project)" } },
        { type: "context", elements: [
            { type: "mrkdwn", text: "⏰ \($ts) KST  ·  세션 `\($session)`" }
          ]
        },
        { type: "divider" },
        { type: "section", text: { type: "mrkdwn", text: $body } },
        { type: "context", elements: [
            { type: "mrkdwn", text: "📁 `\($cwd)`" }
          ]
        },
        { type: "divider" }
      ],
      text: "Claude 턴 · \($project)"
    }')

  curl -sS -X POST "$SLACK_URL" \
    -H 'Content-Type: application/json' \
    -d "$SLACK_PAYLOAD" >/dev/null 2>&1 || true
fi

# ── Notion 전송 ─────────────────────────────────────────────────────
if [[ -n "$NOTION_TOKEN" && -n "$NOTION_DB" ]]; then
  TS_ISO=$(TZ=Asia/Seoul date '+%Y-%m-%dT%H:%M:%S+09:00')
  FULL_SUMMARY=$(cat <<FS
🎯 시킨 일
${TASK}

📝 한 일
${ACTIONS_MD}

🧠 결과
${RESULT_TXT}

⚠️ 확인 필요
${ACTION_ITEMS_MD}
FS
)

  # 각 필드 Notion 2000자 제한 안전 절삭
  notion_trim() { printf '%s' "$1" | head -c 1900; }
  TASK_T=$(notion_trim "$TASK")
  ACTIONS_T=$(notion_trim "$ACTIONS_MD")
  RESULT_T=$(notion_trim "$RESULT_TXT")
  ACTION_ITEMS_T=$(notion_trim "$ACTION_ITEMS_MD")
  FULL_T=$(notion_trim "$FULL_SUMMARY")

  NOTION_PAYLOAD=$(jq -n \
    --arg db "$NOTION_DB" \
    --arg project "$PROJECT_NAME" \
    --arg ts "$TS_ISO" \
    --arg session "$SESSION_ID" \
    --arg cwd "$CWD" \
    --arg task "$TASK_T" \
    --arg actions "$ACTIONS_T" \
    --arg result "$RESULT_T" \
    --arg items "$ACTION_ITEMS_T" \
    --arg summary "$FULL_T" \
    --arg model "$MODEL_SHORT" \
    --argjson tool_count "$TOOL_COUNT" '{
      parent: { database_id: $db },
      properties: {
        "프로젝트": { title: [ { text: { content: $project } } ] },
        "시각": { date: { start: $ts } },
        "세션 ID": { rich_text: [ { text: { content: $session } } ] },
        "작업 디렉토리": { rich_text: [ { text: { content: $cwd } } ] },
        "시킨 일": { rich_text: [ { text: { content: $task } } ] },
        "한 일": { rich_text: [ { text: { content: $actions } } ] },
        "결과": { rich_text: [ { text: { content: $result } } ] },
        "확인 필요": { rich_text: [ { text: { content: $items } } ] },
        "모델": { select: { name: $model } },
        "도구 호출 수": { number: $tool_count },
        "전체 요약": { rich_text: [ { text: { content: $summary } } ] }
      }
    }')

  EXTERNAL_ID="claude:${SESSION_ID}:${TURN_INDEX}"
  TS_DATE="${TS_ISO:0:10}"
  NOTION_RESPONSE=$(NOTION_PAYLOAD="$NOTION_PAYLOAD" NOTION_EXTERNAL_ID="$EXTERNAL_ID" NOTION_API_TOKEN="$NOTION_TOKEN" NOTION_DB="$NOTION_DB" TS_DATE="$TS_DATE" python3 - <<'PY' 2>/dev/null || echo ""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
from lib.notion import upsert_by_external_id

# 일일 통합 DB ID — 사용자 환경변수. 비어있으면 일일 통합 relation 생략.
DAILY_DB = os.environ.get("NOTION_DAILY_DB", "")
H = {
    "Authorization": f'Bearer {os.environ["NOTION_API_TOKEN"]}',
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def get_or_create_daily(date_str):
    if not date_str or not DAILY_DB:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{DAILY_DB}/query",
            data=json.dumps({"filter": {"property": "날짜", "date": {"equals": date_str}}, "page_size": 1}).encode(),
            headers=H, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read() or b"{}")
        if d.get("results"):
            return d["results"][0]["id"]
        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=json.dumps({
                "parent": {"database_id": DAILY_DB},
                "properties": {
                    "이름": {"title": [{"text": {"content": f"{date_str} 일일 통합"}}]},
                    "날짜": {"date": {"start": date_str}},
                    "상태": {"status": {"name": "진행 중"}},
                    "external_id": {"rich_text": [{"text": {"content": f"daily:{date_str}"}}]},
                },
            }).encode(),
            headers=H, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["id"]
    except Exception:
        return None

payload = json.loads(os.environ["NOTION_PAYLOAD"])
# 일일 통합 relation 자동 매칭
daily_id = get_or_create_daily(os.environ.get("TS_DATE", "")[:10])
if daily_id:
    payload["properties"]["📊 일일 통합"] = {"relation": [{"id": daily_id}]}

result = upsert_by_external_id(
    os.environ["NOTION_DB"],
    os.environ["NOTION_EXTERNAL_ID"],
    payload["properties"],
)
print(json.dumps(result, ensure_ascii=False))
PY
)

  NOTION_PAGE_ID=$(printf '%s' "$NOTION_RESPONSE" | jq -r '.id // empty' 2>/dev/null)
  NOTION_UPSERT_STATUS=$(printf '%s' "$NOTION_RESPONSE" | jq -r '._upsert // "created"' 2>/dev/null)
  _log "notion page_id=${NOTION_PAGE_ID:-FAIL}"

  # ── 턴 원문 블록 append (helper 스크립트 호출) ─────────────────────
  if [[ -n "$NOTION_PAGE_ID" && -n "$TRANSCRIPT" && "$NOTION_UPSERT_STATUS" != "updated" ]]; then
    HELPER="$(dirname "${BASH_SOURCE[0]}")/append_turn_raw.py"
    if [[ -f "$HELPER" ]]; then
      NOTION_API_TOKEN="$NOTION_TOKEN" python3 "$HELPER" \
        "$NOTION_PAGE_ID" "$TRANSCRIPT" "$SESSION_ID" \
        >> "${HOOK_LOG:-/tmp/slack-session-summary.log}" 2>&1 || true
    fi
  fi
fi

exit 0

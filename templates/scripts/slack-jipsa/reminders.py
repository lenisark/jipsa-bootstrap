"""슬랙 알리미 — 반복(매월/매주/매일) + 1회성 알림, 한국 공휴일/주말 직전 영업일 이동.

- 저장소: reminders.json (이 디렉토리)
- 임시공휴일 수동 추가: holidays_extra.json (["YYYY-MM-DD", ...])
- 자연어 파싱은 순수 파이썬(정규식)으로 결정적 처리. claude 호출 안 함.
- 부서 채널 봇은 읽기전용이라 저장은 반드시 daemon=이 모듈이 직접 수행.

reminder 레코드:
  freq: 'monthly' | 'weekly' | 'daily' | 'once'
  day(monthly 1-31) / weekday(weekly 0=월..6=일) / once_date('YYYY-MM-DD')
  hour, minute, message, mentions[], created_by, created_at, enabled, last_fired
공휴일 이동은 monthly·once 에만 적용(daily·weekly 는 그날 그대로).
"""
from __future__ import annotations

import re
import json
import uuid
import time
import calendar
import threading
from pathlib import Path
from datetime import datetime, timedelta, date, timezone

import holidays as _holidays

KST = timezone(timedelta(hours=9))
BASE = Path.home() / '.claude/scripts/slack-jipsa'
REMINDERS_FILE = BASE / 'reminders.json'
EXTRA_HOLIDAYS_FILE = BASE / 'holidays_extra.json'
FIRED_FILE = BASE / 'fired_log.json'   # 발사 메시지 ts→알림 매핑 (완료 체크용)
CATCHUP_WINDOW_DAYS = 2

_lock = threading.Lock()
_kr_cache: dict = {}


def _default_log(msg: str) -> None:
    print(f'[reminders] {msg}', flush=True)


log = _default_log


def set_logger(fn) -> None:
    global log
    log = fn


# 알리미 2.0: 능동 작업 실행기(daemon 이 call_claude 래퍼를 주입). 미설정이면 액션 비활성.
_executor = None


def set_executor(fn) -> None:
    """fn(channel, prompt) -> str|None 를 등록. 스케줄된 작업을 에이전트가 실행."""
    global _executor
    _executor = fn


def _run_action(web, r: dict) -> None:
    """능동 작업 알림 발사: 에이전트 실행 → 결과를 채널에 보고. (lock 밖 비동기 호출)"""
    ch = r.get('channel')
    label = r.get('message') or '자동 작업'
    try:
        out = _executor(ch, r.get('action') or '')
    except Exception as e:
        log(f'action exec err id={r.get("id")}: {e}')
        out = None
    try:
        if out:
            web.chat_postMessage(channel=ch, mrkdwn=True,
                                 text=f"🤖 *자동 작업: {label}*\n\n{out}")
        else:
            web.chat_postMessage(channel=ch, mrkdwn=True,
                                 text=f"🤖 자동 작업을 끝내지 못했어요: {label}")
        log(f"action fired id={r.get('id')} ch={ch}")
    except Exception as e:
        log(f'action post fail id={r.get("id")}: {e}')


# ── 공휴일 / 영업일 판정 ────────────────────────────────────────────
def _kr_holidays(year: int):
    if year not in _kr_cache:
        _kr_cache[year] = _holidays.SouthKorea(years=year)
    return _kr_cache[year]


def _extra_holidays() -> set:
    try:
        if EXTRA_HOLIDAYS_FILE.exists():
            return set(json.loads(EXTRA_HOLIDAYS_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return set()


def is_non_working(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    if d in _kr_holidays(d.year):
        return True
    if d.isoformat() in _extra_holidays():
        return True
    return False


def _last_day(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _shift_to_business_day(target: date) -> date:
    d = target
    guard = 0
    while is_non_working(d) and guard < 40:
        d -= timedelta(days=1)
        guard += 1
    return d


def effective_notify_date(year: int, month: int, day: int) -> date:
    target = date(year, month, min(int(day), _last_day(year, month)))
    return _shift_to_business_day(target)


def _next_month(y: int, m: int):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _prev_month(y: int, m: int):
    return (y - 1, 12) if m == 1 else (y, m - 1)


# ── 저장소 (CRUD) ──────────────────────────────────────────────────
def load_reminders() -> list:
    try:
        if REMINDERS_FILE.exists():
            data = json.loads(REMINDERS_FILE.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
    except Exception as e:
        log(f'load_reminders fail: {e}')
    return []


def save_reminders(items: list) -> None:
    tmp = REMINDERS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(REMINDERS_FILE)


def add_reminder(channel: str, user: str, spec: dict) -> dict:
    item = {
        'id': 'rmd_' + uuid.uuid4().hex[:8],
        'channel': channel,
        'freq': spec.get('freq', 'monthly'),
        'day': spec.get('day'),
        'weekday': spec.get('weekday'),
        'once_date': spec.get('once_date'),
        'lead_days': list(spec.get('lead_days') or []),
        'hour': int(spec['hour']),
        'minute': int(spec.get('minute', 0)),
        'message': (spec.get('message') or '').strip(),
        'mentions': list(spec.get('mentions') or []),
        'action': (spec.get('action') or '').strip(),   # 알리미 2.0: 능동 작업 지시문
        'created_by': user,
        'created_at': datetime.now(KST).date().isoformat(),
        'enabled': True,
        'last_fired': '',
    }
    with _lock:
        items = load_reminders()
        items.append(item)
        save_reminders(items)
    return item


def list_reminders(channel: str) -> list:
    return [r for r in load_reminders()
            if r.get('channel') == channel and r.get('enabled', True)]


def remove_reminder(channel: str, target: str):
    with _lock:
        items = load_reminders()
        ch_items = [r for r in items
                    if r.get('channel') == channel and r.get('enabled', True)]
        victim = None
        t = (target or '').strip()
        if t.isdigit():
            idx = int(t) - 1
            if 0 <= idx < len(ch_items):
                victim = ch_items[idx]
        if victim is None and t:
            for r in ch_items:
                if t in r.get('message', ''):
                    victim = r
                    break
        if victim is None:
            return None
        items = [r for r in items if r.get('id') != victim['id']]
        save_reminders(items)
        return victim


def edit_reminder(channel: str, target: str, changes: dict):
    """target(번호/키워드) 알림의 hour/minute/day/weekday/message 수정. 수정된 항목 반환."""
    with _lock:
        items = load_reminders()
        ch_items = [r for r in items
                    if r.get('channel') == channel and r.get('enabled', True)]
        victim = None
        t = (target or '').strip()
        if t.isdigit():
            idx = int(t) - 1
            if 0 <= idx < len(ch_items):
                victim = ch_items[idx]
        if victim is None and t:
            for r in ch_items:
                if t in r.get('message', ''):
                    victim = r
                    break
        if victim is None:
            return None
        for k in ('hour', 'minute', 'day', 'weekday', 'message'):
            if changes.get(k) is not None:
                victim[k] = changes[k]
        save_reminders(items)
        return victim


# ── 자연어 파싱 (정규식, 결정적) ────────────────────────────────────
_MENTION = re.compile(r'<[@!][^>]+>')
_WEEKDAYS = {'월': 0, '화': 1, '수': 2, '목': 3, '금': 4, '토': 5, '일': 6}
_WEEKLY = re.compile(r'매주\s*([월화수목금토일])\s*요일?')
_DAILY = re.compile(r'(매일같이|매일|날마다)')
_DAY = re.compile(r'(?:매월|매달)\s*(\d{1,2})\s*일')
_LAST_DAY = re.compile(r'(말일|마지막\s*날)')
_ONCE_MD = re.compile(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일')      # 'N월 N일' (1회성). '매월'엔 숫자 없어 안 걸림
_ONCE_REL = re.compile(r'(오늘|내일|모레|글피)')
_LEAD = re.compile(r'(\d+)\s*일\s*전')                # '3일 전' → 사전 알림
_TIME_COLON = re.compile(r'(\d{1,2})\s*:\s*(\d{2})')
_TIME_HM = re.compile(r'(오전|오후|아침|저녁|밤|새벽)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?')

_TRIG = re.compile(
    r'(리마인더|리마인드)'
    r'|((매월|매달|매주|매일|매월말|말일).*(알려|알림|공지|리마인|등록))'
    r'|(\d{1,2}\s*월\s*\d{1,2}\s*일.*(알려|알림|공지|리마인))'
    r'|((오늘|내일|모레|글피).*(알려|알림|공지|리마인))'
    r'|(알림.*(추가|등록|목록|리스트|삭제|취소|지워|뭐|바꿔|변경|수정))'
)


# ── 알리미 2.0: 능동 작업(에이전트가 실행+보고) 감지 ────────────────
_ACTION_VERB = re.compile(r'(요약|정리|분석|작성|조사|점검|집계|리포트|보고서|브리핑|확인)')
_ACTION_POST = re.compile(r'(올려|올려줘|게시|공유|보고|브리핑|전해|작성해|알려\s*줘)')
_ACTION_MARK = re.compile(r'자동')
_SCHED_ANY = re.compile(r'(매월|매달|매주|매일|매월말|말일|날마다'
                        r'|\d{1,2}\s*월\s*\d{1,2}\s*일|오늘|내일|모레|글피)')


def _is_action_text(text: str) -> bool:
    """능동 작업 알림인가? 스케줄 + (자동 마커 OR 작업동사+보고동사)."""
    if not _SCHED_ANY.search(text or ''):
        return False
    if _ACTION_MARK.search(text):
        return True
    return bool(_ACTION_VERB.search(text) and _ACTION_POST.search(text))


def _extract_action(text: str) -> str:
    """능동 작업 알림의 '지시문' 추출 — 스케줄/시간/멘션만 제거, 작업 지시는 보존."""
    s = text
    s = _MENTION.sub(' ', s)
    s = _WEEKLY.sub(' ', s)
    s = _DAILY.sub(' ', s)
    s = _DAY.sub(' ', s)
    s = _LAST_DAY.sub(' ', s)
    s = _ONCE_MD.sub(' ', s)
    s = _LEAD.sub(' ', s)
    s = _TIME_COLON.sub(' ', s)
    s = _TIME_HM.sub(' ', s)
    s = re.sub(r'(매월|매달|매주|매일|날마다|정오|자정|오늘|내일|모레|글피|하루\s*전|미리|자동)', ' ', s)
    s = re.sub(r'([월화수목금토일])\s*요일', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s.strip(' .,!~·-')


def looks_like_reminder(text: str) -> bool:
    return bool(_TRIG.search(text or '')) or _is_action_text(text or '')


def _detect_action(text: str) -> str:
    if re.search(r'(삭제|취소|지워|없애)', text):
        return 'remove'
    if re.search(r'(바꿔|바꾸|변경|수정|바뀌)', text):
        return 'edit'
    if re.search(r'(목록|리스트|뭐\s*있|뭐가\s*있|등록된|어떤\s*알림)', text):
        return 'list'
    if (_WEEKLY.search(text) or _DAILY.search(text) or _DAY.search(text)
            or _LAST_DAY.search(text) or _ONCE_MD.search(text) or _ONCE_REL.search(text)):
        return 'add'
    return 'unknown'


def _parse_time(text: str):
    mc = _TIME_COLON.search(text)
    if mc:
        return int(mc.group(1)), int(mc.group(2))
    if re.search(r'정오', text):
        return 12, 0
    if re.search(r'자정', text):
        return 0, 0
    mt = _TIME_HM.search(text)
    if mt:
        mer, hh, mm = mt.group(1), int(mt.group(2)), int(mt.group(3) or 0)
        if mer in ('오후', '저녁', '밤') and hh < 12:
            hh += 12
        if mer in ('오전', '새벽', '아침') and hh == 12:
            hh = 0
        return hh, mm
    return None, 0


def _parse_once_date(text: str, today: date):
    """1회성 날짜 추출. 'N월 N일' 또는 오늘/내일/모레/글피. 과거면 내년으로."""
    m = _ONCE_MD.search(text)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            return None
        yr = today.year
        try:
            d = date(yr, mo, min(dy, _last_day(yr, mo)))
        except Exception:
            return None
        if d < today:
            d = date(yr + 1, mo, min(dy, _last_day(yr + 1, mo)))
        return d
    rel = _ONCE_REL.search(text)
    if rel:
        return today + timedelta(days={'오늘': 0, '내일': 1, '모레': 2, '글피': 3}[rel.group(1)])
    return None


def _parse_leads(text: str) -> list:
    """'3일 전', '하루 전' → 사전 알림 일수 리스트(큰 수부터). monthly·once 에서만 사용."""
    leads = set(int(m) for m in _LEAD.findall(text) if 0 < int(m) <= 60)
    if re.search(r'하루\s*전', text):
        leads.add(1)
    return sorted(leads, reverse=True)


def _extract_message(text: str) -> str:
    msg = text
    msg = _MENTION.sub(' ', msg)
    msg = _WEEKLY.sub(' ', msg)
    msg = _DAILY.sub(' ', msg)
    msg = _DAY.sub(' ', msg)
    msg = _LAST_DAY.sub(' ', msg)
    msg = _ONCE_MD.sub(' ', msg)
    msg = _LEAD.sub(' ', msg)
    msg = _TIME_COLON.sub(' ', msg)
    msg = _TIME_HM.sub(' ', msg)
    msg = re.sub(r'(매월|매달|매주|매일|날마다|정오|자정|오늘|내일|모레|글피|하루\s*전|미리)', ' ', msg)
    msg = re.sub(r'([월화수목금토일])\s*요일', ' ', msg)
    msg = re.sub(r'[,，]', ' ', msg)
    msg = re.sub(r'\s+', ' ', msg).strip()
    # 단독 조사 토큰만 제거 (단어 속 글자는 안 건드림: '함께'의 '께' 등 보존)
    _drop = {'에', '에도', '에서', '에다', '에다가', '도', '좀',
             '에게', '에게로', '한테', '한테로', '께', '및'}
    msg = ' '.join(t for t in msg.split() if t not in _drop)
    msg = re.sub(
        r'\s*(하라고|하라는|하라|하도록|해달라고|해라)?\s*'
        r'(알려\s*줘|알려|알림\s*줘|공지\s*해\s*줘|공지|리마인드\s*해\s*줘|'
        r'리마인드|보내\s*줘|보내|등록\s*해\s*줘|등록)\s*$',
        '', msg)
    return msg.strip(' .,!~·-')


def parse_intent(text: str) -> dict:
    action = _detect_action(text)
    today = datetime.now(KST).date()
    if action == 'add':
        hour, minute = _parse_time(text)
        if hour is None:
            hour, minute = 9, 0
        base = {'hour': hour, 'minute': minute,
                'message': _extract_message(text) or None,
                'mentions': _MENTION.findall(text)}
        # 알리미 2.0: 능동 작업이면 지시문을 action 에 담는다(에이전트가 실행+보고).
        if _is_action_text(text):
            base['action'] = _extract_action(text)
            if not base['message']:
                base['message'] = (base['action'] or '')[:40]
        # 주기 판별 (우선순위: 매주 > 매일 > 매월 > 1회성)
        mw = _WEEKLY.search(text)
        if mw:
            base.update(freq='weekly', weekday=_WEEKDAYS[mw.group(1)])
            return base
        if _DAILY.search(text):
            base.update(freq='daily')
            return base
        leads = _parse_leads(text)
        md = _DAY.search(text)
        if md:
            base.update(freq='monthly', day=int(md.group(1)), lead_days=leads)
            return base
        if _LAST_DAY.search(text):
            base.update(freq='monthly', day=31, lead_days=leads)
            return base
        once = _parse_once_date(text, today)
        if once:
            base.update(freq='once', once_date=once.isoformat(), lead_days=leads)
            return base
        base.update(freq='unknown')
        return base
    if action == 'edit':
        mnum = re.search(r'(\d+)\s*번', text)
        changes = {}
        h, m = _parse_time(text)
        if h is not None:
            changes['hour'] = h
            changes['minute'] = m
        md = _DAY.search(text) or re.search(r'(\d{1,2})\s*일', text)
        if md:
            changes['day'] = int(md.group(1))
        mw = _WEEKLY.search(text)
        if mw:
            changes['weekday'] = _WEEKDAYS[mw.group(1)]
        return {'action': 'edit', 'target': mnum.group(1) if mnum else None,
                'changes': changes}
    if action == 'remove':
        mnum = re.search(r'(\d+)\s*번', text)
        if mnum:
            return {'action': 'remove', 'target': mnum.group(1)}
        kw = re.sub(r'(알림|리마인더|리마인드|삭제|취소|지워|없애|해\s*줘|해|좀)', ' ', text).strip()
        return {'action': 'remove', 'target': kw or None}
    return {'action': action}


# ── 표시 헬퍼 ──────────────────────────────────────────────────────
def _fmt_time(h, mn) -> str:
    return f'{int(h):02d}:{int(mn):02d}'


def _describe(r: dict) -> str:
    freq = r.get('freq', 'monthly')
    t = _fmt_time(r['hour'], r.get('minute', 0))
    if freq == 'daily':
        return f'매일 {t}'
    if freq == 'weekly':
        wd = '월화수목금토일'[int(r.get('weekday', 0))]
        return f'매주 {wd}요일 {t}'
    if freq == 'once':
        return f"{r.get('once_date', '?')} {t} (1회)"
    return f"매월 {r['day']}일 {t}"


def _mention_display(web, token: str) -> str:
    m = re.match(r'<@([A-Za-z0-9]+)(?:\|([^>]+))?>', token)
    if m:
        if m.group(2):
            return '@' + m.group(2)
        try:
            u = web.users_info(user=m.group(1))['user']
            pr = u.get('profile', {})
            return '@' + (pr.get('display_name') or pr.get('real_name') or u.get('name') or m.group(1))
        except Exception:
            return '@사용자'
    ms = re.match(r'<!subteam\^[A-Za-z0-9]+(?:\|([^>]+))?>', token)
    if ms:
        return '@' + (ms.group(1) or '그룹')
    msp = re.match(r'<!([A-Za-z0-9]+)>', token)
    if msp:
        return '@' + msp.group(1)
    return token


# ── 슬랙 핸들러 ────────────────────────────────────────────────────
def handle(web, channel: str, user: str, text: str, ts: str) -> None:
    def react(add=None, remove=None):
        try:
            if remove:
                web.reactions_remove(channel=channel, timestamp=ts, name=remove)
            if add:
                web.reactions_add(channel=channel, timestamp=ts, name=add)
        except Exception:
            pass

    react(add='hourglass_flowing_sand')
    intent = parse_intent(text)
    # parse_intent: add 결과는 'freq' 키를 가짐, 그 외는 'action' 키.
    action = 'add' if 'freq' in intent else intent.get('action', 'unknown')
    log(f'reminder intent: {action} {intent}')

    try:
        if action == 'add':
            _handle_add(web, channel, user, intent)
        elif action == 'list':
            _handle_list(web, channel)
        elif action == 'remove':
            _handle_remove(web, channel, intent.get('target') or '')
        elif action == 'edit':
            _handle_edit(web, channel, intent.get('target') or '', intent.get('changes') or {})
        else:
            web.chat_postMessage(channel=channel, mrkdwn=True, text=(
                "알림 명령을 이해 못했어요. 예)\n"
                "• `매월 25일 10시에 정산 알려줘`\n"
                "• `매주 월요일 9시에 주간회의 알려줘`\n"
                "• `매일 18시에 일일보고 알려줘`\n"
                "• `6월 20일 14시에 워크숍 알려줘` (1회)\n"
                "• `알림 목록` · `2번 알림 삭제`"))
        react(add='white_check_mark', remove='hourglass_flowing_sand')
    except Exception as e:
        log(f'reminder handle err: {e}')
        react(add='warning', remove='hourglass_flowing_sand')


def _handle_add(web, channel, user, intent) -> None:
    freq = intent.get('freq', 'monthly')
    hour = intent.get('hour')
    minute = intent.get('minute') or 0
    msg = (intent.get('message') or '').strip()
    mentions = intent.get('mentions') or []
    today = datetime.now(KST).date()

    # 주기별 유효성 검사
    err = None
    if hour is None or not (0 <= int(hour) <= 23) or not msg:
        err = True
    elif freq == 'monthly':
        d = intent.get('day')
        if d is None or not (1 <= int(d) <= 31):
            err = True
    elif freq == 'weekly':
        if intent.get('weekday') is None:
            err = True
    elif freq == 'once':
        try:
            od = date.fromisoformat(intent.get('once_date'))
            if od < today:
                web.chat_postMessage(channel=channel, mrkdwn=True,
                                     text="그 날짜는 이미 지났어요. 미래 날짜로 다시 알려주세요.")
                return
        except Exception:
            err = True
    else:
        err = True

    if err:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            "알림 등록 정보를 이해 못했어요. 예)\n"
            "• `매월 25일 10시에 정산 알려줘`\n"
            "• `매주 월요일 9시에 회의 알려줘`\n"
            "• `매일 18시에 일일보고 알려줘`\n"
            "• `6월 20일 14시에 워크숍 알려줘`"))
        return

    item = add_reminder(channel, user, intent)

    # 휴일 이동 안내 (monthly·once)
    note = ''
    if freq in ('monthly', 'once'):
        if freq == 'monthly':
            eff = effective_notify_date(today.year, today.month, int(item['day']))
            target = date(today.year, today.month, min(int(item['day']), _last_day(today.year, today.month)))
        else:
            target = date.fromisoformat(item['once_date'])
            eff = _shift_to_business_day(target)
        if eff != target:
            note = (f"\n📅 해당일이 휴일이라 *직전 영업일*({eff.month}/{eff.day})에 알려드려요."
                    if freq == 'once'
                    else f"\n📅 해당일이 휴일이면 *직전 영업일*에 알려드려요 (이번 달 예시: {eff.month}/{eff.day}).")

    call_line = ''
    if mentions:
        names = ', '.join(_mention_display(web, t) for t in mentions)
        call_line = f"\n• 호출: {names} (알림 시 채널에서 함께 호출)"

    lead_line = ''
    if item.get('lead_days'):
        lead_line = "\n• 사전 알림: " + ', '.join(f'{n}일 전' for n in item['lead_days'])
    kind_line = "\n• 🤖 *능동 작업* — 그 시각에 집사가 직접 처리하고 결과를 보고해요." if item.get('action') else ''
    web.chat_postMessage(channel=channel, mrkdwn=True, text=(
        f"✅ 알림 등록 완료\n"
        f"• {_describe(item)}{lead_line}\n"
        f"• 내용: {msg}{call_line}{kind_line}{note}\n"
        f"_목록: `알림 목록`  ·  삭제: `N번 알림 삭제`_"))


def _handle_list(web, channel) -> None:
    items = list_reminders(channel)
    if not items:
        web.chat_postMessage(channel=channel, text="등록된 알림이 없어요.")
        return
    lines = ["🔔 *등록된 알림*"]
    for n, r in enumerate(items, 1):
        call = ''
        if r.get('mentions'):
            names = ', '.join(_mention_display(web, t) for t in r['mentions'])
            call = f" (호출: {names})"
        lead = ''
        if r.get('lead_days'):
            lead = ' (+' + ', '.join(f'{x}일전' for x in r['lead_days']) + ')'
        tag = '🤖 ' if r.get('action') else ''
        lines.append(f"{n}. {tag}{_describe(r)}{lead} — {r['message']}{call}")
    web.chat_postMessage(channel=channel, text='\n'.join(lines), mrkdwn=True)


def _handle_remove(web, channel, target) -> None:
    victim = remove_reminder(channel, target)
    if victim:
        web.chat_postMessage(channel=channel, text=(
            f"🗑️ 삭제했어요: {_describe(victim)} — {victim['message']}"))
    else:
        web.chat_postMessage(channel=channel, text=(
            "삭제할 알림을 못 찾았어요. `알림 목록` 으로 번호를 확인해주세요."))


def _handle_edit(web, channel, target, changes) -> None:
    if not changes:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            "무엇을 바꿀지 모르겠어요. 예) `2번 알림 16시로 바꿔줘` · `1번 알림 20일로 바꿔줘`"))
        return
    victim = edit_reminder(channel, target, changes)
    if victim:
        web.chat_postMessage(channel=channel, mrkdwn=True, text=(
            f"✏️ 수정했어요: {_describe(victim)} — {victim['message']}"))
    else:
        web.chat_postMessage(channel=channel, text=(
            "수정할 알림을 못 찾았어요. `알림 목록` 으로 번호를 확인해주세요."))


# ── 스케줄러 ──────────────────────────────────────────────────────
def _post_fire(web, r: dict, target: date, eff: date,
               late_from: date = None, lead_days_before: int = 0) -> None:
    # 알리미 2.0: action 이 있으면 에이전트가 실행+보고(본 알림만, 사전알림 제외).
    if r.get('action') and _executor is not None and not lead_days_before:
        threading.Thread(target=_run_action, args=(web, r), daemon=True).start()
        return
    mentions = r.get('mentions') or []
    if lead_days_before:
        head = f"⏳ *사전 알림 (D-{lead_days_before})* — {_describe(r)} 예정\n"
        note = f"\n_({eff.month}/{eff.day} 예정이에요. 미리 준비하세요!)_"
    else:
        head = f"🔔 *알림* — {_describe(r)}\n"
        if late_from is not None:
            note = (f"\n_(원래 {late_from.month}/{late_from.day}에 보냈어야 할 알림인데,"
                    f" 그날 PC가 꺼져 있어 지금 전해드려요)_")
        elif eff != target:
            note = "\n_(원래 예정일이 휴일이라 직전 영업일인 오늘 알려드려요)_"
        else:
            note = ""
    if mentions:
        head += ' '.join(mentions) + "\n"
    text = f"{head}\n{r['message']}{note}"
    try:
        res = web.chat_postMessage(channel=r['channel'], text=text, mrkdwn=True)
        log(f"reminder fired id={r['id']} ch={r['channel']} freq={r.get('freq')} lead={lead_days_before}")
        if not lead_days_before:          # 본 알림만 완료체크 대상으로 기록
            _record_fired(res.get('ts'), r)
    except Exception as e:
        log(f"reminder post fail id={r.get('id')}: {e}")


# ── 완료 체크 (✅ 반응 기록) ───────────────────────────────────────
def _load_fired() -> dict:
    try:
        if FIRED_FILE.exists():
            return json.loads(FIRED_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _record_fired(ts: str, r: dict) -> None:
    if not ts:
        return
    d = _load_fired()
    d[ts] = {'id': r.get('id'), 'message': r.get('message'),
             'channel': r.get('channel'), 'completed': False}
    if len(d) > 200:                      # 최근 200건만 유지
        for k in list(d.keys())[:-200]:
            d.pop(k, None)
    try:
        FIRED_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        log(f'_record_fired fail: {e}')


def record_completion(ts: str, user: str, name: str):
    """발사 메시지에 ✅ 누르면 호출. 추적 대상이면 완료 기록 후 info 반환, 아니면 None."""
    d = _load_fired()
    info = d.get(ts)
    if not info:
        return None
    if info.get('completed'):
        return None                       # 이미 완료 처리됨(중복 알림 방지)
    info['completed'] = True
    info['completed_by'] = name
    info['completed_user'] = user
    d[ts] = info
    try:
        FIRED_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        log(f'record_completion fail: {e}')
    return info


def _due_today(r: dict, today: date):
    """오늘 발사 대상이면 (target, eff) 반환, 아니면 None."""
    freq = r.get('freq', 'monthly')
    if freq == 'daily':
        return (today, today)
    if freq == 'weekly':
        if today.weekday() == int(r.get('weekday', 0)):
            return (today, today)
        return None
    if freq == 'once':
        try:
            target = date.fromisoformat(r['once_date'])
        except Exception:
            return None
        eff = _shift_to_business_day(target)
        return (target, eff) if eff == today else None
    # monthly
    for (yy, mm) in [(today.year, today.month), _next_month(today.year, today.month)]:
        target = date(yy, mm, min(int(r['day']), _last_day(yy, mm)))
        eff = _shift_to_business_day(target)
        if eff == today:
            return (target, eff)
    return None


def _occurrence_effs(r: dict, today: date):
    """monthly: 이번달·다음달, once: 그 날짜의 (target, eff) 목록."""
    freq = r.get('freq', 'monthly')
    effs = []
    if freq == 'monthly':
        for (yy, mm) in [(today.year, today.month), _next_month(today.year, today.month)]:
            target = date(yy, mm, min(int(r['day']), _last_day(yy, mm)))
            effs.append((target, _shift_to_business_day(target)))
    elif freq == 'once':
        try:
            target = date.fromisoformat(r['once_date'])
            effs.append((target, _shift_to_business_day(target)))
        except Exception:
            pass
    return effs


def _fire_kind_today(r: dict, today: date):
    """오늘 발사 대상 (kind, target, eff, days_before). kind='main'|'lead'. 없으면 None.
    daily·weekly 는 main만, monthly·once 는 main + 사전알림(lead_days)."""
    if r.get('freq', 'monthly') in ('daily', 'weekly'):
        due = _due_today(r, today)
        return ('main', due[0], due[1], 0) if due else None
    leads = sorted({int(x) for x in (r.get('lead_days') or [])}, reverse=True)
    for (target, eff) in _occurrence_effs(r, today):
        if eff == today:
            return ('main', target, eff, 0)
        for n in leads:
            if today == eff - timedelta(days=n):
                return ('lead', target, eff, n)
    return None


def check_and_fire(web) -> None:
    now = datetime.now(KST)
    today = now.date()
    now_min = now.hour * 60 + now.minute
    with _lock:
        items = load_reminders()
        changed = False
        for r in items:
            if not r.get('enabled', True):
                continue
            try:
                fk = _fire_kind_today(r, today)
                if not fk:
                    continue
                kind, target, eff, ndays = fk
                sched_min = int(r['hour']) * 60 + int(r.get('minute', 0))
                if now_min >= sched_min and r.get('last_fired') != today.isoformat():
                    _post_fire(web, r, target, eff,
                               lead_days_before=(ndays if kind == 'lead' else 0))
                    r['last_fired'] = today.isoformat()
                    if r.get('freq') == 'once' and kind == 'main':
                        r['enabled'] = False   # 1회성은 본 알림 발사 후 비활성
                    changed = True
            except Exception as e:
                log(f"fire check err for {r.get('id')}: {e}")
        if changed:
            save_reminders(items)


def catch_up(web) -> None:
    """PC가 꺼져 '최근'(기본 2일) 놓친 monthly·once 알림 1건 보정.
    daily·weekly 는 자주 오므로 캐치업 안 함."""
    now = datetime.now(KST)
    today = now.date()
    cutoff = today - timedelta(days=CATCHUP_WINDOW_DAYS)
    with _lock:
        items = load_reminders()
        changed = False
        for r in items:
            if not r.get('enabled', True):
                continue
            freq = r.get('freq', 'monthly')
            if freq not in ('monthly', 'once'):
                continue
            last = r.get('last_fired') or ''
            created = r.get('created_at') or ''
            cands = []
            if freq == 'monthly':
                for (yy, mm) in [_prev_month(today.year, today.month),
                                 (today.year, today.month)]:
                    target = date(yy, mm, min(int(r['day']), _last_day(yy, mm)))
                    eff = _shift_to_business_day(target)
                    cands.append((target, eff))
            else:  # once
                try:
                    target = date.fromisoformat(r['once_date'])
                    cands.append((target, _shift_to_business_day(target)))
                except Exception:
                    pass
            missed = [(t, e) for (t, e) in cands
                      if cutoff <= e < today and last < e.isoformat()
                      and (not created or e.isoformat() >= created)]
            if missed:
                target, eff = max(missed, key=lambda te: te[1])
                _post_fire(web, r, target, eff, late_from=eff)
                r['last_fired'] = eff.isoformat()
                if freq == 'once':
                    r['enabled'] = False
                changed = True
                log(f"catch-up fired id={r['id']} missed_eff={eff}")
        if changed:
            save_reminders(items)


def reminder_loop(web) -> None:
    log('reminder scheduler 시작 (30초 간격)')
    try:
        catch_up(web)
    except Exception as e:
        log(f'catch_up err: {e}')
    while True:
        try:
            check_and_fire(web)
        except Exception as e:
            log(f'reminder loop err: {e}')
        time.sleep(30)

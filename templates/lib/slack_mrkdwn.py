"""
GitHub markdown → Slack mrkdwn 변환.

Slack mrkdwn 차이:
- **bold** → *bold*
- _italic_ → _italic_ (동일)
- ~~strike~~ → ~strike~
- [text](url) → <url|text>
- # / ## / ### 헤더 → *헤더* (한 줄)
- 리스트/코드블록/이모지 보존

코드블록 안의 텍스트는 변환하지 않음.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r'```[\s\S]*?```')
_INLINE_CODE = re.compile(r'`[^`\n]+`')
_LINK = re.compile(r'\[([^\]\n]+)\]\(([^)\s]+)\)')
_BOLD = re.compile(r'\*\*([^*\n]+?)\*\*')
_STRIKE = re.compile(r'~~([^~\n]+?)~~')
_HEADER = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.M)


def to_mrkdwn(text: str) -> str:
    """GitHub markdown → Slack mrkdwn. 코드블록은 그대로 보존."""
    if not text: return text
    placeholders: list[str] = []

    def stash(m):
        placeholders.append(m.group(0))
        return f'\x00{len(placeholders)-1}\x00'

    # 코드블록 / 인라인코드 임시 치환 (변환 회피)
    out = _FENCE.sub(stash, text)
    out = _INLINE_CODE.sub(stash, out)

    # 변환
    out = _HEADER.sub(lambda m: f'*{m.group(2)}*', out)
    out = _BOLD.sub(r'*\1*', out)
    out = _STRIKE.sub(r'~\1~', out)
    out = _LINK.sub(r'<\2|\1>', out)

    # placeholder 복원
    for i, ph in enumerate(placeholders):
        out = out.replace(f'\x00{i}\x00', ph)
    return out


def to_mrkdwn_lines(lines: list[str]) -> list[str]:
    return [to_mrkdwn(l) for l in lines]


if __name__ == '__main__':
    cases = [
        ('**hello**', '*hello*'),
        ('[click](https://example.com)', '<https://example.com|click>'),
        ('# Title', '*Title*'),
        ('## Sub', '*Sub*'),
        ('~~old~~', '~old~'),
        ('use `code` here', 'use `code` here'),
        ('```\n**bold** in code\n```', '```\n**bold** in code\n```'),
        ('**bold** and [link](https://x.com) and `code`',
         '*bold* and <https://x.com|link> and `code`'),
    ]
    for src, expected in cases:
        got = to_mrkdwn(src)
        ok = got == expected
        sym = '✓' if ok else '✗'
        print(f'{sym} {src!r} → {got!r}')
        assert ok, f'expected {expected!r}, got {got!r}'
    print(f'\n{len(cases)}/{len(cases)} pass')

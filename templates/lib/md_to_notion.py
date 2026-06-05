"""
마크다운 텍스트 → 노션 블록 리스트 변환 유틸.

지원:
- 헤딩: #, ##, ### → heading_1/2/3
- 리스트: -, *, + → bulleted_list_item
- 번호 리스트: 1. 2. → numbered_list_item
- 인용: > → quote
- 코드 블록: ```lang ... ``` → code
- 수평선: ---, ***, ___ → divider
- 일반 단락 → paragraph
- 인라인: **bold**, *italic*, `code`, [text](url)

제약:
- rich_text 항목 1개 2000자 제한 → 초과 시 자동 분할
- 이스케이프 (\*, \_ 등)는 무시
"""

import re


TEXT_CHUNK = 1900  # 노션 rich_text 2000자 제한 여유
NOTION_LANG_MAP = {
    "py": "python", "python": "python",
    "js": "javascript", "javascript": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "sh": "shell", "bash": "shell", "zsh": "shell", "shell": "shell",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "md": "markdown", "markdown": "markdown",
    "html": "html", "css": "css",
    "sql": "sql", "go": "go", "rust": "rust", "rs": "rust",
    "java": "java", "c": "c", "cpp": "c++", "c++": "c++",
    "ruby": "ruby", "rb": "ruby",
    "": "plain text",
}


def _map_lang(lang):
    return NOTION_LANG_MAP.get((lang or "").strip().lower(), "plain text")


_URL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "ftp://")


def _is_valid_url(u):
    if not u:
        return False
    u = u.strip()
    return u.startswith(_URL_PREFIXES)


def _rich_text(content, annotations=None, link=None):
    item = {"type": "text", "text": {"content": content[:TEXT_CHUNK]}}
    if link and _is_valid_url(link):
        item["text"]["link"] = {"url": link}
    if annotations:
        item["annotations"] = annotations
    return item


# 인라인 마크다운 토큰 패턴 (순서 중요: 긴 것 먼저)
_INLINE_PATTERNS = [
    # 코드 인라인
    (re.compile(r"`([^`\n]+?)`"), "code"),
    # 굵게
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), "bold"),
    (re.compile(r"__(.+?)__", re.DOTALL), "bold"),
    # 기울임 (_text_ — 단어 경계 고려)
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"), "italic"),
    (re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])"), "italic"),
    # 링크
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), "link"),
]


def parse_inline(text):
    """인라인 마크다운 → rich_text 배열."""
    if not text:
        return [_rich_text("")]

    # 토큰 위치 수집
    spans = []  # (start, end, kind, content, extra)
    for pat, kind in _INLINE_PATTERNS:
        for m in pat.finditer(text):
            if kind == "link":
                spans.append((m.start(), m.end(), kind, m.group(1), m.group(2)))
            else:
                spans.append((m.start(), m.end(), kind, m.group(1), None))

    # 겹치는 span 제거 (먼저 매치된 것 우선)
    spans.sort(key=lambda x: (x[0], -x[1]))
    non_overlap = []
    last_end = 0
    for s in spans:
        if s[0] >= last_end:
            non_overlap.append(s)
            last_end = s[1]

    if not non_overlap:
        # plain text만. 2000자 초과 분할
        parts = []
        for i in range(0, len(text), TEXT_CHUNK):
            parts.append(_rich_text(text[i:i + TEXT_CHUNK]))
        return parts

    # 토큰 사이 plain + 토큰 → rich_text
    result = []
    cursor = 0
    for start, end, kind, content, extra in non_overlap:
        if start > cursor:
            plain = text[cursor:start]
            for i in range(0, len(plain), TEXT_CHUNK):
                result.append(_rich_text(plain[i:i + TEXT_CHUNK]))
        if kind == "code":
            result.append(_rich_text(content, annotations={"code": True}))
        elif kind == "bold":
            result.append(_rich_text(content, annotations={"bold": True}))
        elif kind == "italic":
            result.append(_rich_text(content, annotations={"italic": True}))
        elif kind == "link":
            if _is_valid_url(extra):
                result.append(_rich_text(content, link=extra))
            else:
                # url 형식 아니면 마크다운 표기 그대로 plain text 유지
                result.append(_rich_text(f"[{content}]({extra})"))
        cursor = end
    if cursor < len(text):
        tail = text[cursor:]
        for i in range(0, len(tail), TEXT_CHUNK):
            result.append(_rich_text(tail[i:i + TEXT_CHUNK]))

    return result or [_rich_text("")]


def _block(block_type, rich_text_or_children, extra=None):
    inner = {"rich_text": rich_text_or_children} if isinstance(rich_text_or_children, list) else rich_text_or_children
    if extra:
        inner.update(extra)
    return {"object": "block", "type": block_type, block_type: inner}


def _paragraph_blocks(text):
    """2000자 초과 시 여러 paragraph 블록으로 분할."""
    if not text:
        return []
    # 첫 블록에 rich_text 넣고, 남은 건 새 paragraph로
    # 인라인 파싱은 각 chunk별로
    blocks = []
    # 단락은 최대 TEXT_CHUNK 단위로 잘라 한 paragraph씩 (rich_text도 각 chunk 내부에서 재분할)
    for i in range(0, len(text), TEXT_CHUNK):
        chunk = text[i:i + TEXT_CHUNK]
        blocks.append(_block("paragraph", parse_inline(chunk)))
    return blocks


def _heading_block(level, text):
    level = max(1, min(3, level))
    return _block(f"heading_{level}", parse_inline(text))


def _is_special_line(line):
    """빈 줄 또는 단락 경계로 볼 line?"""
    s = line.strip()
    if not s:
        return True
    if re.match(r"^#{1,6}\s", s):
        return True
    if re.match(r"^[-*+]\s", s):
        return True
    if re.match(r"^\d+\.\s", s):
        return True
    if s.startswith(">"):
        return True
    if s in ("---", "***", "___"):
        return True
    if s.startswith("```"):
        return True
    return False


def markdown_to_blocks(text, max_blocks=None):
    """메인 변환 함수.
    text → 노션 블록 리스트.
    max_blocks 지정 시 초과 분 truncate.
    """
    if not text:
        return []

    lines = text.split("\n")
    blocks = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 빈 줄 skip
        if not stripped:
            i += 1
            continue

        # 코드 블록
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip().startswith("```"):
                    break
                code_lines.append(lines[i])
                i += 1
            i += 1  # closing ``` skip
            code_text = "\n".join(code_lines)
            # 2000자 초과 시 잘림 — 노션 code 블록 1개당 한 rich_text
            blocks.append(_block(
                "code",
                [_rich_text(code_text[:TEXT_CHUNK])],
                extra={"language": _map_lang(lang)},
            ))
            continue

        # 헤딩
        m = re.match(r"^(#{1,6})\s+(.+)", stripped)
        if m:
            level = min(3, len(m.group(1)))
            content = m.group(2)
            blocks.append(_heading_block(level, content))
            i += 1
            continue

        # 수평선
        if stripped in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # 인용
        if stripped.startswith(">"):
            # 연속된 quote 라인 모으기
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q = lines[i].strip().lstrip(">").strip()
                quote_lines.append(q)
                i += 1
            quote_text = "\n".join(quote_lines)
            blocks.append(_block("quote", parse_inline(quote_text[:TEXT_CHUNK])))
            continue

        # 번호 리스트
        m = re.match(r"^(\d+)\.\s+(.+)", stripped)
        if m:
            content = m.group(2)
            blocks.append(_block("numbered_list_item", parse_inline(content)))
            i += 1
            continue

        # 불릿 리스트
        m = re.match(r"^[-*+]\s+(.+)", stripped)
        if m:
            content = m.group(1)
            blocks.append(_block("bulleted_list_item", parse_inline(content)))
            i += 1
            continue

        # 일반 단락 (연속된 일반 줄들 모으기)
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _is_special_line(lines[i]):
            para_lines.append(lines[i])
            i += 1
        para_text = "\n".join(para_lines).strip()
        blocks.extend(_paragraph_blocks(para_text))

    if max_blocks and len(blocks) > max_blocks:
        blocks = blocks[:max_blocks]
        blocks.append(_block("paragraph", [_rich_text("... (이하 생략)")]))

    return blocks


if __name__ == "__main__":
    import json
    import sys
    text = sys.stdin.read() if not sys.stdin.isatty() else "# 샘플\n\n**굵게** 일반 `코드`\n\n- 리스트1\n- 리스트2\n\n```python\nprint('hi')\n```\n\n> 인용문"
    print(json.dumps(markdown_to_blocks(text), ensure_ascii=False, indent=2))

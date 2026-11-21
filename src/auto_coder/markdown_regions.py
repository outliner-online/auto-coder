"""Shared Markdown region filtering for authored declarations."""

import re
from typing import Iterator, Optional, Tuple

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
_INDENTED_CODE = re.compile(r"^ {4,}\S")


def authored_markdown_lines(body: str) -> Iterator[Tuple[int, str]]:
    """Yield (1-based line number, raw line) pairs outside excluded regions.

    Fenced code blocks, indented code blocks, blockquote lines, and HTML
    comments are non-authored examples: they are skipped entirely rather than
    being inspected for headings or declarations, and text after they end is
    inspected normally again.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    fence_char: Optional[str] = None
    fence_len = 0
    in_comment = False
    for index, line in enumerate(lines, start=1):
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        comment_start = line.find("<!--")
        if comment_start != -1:
            if "-->" not in line[comment_start:]:
                in_comment = True
            continue
        fence_match = _FENCE_OPEN.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
            continue
        if fence_char is not None:
            continue
        if _BLOCKQUOTE.match(line):
            continue
        if _INDENTED_CODE.match(line):
            continue
        yield index, line

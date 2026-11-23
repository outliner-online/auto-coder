"""CommonMark source regions shared by structural review and lifecycle intake."""

from typing import Callable, Iterator, Sequence, Tuple

from markdown_it import MarkdownIt
from markdown_it.rules_inline.backticks import backtick
from markdown_it.rules_inline.html_inline import html_inline
from markdown_it.rules_inline.state_inline import StateInline
from markdown_it.token import Token


def _record_inline_region(rule: Callable[[StateInline, bool], bool]) -> Callable[[StateInline, bool], bool]:
    """Retain raw offsets while the parser recognizes code and HTML spans."""

    def recorded(state: StateInline, silent: bool) -> bool:
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent and len(state.tokens) > count and state.tokens[-1].type in {"code_inline", "html_inline"}:
            state.tokens[-1].meta["authored_source_span"] = (start, state.pos)
        return matched

    return recorded


def _parse_commonmark(body: str) -> list[Token]:
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.at("backticks", _record_inline_region(backtick))
    parser.inline.ruler.at("html_inline", _record_inline_region(html_inline))
    return parser.parse(body)


def _excluded_lines(tokens: Sequence[Token], *, html_blocks: bool = True) -> set[int]:
    excluded: set[int] = set()
    for token in tokens:
        if (token.type in {"fence", "code_block", "blockquote_open"} or (html_blocks and token.type == "html_block")) and token.map is not None:
            excluded.update(range(*token.map))
    return excluded


def authored_markdown_lines(body: str) -> Iterator[Tuple[int, str]]:
    """Yield original source lines outside code, quotes and HTML comments.

    Block ranges include containers, tab indentation and lazy quote lines.
    Structural review retains its policy of excluding a whole comment line.
    """
    excluded = _excluded_lines(_parse_commonmark(body), html_blocks=False)
    in_comment = False
    for index, line in enumerate(body.replace("\r\n", "\n").split("\n")):
        if index in excluded:
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        comment_start = line.find("<!--")
        if comment_start != -1:
            in_comment = "-->" not in line[comment_start:]
            continue
        yield index + 1, line


def authored_lifecycle_markdown(body: str) -> str:
    """Retain raw paragraph/heading source, masking documentary regions.

    Inline content is the parser's unrendered source, so escapes and entities
    cannot manufacture a closing keyword. NUL barriers preserve line breaks
    without allowing a directive to bridge code, quotes or HTML.
    """
    tokens = _parse_commonmark(body)
    excluded = _excluded_lines(tokens)
    lines = ["" if not line.strip() else "\x00" for line in body.replace("\r\n", "\n").split("\n")]
    for token in tokens:
        if token.type != "inline" or token.map is None or token.map[0] in excluded:
            continue
        source = list(token.content)
        for child in token.children or ():
            span = child.meta.get("authored_source_span")
            if span is not None:
                for index in range(*span):
                    if source[index] != "\n":
                        source[index] = "\x00"
        for offset, line in enumerate("".join(source).split("\n")):
            if token.map[0] + offset < token.map[1]:
                lines[token.map[0] + offset] = line
    for index in excluded:
        lines[index] = "\x00"
    return "\n".join(lines)

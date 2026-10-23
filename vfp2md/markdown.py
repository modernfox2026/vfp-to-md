"""Small Markdown builder with the escaping rules legacy data needs.

Legacy VFP content is full of characters that break Markdown: pipes inside
expressions, backtick runs inside code, CR only line endings, stray NUL and
control bytes, and enormous memo values. Everything that reaches the output
goes through the helpers here so the result always renders cleanly.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Any, Iterable, List, Optional, Sequence

_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BACKTICK_RUN_RE = re.compile("`+")
_PIPE_RE = re.compile(r"(\\*)\|")


def clean_text(text: str) -> str:
    """Normalise line endings and drop control characters (keeps tabs/newlines)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub("", text)


def format_value(value: Any) -> str:
    """Render a decoded DBF value the way a FoxPro developer would expect."""
    if value is None:
        return ".NULL."
    if value is True:
        return ".T."
    if value is False:
        return ".F."
    if isinstance(value, _dt.datetime):
        if value.microsecond:
            return value.isoformat(sep=" ", timespec="milliseconds")
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "<binary, {} bytes>".format(len(value)) if value else ""
    return str(value)


def truncate(text: str, limit: Optional[int]) -> str:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[+{} chars]".format(len(text) - limit)


def cell(value: Any, limit: Optional[int] = None) -> str:
    """Escape any value for use inside a Markdown table cell."""
    text = value if isinstance(value, str) else format_value(value)
    text = truncate(clean_text(text), limit)
    # Only pipes need escaping in GFM tables. Backslashes are left alone so
    # Windows paths read naturally, except directly before a pipe where they
    # would otherwise swallow the escape.
    text = _PIPE_RE.sub(lambda m: m.group(1) * 2 + "\\|", text)
    text = text.replace("\t", "    ").replace("\n", "<br>")
    return text.strip() or " "


def inline_code(text: str) -> str:
    """Wrap text in an inline code span that survives embedded backticks."""
    text = clean_text(text).replace("\n", " ")
    if not text:
        return "` `"
    runs = [len(m) for m in _BACKTICK_RUN_RE.findall(text)]
    ticks = "`" * (max(runs) + 1 if runs else 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return "{t}{p}{x}{p}{t}".format(t=ticks, p=pad, x=text)


def heading_text(text: str) -> str:
    """Single line, control free text safe for a heading."""
    text = clean_text(text).replace("\n", " ").strip()
    return text.replace("#", "\\#") if text.startswith("#") else (text or "(unnamed)")


def fence_for(text: str) -> str:
    runs = [len(m) for m in _BACKTICK_RUN_RE.findall(text)]
    return "`" * max(3, (max(runs) + 1) if runs else 3)


class MarkdownDoc:
    """Accumulates Markdown blocks separated by blank lines."""

    def __init__(self) -> None:
        self._blocks: List[str] = []

    def heading(self, level: int, text: str) -> "MarkdownDoc":
        level = max(1, min(level, 6))
        self._blocks.append("{} {}".format("#" * level, heading_text(text)))
        return self

    def para(self, text: str) -> "MarkdownDoc":
        text = clean_text(text).strip()
        if text:
            self._blocks.append(text)
        return self

    def raw(self, text: str) -> "MarkdownDoc":
        if text:
            self._blocks.append(text)
        return self

    def bullets(self, items: Iterable[str]) -> "MarkdownDoc":
        lines = ["- " + clean_text(i).replace("\n", " ") for i in items if i is not None]
        if lines:
            self._blocks.append("\n".join(lines))
        return self

    def kv(self, pairs: Iterable[Sequence[Any]]) -> "MarkdownDoc":
        """Bulleted ``**Key:** value`` list; pairs with empty values are skipped."""
        lines = []
        for key, value in pairs:
            text = value if isinstance(value, str) else format_value(value)
            text = clean_text(text).replace("\n", " ").strip()
            if text:
                lines.append("- **{}:** {}".format(key, text))
        if lines:
            self._blocks.append("\n".join(lines))
        return self

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]], limit: Optional[int] = None) -> "MarkdownDoc":
        rows = list(rows)
        if not rows:
            return self
        lines = [
            "| " + " | ".join(cell(h) for h in headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
        ]
        width = len(headers)
        for row in rows:
            cells = [cell(v, limit) for v in list(row)[:width]]
            cells += [" "] * (width - len(cells))
            lines.append("| " + " | ".join(cells) + " |")
        self._blocks.append("\n".join(lines))
        return self

    def code(self, text: str, lang: str = "") -> "MarkdownDoc":
        text = clean_text(text).rstrip()
        if not text.strip():
            return self
        fence = fence_for(text)
        self._blocks.append("{f}{l}\n{t}\n{f}".format(f=fence, l=lang, t=text))
        return self

    def note(self, text: str) -> "MarkdownDoc":
        """A block quote, used for warnings and conversion notes."""
        text = clean_text(text).strip()
        if text:
            self._blocks.append("\n".join("> " + line for line in text.split("\n")))
        return self

    def render(self) -> str:
        return "\n\n".join(self._blocks).rstrip() + "\n"

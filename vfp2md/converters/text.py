"""Plain text sources: programs, headers, text exports and config files."""

from __future__ import annotations

from typing import Tuple

from ..markdown import MarkdownDoc
from .base import Context, header_section, split_procedures, warnings_section

LANGUAGES = {
    ".xml": "xml",
    ".json": "json",
    ".htm": "html",
    ".html": "html",
    ".css": "css",
    ".js": "javascript",
    ".ini": "ini",
    ".fpw": "ini",
    ".cfg": "ini",
    ".txt": "",
}

# Larger files are still converted, but users are told the result is big.
LARGE_FILE = 20 * 1024 * 1024


def decode_bytes(data: bytes, fallback: str) -> Tuple[str, str]:
    """Decode text, preferring UTF variants when a BOM or valid UTF-8 is present."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8 (BOM)"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode(fallback, "replace"), fallback


def looks_binary(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return sample.count(b"\x00") > len(sample) // 100 + 1


def convert(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    item = ctx.item
    with open(item.path, "rb") as fh:
        data = fh.read()
    if item.size > LARGE_FILE:
        ctx.warn("source file is {} bytes; output will be large".format(item.size))
    if looks_binary(data):
        header_section(doc, ctx)
        ctx.warn("file contains binary data; it may be compiled or encrypted and was not rendered as code")
        doc.para("This file appears to be binary rather than text, so its content is not included.")
        warnings_section(doc, ctx)
        return doc

    text, used = decode_bytes(data, ctx.options.encoding or "cp1252")
    if "�" in text and used != "utf-8":
        ctx.warn("some bytes could not be decoded with {}; try --encoding".format(used))
    lang = LANGUAGES.get(item.ext, "foxpro")
    extra = [("Encoding", used), ("Lines", text.count("\n") + 1 if text else 0)]
    if lang == "foxpro":
        _, procs = split_procedures(text)
        if procs:
            extra.append(("Procedures / functions", ", ".join(name for name, _ in procs)))
    header_section(doc, ctx, extra)
    doc.heading(2, "Source")
    if text.strip():
        doc.code(text, lang)
    else:
        doc.para("(file is empty)")
    warnings_section(doc, ctx)
    return doc

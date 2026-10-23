"""Runs converters over a selection of files and writes the Markdown output."""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import time
import traceback
from typing import Callable, List, Optional

from . import __version__
from .converters import CONVERTERS, Context, Options
from .converters.base import header_section
from .discovery import DiscoveredFile
from .errors import VfpError
from .fsutil import safe_relpath
from .markdown import MarkdownDoc, cell

STATUS_OK = "ok"
STATUS_WARN = "warnings"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

CONFLICT_OVERWRITE = "overwrite"
CONFLICT_SKIP = "skip"
CONFLICT_RENAME = "rename"

INDEX_NAME = "INDEX.md"


class Outcome:
    def __init__(self, item: DiscoveredFile):
        self.item = item
        self.status = STATUS_OK
        self.output: Optional[str] = None
        self.warnings: List[str] = []
        self.error: Optional[str] = None
        self.seconds = 0.0


def output_relpath(item: DiscoveredFile) -> str:
    """forms/Main.scx -> forms/Main.scx.md (keeps the extension so names never clash)."""
    return safe_relpath(item.relpath) + ".md"


def render(item: DiscoveredFile, options: Options) -> "tuple[str, List[str], Optional[str]]":
    """Convert one file. Never raises: failures produce an error document.

    Returns ``(markdown, warnings, error_message_or_None)``.
    """
    ctx = Context(item, options)
    converter = CONVERTERS.get(item.kind.key)
    try:
        if converter is None:
            raise VfpError("no converter for file kind {!r}".format(item.kind.key))
        if item.missing_required:
            ctx.warn(
                "companion file {} is missing; output will be incomplete".format(
                    os.path.splitext(os.path.basename(item.path))[0] + item.missing_required
                )
            )
        doc = converter(ctx)
        return doc.render(), ctx.warnings, None
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes a readable report
        message = "{}: {}".format(type(exc).__name__, exc) if not isinstance(exc, VfpError) else str(exc)
        doc = MarkdownDoc()
        try:
            header_section(doc, ctx)
        except Exception:  # noqa: BLE001
            doc.heading(1, os.path.basename(item.path))
        doc.heading(2, "Conversion failed")
        doc.para("The file could not be converted. It may be damaged, encrypted, or not a genuine {} file.".format(item.kind.label))
        doc.code(message)
        if not isinstance(exc, VfpError):
            doc.heading(3, "Technical details")
            doc.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:]))
        if ctx.warnings:
            doc.heading(2, "Notes collected before the failure")
            doc.bullets(ctx.warnings)
        return doc.render(), ctx.warnings, message


def write_atomic(path: str, content: str) -> None:
    """Write UTF-8 text via a temp file + rename so a crash never leaves half a file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".vfp2md-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unique_path(path: str) -> str:
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("{}_{}{}".format(base, n, ext)):
        n += 1
    return "{}_{}{}".format(base, n, ext)


ProgressFn = Callable[[int, int, DiscoveredFile, Optional[Outcome]], None]


def convert_all(
    items: List[DiscoveredFile],
    out_dir: str,
    options: Options,
    on_conflict: str = CONFLICT_OVERWRITE,
    progress: Optional[ProgressFn] = None,
    source_root: str = "",
) -> List[Outcome]:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    outcomes: List[Outcome] = []
    total = len(items)
    for n, item in enumerate(items, 1):
        if progress:
            progress(n, total, item, None)
        outcome = Outcome(item)
        started = time.monotonic()
        target = os.path.join(out_dir, output_relpath(item))
        if os.path.exists(target):
            if on_conflict == CONFLICT_SKIP:
                outcome.status = STATUS_SKIPPED
                outcome.output = target
                outcome.warnings.append("output already exists; skipped")
                outcomes.append(outcome)
                if progress:
                    progress(n, total, item, outcome)
                continue
            if on_conflict == CONFLICT_RENAME:
                target = _unique_path(target)
        markdown, warnings, error = render(item, options)
        outcome.warnings = warnings
        if error:
            outcome.status = STATUS_FAILED
            outcome.error = error
        elif warnings:
            outcome.status = STATUS_WARN
        try:
            write_atomic(target, markdown)
            outcome.output = target
        except OSError as exc:
            outcome.status = STATUS_FAILED
            outcome.error = "could not write {}: {}".format(target, exc)
        outcome.seconds = time.monotonic() - started
        outcomes.append(outcome)
        if progress:
            progress(n, total, item, outcome)
    write_index(out_dir, outcomes, options, source_root)
    return outcomes


def write_index(out_dir: str, outcomes: List[Outcome], options: Options, source_root: str = "") -> str:
    """Write INDEX.md: a table of contents for the AI/reader plus a conversion log."""
    doc = MarkdownDoc()
    doc.heading(1, "VFP conversion index")
    doc.para(
        "This folder contains Markdown renderings of Visual FoxPro application files. "
        "Each document describes one source file: its structure, properties, data and code. "
        "Start with the project (.pjx) and database (.dbc) documents for an overview, then forms, "
        "class libraries, programs, reports and menus."
    )
    counts = {s: sum(1 for o in outcomes if o.status == s) for s in (STATUS_OK, STATUS_WARN, STATUS_FAILED, STATUS_SKIPPED)}
    doc.kv([
        ("Source folder", source_root),
        ("Generated", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Tool", "vfp2md {}".format(__version__)),
        ("Files", len(outcomes)),
        ("Converted cleanly", counts[STATUS_OK]),
        ("Converted with notes", counts[STATUS_WARN]),
        ("Failed", counts[STATUS_FAILED]),
        ("Skipped (already existed)", counts[STATUS_SKIPPED]),
        ("Data rows per table", "all" if options.max_rows <= 0 else options.max_rows),
    ])
    rows = []
    for o in outcomes:
        link = ""
        if o.output:
            rel = os.path.relpath(o.output, out_dir).replace(os.sep, "/")
            link = "[{}]({})".format(cell(rel).replace("]", "\\]"), rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29"))
        rows.append([o.item.relpath.replace("\\", "/"), o.item.kind.label, o.status, link])
    doc.heading(2, "Documents")
    if rows:
        # Links are pre-escaped, so build this table by hand rather than via doc.table.
        lines = ["| Source file | Type | Status | Markdown |", "|---|---|---|---|"]
        for src, kind, status, link in rows:
            lines.append("| {} | {} | {} | {} |".format(cell(src), cell(kind), status, link or " "))
        doc.raw("\n".join(lines))
    problems = [o for o in outcomes if o.error or o.warnings]
    if problems:
        doc.heading(2, "Conversion log")
        for o in problems:
            doc.heading(3, o.item.relpath.replace("\\", "/"))
            items = []
            if o.error:
                items.append("ERROR: " + o.error)
            items.extend(o.warnings)
            doc.bullets(items)
    path = os.path.join(out_dir, INDEX_NAME)
    write_atomic(path, doc.render())
    return path

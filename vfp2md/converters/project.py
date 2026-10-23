"""Projects (.pjx)."""

from __future__ import annotations

import re
from typing import Dict, List

from ..markdown import MarkdownDoc
from .base import Context, generic_records, header_section, warnings_section

TYPES = {
    "H": "Project header",
    "P": "Programs",
    "K": "Forms",
    "V": "Class libraries",
    "R": "Reports",
    "B": "Labels",
    "M": "Menus",
    "Q": "Queries",
    "d": "Databases",
    "D": "Free tables",
    "L": "API libraries (FLL)",
    "Z": "Applications (APP)",
    "T": "Text files",
    "x": "Other files",
    "i": "Icons / images",
}
ORDER = ["P", "K", "V", "R", "B", "M", "Q", "d", "D", "L", "Z", "T", "x", "i"]

_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\x80-\xff]{3,}")


def _clean_path(text: str) -> str:
    return text.split("\x00", 1)[0].strip()


def convert(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    with ctx.open_table(".pjt") as table:
        if not (table.has_field("NAME") and table.has_field("TYPE")):
            header_section(doc, ctx)
            ctx.warn("unexpected column layout; rendered record by record")
            generic_records(doc, table, ctx, title_fields=("NAME",))
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        recs = list(table.records(include_deleted=False))
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file (.pjt) is missing: file names are unavailable")
    for rec in recs:
        for err in rec.errors:
            ctx.warn("record {}: {}".format(rec.index, err))

    header = next((r for r in recs if r.text("TYPE").strip() == "H"), None)
    files = [r for r in recs if r.text("TYPE").strip() != "H"]
    main = next((r for r in files if r.get("MAINPROG") is True), None)

    extra = [
        ("Project path (when saved)", _clean_path(header.text("NAME")) if header else ""),
        ("Home directory", _clean_path(header.text("HOMEDIR")) if header else ""),
        ("Build output", _clean_path(header.text("OUTFILE")) if header else ""),
        ("Main file", _clean_path(main.text("NAME")) if main else ""),
        ("Files in project", len(files)),
    ]
    header_section(doc, ctx, extra)

    if header is not None:
        devinfo = header.get("DEVINFO")
        if isinstance(devinfo, (bytes, bytearray)) and devinfo.strip(b"\x00 "):
            strings = [m.decode(table.encoding, "replace").strip() for m in _PRINTABLE_RUN.findall(bytes(devinfo))]
            strings = [s for s in strings if s]
            if strings:
                doc.heading(2, "Developer information")
                doc.bullets(strings)

    grouped: Dict[str, List] = {}
    for rec in files:
        grouped.setdefault(rec.text("TYPE").strip() or "?", []).append(rec)

    doc.heading(2, "Files by type")
    summary = [[TYPES.get(t, "Type {}".format(t)), len(grouped[t])] for t in ORDER + sorted(set(grouped) - set(ORDER)) if t in grouped]
    doc.table(["Type", "Count"], summary)

    for t in ORDER + sorted(set(grouped) - set(ORDER)):
        if t not in grouped:
            continue
        doc.heading(3, TYPES.get(t, "Type {}".format(t)))
        rows = []
        for rec in sorted(grouped[t], key=lambda r: _clean_path(r.text("NAME")).lower()):
            flags = []
            if rec.get("MAINPROG") is True:
                flags.append("main")
            if rec.get("EXCLUDE") is True:
                flags.append("excluded from build")
            rows.append([_clean_path(rec.text("NAME")), ", ".join(flags), rec.text("COMMENTS").strip()])
        doc.table(["File", "Flags", "Comment"], rows)
    warnings_section(doc, ctx)
    return doc

"""Database containers (.dbc).

A DBC is a DBF whose rows are objects (Database, Table, Field, Index, View,
Connection, Relation) linked by OBJECTID / PARENTID. Object settings live in
a binary PROPERTY memo; stored procedures live in the CODE memo of the
``StoredProceduresSource`` row.
"""

from __future__ import annotations

import re
import struct
from typing import Dict, List, Tuple

from ..markdown import MarkdownDoc
from .base import Context, generic_records, header_section, warnings_section

RI_RULES = {"C": "cascade", "R": "restrict", "I": "ignore"}
_PRINTABLE_RUN = re.compile(rb"[\x09\x0a\x0d\x20-\x7e\x80-\xff]{2,}")


def _decode(data: bytes, encoding: str) -> str:
    return data.decode(encoding, "replace")


def parse_property_blob(blob: bytes, encoding: str) -> List[Tuple[int, str]]:
    """Decode a DBC PROPERTY memo into ``[(property_id, value_text), ...]``.

    Each entry is ``<int32 total length><int16 type><uint8 id><value>``.
    When the chain does not add up (damaged or unexpected data) the blob is
    reduced to its printable strings with id -1 so nothing is lost.
    """
    if not blob:
        return []
    entries: List[Tuple[int, str]] = []
    pos = 0
    ok = True
    while pos < len(blob):
        if len(blob) - pos < 7:
            ok = not blob[pos:].strip(b"\x00")
            break
        length = struct.unpack("<i", blob[pos : pos + 4])[0]
        if length < 7 or pos + length > len(blob):
            ok = False
            break
        pid = blob[pos + 6]
        value = blob[pos + 7 : pos + length]
        if value.endswith(b"\x00"):
            text = _decode(value.rstrip(b"\x00"), encoding)
        elif len(value) == 1:
            text = str(value[0])
        elif len(value) == 4:
            text = str(struct.unpack("<i", value)[0])
        else:
            text = _decode(value, encoding)
        entries.append((pid, text.strip()))
        pos += length
    if ok:
        return entries
    return [(-1, _decode(m, encoding).strip()) for m in _PRINTABLE_RUN.findall(blob) if m.strip()]


def property_strings(rec, encoding: str) -> List[str]:
    blob = rec.get("PROPERTY")
    if isinstance(blob, str):
        blob = blob.encode(encoding, "replace")
    if not blob:
        return []
    values = []
    for _, text in parse_property_blob(bytes(blob), encoding):
        text = text.strip()
        # Single digit flags carry little meaning without the id table; keep real text.
        if text and not (len(text) <= 2 and text.isdigit()):
            values.append(text)
    return values


def _looks_like_sql(text: str) -> bool:
    return bool(re.match(r"\s*(select|insert|update|delete|exec|with)\b", text, re.IGNORECASE))


def convert(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    with ctx.open_table(".dct") as table:
        encoding = table.encoding
        if not (table.has_field("OBJECTTYPE") and table.has_field("OBJECTNAME")):
            header_section(doc, ctx)
            ctx.warn("unexpected column layout; rendered record by record")
            generic_records(doc, table, ctx)
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        recs = list(table.records(include_deleted=False))
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file (.dct) is missing: properties and stored procedures are unavailable")
    for rec in recs:
        for err in rec.errors:
            ctx.warn("record {}: {}".format(rec.index, err))

    by_id: Dict[int, object] = {}
    children: Dict[int, List] = {}
    by_type: Dict[str, List] = {}
    for rec in recs:
        oid = rec.get("OBJECTID")
        if isinstance(oid, int):
            by_id[oid] = rec
        pid = rec.get("PARENTID")
        children.setdefault(pid if isinstance(pid, int) else -1, []).append(rec)
        by_type.setdefault(rec.text("OBJECTTYPE").strip().lower(), []).append(rec)

    def kids(rec, otype: str) -> List:
        return [c for c in children.get(rec.get("OBJECTID"), []) if c.text("OBJECTTYPE").strip().lower() == otype]

    tables = by_type.get("table", [])
    views = by_type.get("view", [])
    connections = by_type.get("connection", [])
    relations = by_type.get("relation", [])
    header_section(doc, ctx, [
        ("Tables", len(tables)),
        ("Views", len(views)),
        ("Connections", len(connections)),
        ("Persistent relations", len(relations)),
    ])
    doc.note(
        "DBC properties are stored in a binary format; values below are the decoded text "
        "properties of each object (paths, captions, comments, default values, validation rules, SQL)."
    )

    if tables:
        doc.heading(2, "Tables")
        for t in tables:
            name = t.text("OBJECTNAME").strip()
            doc.heading(3, "Table {}".format(name))
            doc.bullets("Property: {}".format(v) for v in property_strings(t, encoding))
            fields = kids(t, "field")
            if fields:
                doc.para("Fields:")
                doc.table(["Field", "Properties (caption, comment, default, rule, ...)"],
                          [[f.text("OBJECTNAME").strip(), "; ".join(property_strings(f, encoding))] for f in fields])
            indexes = kids(t, "index")
            if indexes:
                doc.para("Indexes:")
                doc.table(["Index tag", "Properties"],
                          [[i.text("OBJECTNAME").strip(), "; ".join(property_strings(i, encoding))] for i in indexes])

    if views:
        doc.heading(2, "Views")
        for v in views:
            doc.heading(3, "View {}".format(v.text("OBJECTNAME").strip()))
            props = property_strings(v, encoding)
            sql = [p for p in props if _looks_like_sql(p)]
            for statement in sql:
                doc.code(statement, "sql")
            others = [p for p in props if p not in sql]
            doc.bullets("Property: {}".format(p) for p in others)
            fields = kids(v, "field")
            if fields:
                doc.table(["View field", "Properties"],
                          [[f.text("OBJECTNAME").strip(), "; ".join(property_strings(f, encoding))] for f in fields])

    if connections:
        doc.heading(2, "Connections")
        for c in connections:
            doc.heading(3, "Connection {}".format(c.text("OBJECTNAME").strip()))
            doc.bullets("Property: {}".format(p) for p in property_strings(c, encoding))

    if relations:
        doc.heading(2, "Persistent relations and referential integrity")
        rows = []
        for r in relations:
            owner = by_id.get(r.get("PARENTID"))
            ri = r.text("RIINFO").strip().upper()
            rules = ", ".join(
                "{} {}".format(label, RI_RULES.get(ri[i], ri[i])) for i, label in enumerate(("update", "delete", "insert")) if i < len(ri)
            )
            rows.append([owner.text("OBJECTNAME").strip() if owner is not None else "?",
                         "; ".join(property_strings(r, encoding)), rules])
        doc.table(["Child table", "Relation properties (tags / parent table)", "RI rules"], rows)

    procs = [r for r in by_type.get("database", []) if r.text("OBJECTNAME").strip().lower() == "storedproceduressource"]
    for r in procs:
        code = r.get("CODE")
        if isinstance(code, (bytes, bytearray)):
            code = _decode(bytes(code), encoding)
        if code and code.strip():
            doc.heading(2, "Stored procedures")
            doc.code(code, "foxpro")

    warnings_section(doc, ctx)
    return doc

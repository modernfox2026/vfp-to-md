"""DBF data tables: structure, indexes and a data sample."""

from __future__ import annotations

import os

from ..cdx import read_cdx_safe
from ..dbf import CODEPAGE_MARKS
from ..markdown import MarkdownDoc
from .base import Context, header_section, warnings_section

# Above this many columns a Markdown table becomes unreadable, so rows are
# rendered as one block per record instead.
WIDE_TABLE_COLUMNS = 12


def field_notes(f) -> str:
    notes = []
    if f.autoinc:
        notes.append("autoincrement (next {}, step {})".format(f.autoinc_next, f.autoinc_step))
    if f.binary:
        notes.append("binary / NOCPTRANS")
    if f.is_system:
        notes.append("system field")
    return ", ".join(notes)


def describe_structure(doc: MarkdownDoc, table) -> None:
    rows = []
    for i, f in enumerate(table.visible_fields, 1):
        rows.append([
            i,
            f.name,
            "{} ({})".format(f.type_name, f.type),
            f.length,
            f.decimals if f.type in ("N", "F", "B") else "",
            "yes" if f.nullable else "",
            field_notes(f),
        ])
    doc.table(["#", "Field", "Type", "Width", "Dec", "Null", "Notes"], rows)


def describe_indexes(doc: MarkdownDoc, ctx: Context, encoding: str) -> None:
    cdx_path = ctx.item.companions.get(".cdx")
    if not cdx_path:
        return
    tags, error = read_cdx_safe(cdx_path, encoding)
    doc.heading(2, "Indexes ({})".format(os.path.basename(cdx_path)))
    if error:
        doc.note("Index tags could not be read: {}".format(error))
        ctx.warn("CDX index not decoded: {}".format(error))
        return
    rows = [[t.name, t.expression, t.for_expression, "UNIQUE/CANDIDATE" if t.unique else "",
             "descending" if t.descending else "ascending"] for t in tags]
    doc.table(["Tag", "Key expression", "FOR filter", "Uniqueness", "Order"], rows)


def convert(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    opts = ctx.options
    with ctx.open_table(".fpt") as table:
        deleted = table.count_deleted()
        cp = "0x{:02X} ({})".format(table.codepage_mark, CODEPAGE_MARKS.get(table.codepage_mark, "unknown"))
        header_section(doc, ctx, [
            ("Table format", table.version_name),
            ("Last updated (header)", table.last_update),
            ("Records", "{} ({} marked deleted)".format(table.record_count, deleted)),
            ("Fields", len(table.visible_fields)),
            ("Record length", "{} bytes".format(table.record_length)),
            ("Code page", cp),
            ("Text decoded as", table.encoding),
            ("Memo file", os.path.basename(table.memo_path) if table.memo_path else ""),
            ("Structural index", "yes (.cdx)" if table.has_cdx else "no"),
            ("Belongs to database", table.backlink),
        ])
        if table.memo_missing:
            ctx.warn("memo file (.fpt) is missing; memo field contents are unavailable")
        if table.has_cdx and ".cdx" not in ctx.item.companions:
            ctx.warn("header says a structural .cdx index exists but the file was not found")

        doc.heading(2, "Structure")
        describe_structure(doc, table)
        describe_indexes(doc, ctx, table.encoding)

        doc.heading(2, "Data")
        limit = opts.max_rows if opts.max_rows > 0 else None
        fields = table.visible_fields
        records = list(table.records(include_deleted=opts.include_deleted, limit=limit))
        live = table.record_count - (0 if opts.include_deleted else deleted)
        if not records:
            doc.para("The table contains no records." if live == 0 else "No records could be read.")
        else:
            shown = "Showing {} of {} record(s)".format(len(records), live)
            if limit is not None and live > len(records):
                shown += " (limit set by --max-rows; use --max-rows 0 for all)"
            doc.para(shown + ".")
            show_deleted = opts.include_deleted and deleted
            if len(fields) <= WIDE_TABLE_COLUMNS:
                headers = (["Deleted"] if show_deleted else []) + [f.name for f in fields]
                rows = []
                for rec in records:
                    row = ([("*" if rec.deleted else "")] if show_deleted else []) + [rec.values.get(f.name) for f in fields]
                    rows.append(row)
                doc.table(headers, rows, limit=opts.max_cell)
            else:
                for rec in records:
                    title = "Record {}{}".format(rec.index, " (deleted)" if rec.deleted else "")
                    doc.heading(3, title)
                    doc.table(["Field", "Value"], [[f.name, rec.values.get(f.name)] for f in fields], limit=opts.max_cell)
        for rec in records:
            for err in rec.errors:
                ctx.warn("record {}: {}".format(rec.index, err))
        ctx.collect_table_warnings(table)
    warnings_section(doc, ctx)
    return doc


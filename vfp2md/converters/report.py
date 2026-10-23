"""Reports (.frx) and labels (.lbx)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..markdown import MarkdownDoc
from .base import Context, generic_records, header_section, parse_properties, warnings_section

OBJTYPES = {
    1: "Report header",
    2: "Work area",
    3: "Index",
    4: "Relation",
    5: "Label (text)",
    6: "Line",
    7: "Box / rectangle",
    8: "Field",
    9: "Band",
    10: "Group",
    17: "Picture / OLE",
    18: "Variable",
    21: "Printer driver setup",
    23: "Font resource",
    25: "Data environment",
    26: "Data environment object",
}

BANDS = {
    0: "Title",
    1: "Page header",
    2: "Column header",
    3: "Group header",
    4: "Detail",
    5: "Group footer",
    6: "Column footer",
    7: "Page footer",
    8: "Summary",
    9: "Detail header",
    10: "Detail footer",
}

TOTAL_TYPES = {0: "", 1: "Count", 2: "Sum", 3: "Average", 4: "Lowest", 5: "Highest", 6: "Std deviation", 7: "Variance"}
RESET_TYPES = {0: "", 1: "End of report", 2: "End of page", 3: "End of column"}

# The report designer draws a separator bar between bands; object positions
# include those bars, so they are added when locating objects inside bands.
BAND_SEPARATOR = 2083.333
UNITS_PER_INCH = 10000.0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(_num(value))
    except (OverflowError, ValueError):  # inf / nan from damaged numeric fields
        return 0


def _inches(value: Any) -> str:
    return "{:.2f}in".format(_num(value) / UNITS_PER_INCH)


def is_frx_layout(table) -> bool:
    return table.has_field("OBJTYPE") and table.has_field("EXPR")


def convert(ctx: Context) -> MarkdownDoc:
    memo_ext = ".lbt" if ctx.item.ext == ".lbx" else ".frt"
    doc = MarkdownDoc()
    with ctx.open_table(memo_ext) as table:
        if not is_frx_layout(table):
            header_section(doc, ctx)
            ctx.warn("unexpected column layout; rendered record by record")
            generic_records(doc, table, ctx, title_fields=("NAME", "EXPR"))
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        recs = []
        for rec in table.records(include_deleted=False):
            for err in rec.errors:
                ctx.warn("record {}: {}".format(rec.index, err))
            if rec.text("PLATFORM").strip().upper() not in ("WINDOWS", ""):
                continue
            recs.append(rec)
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file ({}) is missing: expressions are unavailable".format(memo_ext))

    by_type: Dict[int, List] = {}
    for rec in recs:
        by_type.setdefault(_int(rec.get("OBJTYPE")), []).append(rec)

    header = (by_type.get(1) or [None])[0]
    extra: List[Tuple[str, Any]] = []
    if header is not None:
        extra = [
            ("Columns", _int(header.get("VPOS")) or ""),
            ("Printer setup", header.text("EXPR").strip().replace("\n", "; ")[:200]),
        ]
    header_section(doc, ctx, extra)

    # Data environment ----------------------------------------------------
    de_objects = by_type.get(25, []) + by_type.get(26, [])
    if de_objects:
        doc.heading(2, "Data environment")
        rows = []
        for rec in de_objects:
            props = dict((k.lower(), v) for k, v in parse_properties(rec.text("TAG")))
            if not props:
                continue
            rows.append([
                rec.text("NAME").strip() or props.get("name", ""),
                props.get("alias", "").strip('"'),
                props.get("cursorsource", "").strip('"'),
                props.get("parentalias", "").strip('"'),
                props.get("childalias", "").strip('"'),
                props.get("relationalexpr", "").strip('"'),
                props.get("order", "").strip('"') or props.get("childorder", "").strip('"'),
            ])
        doc.table(["Object", "Alias", "Cursor source", "Parent alias", "Child alias", "Relational expr", "Order"], rows)
        for rec in de_objects:
            code = rec.text("TAG2").strip()
            if code and "PROCEDURE" in code.upper():
                doc.para("Event code for {}:".format(rec.text("NAME").strip() or "data environment"))
                doc.code(code, "foxpro")

    # Legacy (FoxPro 2.x style) work areas, indexes, relations -------------
    legacy = by_type.get(2, []) + by_type.get(3, []) + by_type.get(4, [])
    if legacy:
        doc.heading(2, "Environment (work areas, indexes, relations)")
        doc.table(["Type", "Name", "Expression", "Tag"],
                  [[OBJTYPES[_int(r.get("OBJTYPE"))], r.text("NAME"), r.text("EXPR"), r.text("TAG")] for r in legacy])

    # Groups and variables --------------------------------------------------
    groups = by_type.get(10, [])
    if groups:
        doc.heading(2, "Data grouping")
        doc.table(["Level", "Group expression", "Start on new page"],
                  [[i + 1, g.text("EXPR"), "yes" if g.get("EJECTBEFOR") is True else ""]
                   for i, g in enumerate(groups)])

    variables = by_type.get(18, [])
    if variables:
        doc.heading(2, "Report variables")
        doc.table(["Name", "Value to store", "Initial value", "Calculation", "Reset at"],
                  [[v.text("NAME"), v.text("EXPR"), v.text("TAG"),
                    TOTAL_TYPES.get(_int(v.get("TOTALTYPE")), _int(v.get("TOTALTYPE"))),
                    RESET_TYPES.get(_int(v.get("RESETTOTAL")), "group {}".format(_int(v.get("RESETTOTAL")) - 5))]
                   for v in variables])

    # Bands with their objects ---------------------------------------------
    bands = by_type.get(9, [])
    layout = [r for t in (5, 6, 7, 8, 17) for r in by_type.get(t, [])]
    layout.sort(key=lambda r: (_num(r.get("VPOS")), _num(r.get("HPOS"))))
    doc.heading(2, "Layout by band")
    if not bands:
        doc.para("No band records were found.")
    ranges = []
    top = 0.0
    group_counter = {3: 0, 5: 0}
    for band in bands:
        code = _int(band.get("OBJCODE"))
        name = BANDS.get(code, "Band code {}".format(code))
        if code in group_counter:
            group_counter[code] += 1
            name = "{} {}".format(name, group_counter[code])
        height = _num(band.get("HEIGHT"))
        ranges.append((name, band, top, top + height + BAND_SEPARATOR))
        top += height + BAND_SEPARATOR

    placed: Dict[int, List] = {i: [] for i in range(len(ranges))}
    unplaced = []
    for obj in layout:
        v = _num(obj.get("VPOS"))
        for i, (_, _, start, end) in enumerate(ranges):
            if start <= v < end:
                placed[i].append(obj)
                break
        else:
            unplaced.append(obj)

    for i, (name, band, _, _) in enumerate(ranges):
        doc.heading(3, "{} (height {})".format(name, _inches(band.get("HEIGHT"))))
        details = [
            ("Group expression", band.text("EXPR") if _int(band.get("OBJCODE")) in (3, 5) else ""),
            ("On entry", band.text("TAG")),
            ("On exit", band.text("TAG2")),
            ("Print when", band.text("SUPEXPR")),
        ]
        doc.kv(details)
        _object_table(doc, placed[i])
    if unplaced:
        doc.heading(3, "Objects outside any band")
        _object_table(doc, unplaced)

    warnings_section(doc, ctx)
    return doc


def _object_table(doc: MarkdownDoc, objects: List) -> None:
    rows = []
    for obj in objects:
        otype = _int(obj.get("OBJTYPE"))
        kind = OBJTYPES.get(otype, "Type {}".format(otype))
        content = obj.text("EXPR")
        if otype == 17:
            content = obj.text("PICTURE") or obj.text("NAME") or content
        calc = TOTAL_TYPES.get(_int(obj.get("TOTALTYPE")), "") if otype == 8 else ""
        rows.append([
            kind,
            content,
            obj.text("PICTURE") if otype == 8 else "",
            calc,
            obj.text("SUPEXPR"),
            "{} {}".format(obj.text("FONTFACE"), _int(obj.get("FONTSIZE")) or "").strip() if otype in (5, 8) else "",
            "{}, {}".format(_inches(obj.get("HPOS")), _inches(obj.get("VPOS"))),
            "{} x {}".format(_inches(obj.get("WIDTH")), _inches(obj.get("HEIGHT"))),
        ])
    if rows:
        doc.table(["Object", "Expression / text", "Format", "Calculation", "Print when", "Font", "Position (x, y)", "Size"], rows)
    else:
        doc.para("(empty band)")

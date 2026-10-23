"""Forms (.scx) and visual class libraries (.vcx).

Both are DBF tables where each row is one object: a form, a control, a
data environment cursor, or (in a .vcx) a class definition and its member
objects. Rows link to their container through the PARENT column.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from ..markdown import MarkdownDoc, inline_code
from .base import Context, generic_records, header_section, parse_properties, split_procedures, warnings_section


class VfpObject:
    def __init__(self, rec):
        self.index = rec.index
        self.platform = rec.text("PLATFORM").strip()
        self.uniqueid = rec.text("UNIQUEID").strip()
        self.cls = rec.text("CLASS").strip()
        self.classloc = rec.text("CLASSLOC").strip()
        self.baseclass = rec.text("BASECLASS").strip()
        self.objname = rec.text("OBJNAME").strip()
        self.parent = rec.text("PARENT").strip()
        self.properties = parse_properties(rec.text("PROPERTIES"))
        self.protected = [p.strip() for p in rec.text("PROTECTED").replace("\r", "\n").split("\n") if p.strip()]
        self.methods = rec.text("METHODS")
        self.reserved = {i: rec.text("RESERVED{}".format(i)) for i in range(1, 9)}
        self.user = rec.text("USER").strip()
        ole = rec.get("OLE")
        self.ole_size = len(ole) if isinstance(ole, (bytes, bytearray)) else len(ole or "")
        ole2 = rec.get("OLE2")
        self.ole2 = ole2.strip() if isinstance(ole2, str) else ""

    @property
    def path(self) -> str:
        return "{}.{}".format(self.parent, self.objname) if self.parent else self.objname

    @property
    def is_class_header(self) -> bool:
        return self.reserved[1].strip().lower() == "class"

    def prop(self, name: str) -> str:
        for k, v in self.properties:
            if k.lower() == name.lower():
                return v
        return ""


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        return value[1:-1]
    return value


def parse_member_defs(text: str) -> List[Tuple[str, str, str]]:
    """Parse RESERVED3 (custom members): ``*method``, ``^array[1,0]``, ``property``."""
    rows = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("_memberdata"):
            if line.startswith("_memberdata"):
                rows.append(("_memberdata", "property", "member metadata (XML)"))
            continue
        name, _, desc = line.partition(" ")
        if name.startswith("*"):
            kind, name = "method", name[1:]
        elif name.startswith("^"):
            kind, name = "array property", name[1:]
        else:
            kind = "property"
        rows.append((name, kind, desc.strip()))
    return rows


def describe_class(obj: VfpObject) -> str:
    parts = []
    if obj.baseclass:
        parts.append(obj.baseclass)
    if obj.cls and obj.cls.lower() != obj.baseclass.lower():
        src = " from {}".format(os.path.basename(obj.classloc.replace("\\", "/"))) if obj.classloc else ""
        parts.append("class {}{}".format(obj.cls, src))
    return ", ".join(parts)


def render_tree(doc: MarkdownDoc, objects: List[VfpObject], root_parent: str = "") -> None:
    children: Dict[str, List[VfpObject]] = {}
    for o in objects:
        children.setdefault(o.parent.lower(), []).append(o)
    lines: List[str] = []
    seen = set()

    def walk(parent_path: str, depth: int) -> None:
        if depth > 50:
            return
        for o in children.get(parent_path.lower(), []):
            if id(o) in seen:
                continue
            seen.add(id(o))
            lines.append("{}- {} ({})".format("  " * depth, inline_code(o.objname or "?"), describe_class(o) or "?"))
            walk(o.path, depth + 1)

    walk(root_parent, 0)
    # Objects whose parent never appeared (damaged files) still get listed.
    for o in objects:
        if id(o) not in seen:
            seen.add(id(o))
            lines.append("- {} ({}); parent {} not found".format(inline_code(o.path), describe_class(o) or "?", inline_code(o.parent)))
            walk(o.path, 1)
    if lines:
        doc.raw("\n".join(lines))


def render_methods(doc: MarkdownDoc, code: str, level: int) -> None:
    if not code.strip():
        return
    preamble, procs = split_procedures(code)
    doc.heading(level, "Methods")
    if preamble:
        doc.code(preamble, "foxpro")
    for name, body in procs:
        doc.heading(level + 1, name)
        doc.code(body, "foxpro")


def render_object(doc: MarkdownDoc, obj: VfpObject, level: int, title: Optional[str] = None) -> None:
    doc.heading(level, title or "{} ({})".format(obj.path, obj.baseclass or "object"))
    doc.kv([
        ("Base class", obj.baseclass),
        ("Class", obj.cls if obj.cls.lower() != obj.baseclass.lower() else ""),
        ("Class library", obj.classloc),
        ("Contained in", obj.parent),
        ("ActiveX control", "{} ({} bytes of OLE data)".format(obj.ole2.split("\n")[0], obj.ole_size) if obj.ole_size else ""),
    ])
    if obj.properties:
        doc.heading(level + 1, "Properties")
        doc.table(["Property", "Value"], obj.properties)
    members = parse_member_defs(obj.reserved[3])
    if members:
        doc.heading(level + 1, "Custom properties and methods")
        doc.table(["Member", "Kind", "Description"], members)
    if obj.protected:
        doc.heading(level + 1, "Protected / hidden members")
        doc.bullets(
            "{} ({})".format(inline_code(p.rstrip("^")), "hidden" if p.endswith("^") else "protected") for p in obj.protected
        )
    render_methods(doc, obj.methods, level + 1)
    if obj.user:
        doc.heading(level + 1, "User data")
        doc.code(obj.user)


def render_data_environment(doc: MarkdownDoc, objects: List[VfpObject], level: int) -> None:
    cursors = [o for o in objects if o.baseclass.lower() == "cursor"]
    adapters = [o for o in objects if o.baseclass.lower() == "cursoradapter"]
    relations = [o for o in objects if o.baseclass.lower() == "relation"]
    if not (cursors or adapters or relations):
        return
    doc.heading(level, "Data environment")
    if cursors:
        doc.table(
            ["Alias", "Cursor source", "Database", "Order", "Read only", "Filter"],
            [[unquote(c.prop("Alias")), unquote(c.prop("CursorSource")), unquote(c.prop("Database")),
              unquote(c.prop("Order")), c.prop("ReadOnly"), unquote(c.prop("Filter"))] for c in cursors],
        )
    if adapters:
        doc.para("Cursor adapters:")
        doc.table(
            ["Alias", "Data source type", "Tables", "Select command", "Key fields"],
            [[unquote(a.prop("Alias")), unquote(a.prop("DataSourceType")), unquote(a.prop("Tables")),
              unquote(a.prop("SelectCmd")), unquote(a.prop("KeyFieldList"))] for a in adapters],
        )
    if relations:
        doc.para("Relations:")
        doc.table(
            ["Parent alias", "Child alias", "Child order", "Relational expression", "One to many"],
            [[unquote(r.prop("ParentAlias")), unquote(r.prop("ChildAlias")), unquote(r.prop("ChildOrder")),
              unquote(r.prop("RelationalExpr")), r.prop("OneToMany")] for r in relations],
        )


def load_objects(table, ctx: Context) -> List[VfpObject]:
    objects = []
    for rec in table.records(include_deleted=False):
        for err in rec.errors:
            ctx.warn("record {}: {}".format(rec.index, err))
        obj = VfpObject(rec)
        if obj.platform.upper() == "COMMENT":
            continue  # header/footer bookkeeping rows (font metrics, markers)
        if obj.platform and obj.platform.upper() not in ("WINDOWS", ""):
            ctx.warn("record {} is for platform {!r}".format(rec.index, obj.platform))
        objects.append(obj)
    return objects


def is_vfp_layout(table) -> bool:
    return table.has_field("OBJNAME") and table.has_field("PROPERTIES")


def convert_legacy(doc: MarkdownDoc, table, ctx: Context) -> None:
    """FoxPro 2.x screens/class files have a different column layout."""
    ctx.warn("file uses the FoxPro 2.x layout; rendered record by record")
    doc.heading(2, "Screen objects (FoxPro 2.x layout)")
    generic_records(doc, table, ctx, title_fields=("NAME", "EXPR"))


def convert_form(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    with ctx.open_table(".sct") as table:
        if not is_vfp_layout(table):
            header_section(doc, ctx)
            convert_legacy(doc, table, ctx)
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        objects = load_objects(table, ctx)
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file (.sct) is missing: properties and code are unavailable")

    forms = [o for o in objects if o.baseclass.lower() in ("form", "formset", "toolbar")]
    controls = [o for o in objects if o.baseclass.lower() not in ("form", "formset", "dataenvironment", "cursor", "relation", "cursoradapter")]
    method_count = sum(len(split_procedures(o.methods)[1]) for o in objects)
    header_section(doc, ctx, [
        ("Forms", ", ".join("{} ({})".format(f.objname, unquote(f.prop("Caption")) or "no caption") for f in forms)),
        ("Objects", len(objects)),
        ("Controls", len(controls)),
        ("Methods with code", method_count),
    ])
    render_data_environment(doc, objects, 2)
    doc.heading(2, "Object hierarchy")
    render_tree(doc, objects)
    doc.heading(2, "Objects")
    for obj in objects:
        render_object(doc, obj, 3)
    warnings_section(doc, ctx)
    return doc


def convert_classlib(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    with ctx.open_table(".vct") as table:
        if not is_vfp_layout(table):
            header_section(doc, ctx)
            convert_legacy(doc, table, ctx)
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        objects = load_objects(table, ctx)
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file (.vct) is missing: properties and code are unavailable")

    # Group member rows under their class: the first segment of PARENT is the class name.
    classes: List[Tuple[VfpObject, List[VfpObject]]] = []
    by_name: Dict[str, List[VfpObject]] = {}
    current: Optional[List[VfpObject]] = None
    for obj in objects:
        if obj.is_class_header or (not obj.parent and obj.objname):
            members: List[VfpObject] = []
            classes.append((obj, members))
            by_name[obj.objname.lower()] = members
            current = members
            continue
        owner = by_name.get(obj.parent.split(".", 1)[0].lower())
        if owner is not None:
            owner.append(obj)
        elif current is not None:
            current.append(obj)
        else:
            ctx.warn("object {} (record {}) does not belong to any class".format(obj.path, obj.index))

    header_section(doc, ctx, [("Classes", len(classes))])
    doc.heading(2, "Class summary")
    doc.table(
        ["Class", "Parent class", "Base class", "Parent class library", "Description"],
        [[c.objname, c.cls, c.baseclass, c.classloc or "(VFP base class)", c.reserved[7].strip()] for c, _ in classes],
    )
    for cls, members in classes:
        doc.heading(2, "Class {}".format(cls.objname))
        doc.kv([
            ("Parent class", cls.cls),
            ("Base class", cls.baseclass),
            ("Parent class library", cls.classloc or "(VFP base class)"),
            ("Description", cls.reserved[7].strip()),
            ("Include file", cls.reserved[8].strip()),
            ("Member objects", len(members)),
        ])
        render_object(doc, cls, 3, title="Definition")
        if members:
            doc.heading(3, "Member hierarchy")
            render_tree(doc, members, root_parent=cls.objname)
            for m in members:
                render_object(doc, m, 3)
    warnings_section(doc, ctx)
    return doc

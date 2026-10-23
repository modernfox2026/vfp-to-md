"""Menus (.mnx)."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ..markdown import MarkdownDoc, inline_code
from .base import Context, generic_records, header_section, warnings_section

ACTION_COMMAND = 67
ACTION_SUBMENU = 77
ACTION_BAR = 78
ACTION_PROCEDURE = 80

LOCATIONS = {0: "replace system menu", 1: "append to system menu", 2: "before", 3: "after"}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


class MenuNode:
    def __init__(self, rec, children: Optional[List["MenuItem"]] = None):
        self.rec = rec
        self.items: List[MenuItem] = children or []


class MenuItem:
    def __init__(self, rec, submenu: Optional[MenuNode] = None):
        self.rec = rec
        self.submenu = submenu

    @property
    def prompt(self) -> str:
        return self.rec.text("PROMPT").strip()


def _parse_popup(recs: List, i: int, depth: int = 0) -> Tuple[MenuNode, int]:
    if depth > 30:
        raise ValueError("menu nesting is too deep")
    popup = recs[i]
    if _int(popup.get("OBJTYPE")) != 2:
        raise ValueError("expected a popup definition at record {}".format(popup.index))
    node = MenuNode(popup)
    count = _int(popup.get("NUMITEMS"))
    i += 1
    for _ in range(count):
        if i >= len(recs):
            raise ValueError("menu ended before all items of {} were read".format(popup.text("NAME").strip()))
        item = recs[i]
        if _int(item.get("OBJTYPE")) != 3:
            raise ValueError("expected a menu item at record {}".format(item.index))
        i += 1
        sub = None
        if _int(item.get("OBJCODE")) == ACTION_SUBMENU and i < len(recs) and _int(recs[i].get("OBJTYPE")) == 2:
            sub, i = _parse_popup(recs, i, depth + 1)
        node.items.append(MenuItem(item, sub))
    return node, i


def _action(item: MenuItem) -> str:
    rec = item.rec
    code = _int(rec.get("OBJCODE"))
    if code == ACTION_COMMAND:
        cmd = rec.text("COMMAND").strip()
        return "command {}".format(inline_code(cmd)) if cmd else "command (empty)"
    if code == ACTION_SUBMENU:
        return "submenu"
    if code == ACTION_BAR:
        return "system bar {}".format(inline_code(rec.text("NAME").strip()))
    if code == ACTION_PROCEDURE:
        return "procedure (see below)" if rec.text("PROCEDURE").strip() else "procedure (empty)"
    return "action code {}".format(code) if code else ""


def _render_items(lines: List[str], items: List[MenuItem], depth: int, path: List[str], procs: List) -> None:
    for item in items:
        rec = item.rec
        prompt = item.prompt
        if prompt == "\\-":
            lines.append("{}- ---- (separator)".format("  " * depth))
            continue
        label = prompt.replace("\\<", "") or "(no prompt)"
        bits = [_action(item)]
        key = rec.text("KEYLABEL").strip() or rec.text("KEYNAME").strip()
        if key:
            bits.append("shortcut {}".format(inline_code(key)))
        skip = rec.text("SKIPFOR").strip()
        if skip:
            bits.append("skip for {}".format(inline_code(skip)))
        message = rec.text("MESSAGE").strip()
        if message:
            bits.append("message {}".format(inline_code(message)))
        name = rec.text("NAME").strip()
        if name and _int(rec.get("OBJCODE")) != ACTION_BAR:
            bits.append("name {}".format(inline_code(name)))
        comment = rec.text("COMMENT").strip()
        if comment:
            bits.append("comment: {}".format(comment.replace("\n", " ")))
        lines.append("{}- **{}**: {}".format("  " * depth, label, "; ".join(b for b in bits if b)))
        here = path + [label]
        if _int(rec.get("OBJCODE")) == ACTION_PROCEDURE and rec.text("PROCEDURE").strip():
            procs.append((" > ".join(here), rec.text("PROCEDURE")))
        if item.submenu is not None:
            sub_proc = item.submenu.rec.text("PROCEDURE").strip()
            if sub_proc:
                procs.append((" > ".join(here) + " (submenu default procedure)", sub_proc))
            _render_items(lines, item.submenu.items, depth + 1, here, procs)


def convert(ctx: Context) -> MarkdownDoc:
    doc = MarkdownDoc()
    with ctx.open_table(".mnt") as table:
        if not (table.has_field("OBJTYPE") and table.has_field("PROMPT")):
            header_section(doc, ctx)
            ctx.warn("unexpected column layout; rendered record by record")
            generic_records(doc, table, ctx, title_fields=("PROMPT", "NAME"))
            ctx.collect_table_warnings(table)
            warnings_section(doc, ctx)
            return doc
        recs = list(table.records(include_deleted=False))
        ctx.collect_table_warnings(table)
        if table.memo_missing:
            ctx.warn("memo file (.mnt) is missing: commands and code are unavailable")
    for rec in recs:
        for err in rec.errors:
            ctx.warn("record {}: {}".format(rec.index, err))

    header = next((r for r in recs if _int(r.get("OBJTYPE")) == 1), None)
    extra = []
    if header is not None:
        extra.append(("Location", LOCATIONS.get(_int(header.get("LOCATION")), "")))
    header_section(doc, ctx, extra)

    body = [r for r in recs if _int(r.get("OBJTYPE")) != 1]
    roots: List[MenuNode] = []
    try:
        i = 0
        while i < len(body):
            node, i = _parse_popup(body, i)
            roots.append(node)
    except (ValueError, IndexError) as exc:
        roots = []
        ctx.warn("menu hierarchy could not be rebuilt ({}); showing a flat list".format(exc))

    procs: List[Tuple[str, str]] = []
    doc.heading(2, "Menu structure")
    if roots:
        for root in roots:
            level = root.rec.text("LEVELNAME").strip() or root.rec.text("NAME").strip()
            doc.para("Menu {}:".format(inline_code(level or "(unnamed)")))
            lines: List[str] = []
            _render_items(lines, root.items, 0, [], procs)
            doc.raw("\n".join(lines) if lines else "(no items)")
    else:
        rows = []
        for r in body:
            item = MenuItem(r)
            if _int(r.get("OBJTYPE")) == 3 and _int(r.get("OBJCODE")) == ACTION_PROCEDURE and r.text("PROCEDURE").strip():
                procs.append((item.prompt.replace("\\<", ""), r.text("PROCEDURE")))
            rows.append([r.text("LEVELNAME"), "popup" if _int(r.get("OBJTYPE")) == 2 else "item",
                         item.prompt, _action(item), r.text("KEYLABEL"), r.text("SKIPFOR")])
        doc.table(["Level", "Type", "Prompt", "Action", "Shortcut", "Skip for"], rows)

    if header is not None:
        for title, field in (("Setup code", "SETUP"), ("Cleanup code", "CLEANUP"), ("Default procedure", "PROCEDURE")):
            code = header.text(field)
            if code.strip():
                doc.heading(2, title)
                doc.code(code, "foxpro")
    if procs:
        doc.heading(2, "Item procedures")
        for name, code in procs:
            doc.heading(3, name)
            doc.code(code, "foxpro")
    warnings_section(doc, ctx)
    return doc

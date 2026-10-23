"""Converter registry: maps a file kind to the function that renders it."""

from __future__ import annotations

from typing import Callable, Dict

from ..markdown import MarkdownDoc
from . import database, form, menu, project, report, table, text
from .base import Context, Options

Converter = Callable[[Context], MarkdownDoc]

CONVERTERS: Dict[str, Converter] = {
    "project": project.convert,
    "database": database.convert,
    "form": form.convert_form,
    "classlib": form.convert_classlib,
    "report": report.convert,
    "label": report.convert,
    "menu": menu.convert,
    "table": table.convert,
    "program": text.convert,
    "text_export": text.convert,
    "config": text.convert,
}

__all__ = ["CONVERTERS", "Context", "Options"]

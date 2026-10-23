"""Locate VFP files in a directory tree and group them with their companions.

A VFP "file" is usually a family on disk: ``main.scx`` (the table) plus
``main.sct`` (its memo). Users pick the primary file; companions are attached
automatically so they are never converted on their own.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Dict, List, Optional, Tuple


class FileKind:
    def __init__(self, key: str, label: str, extensions: List[str], companions: List[str], required: Optional[str] = None):
        self.key = key
        self.label = label
        self.extensions = extensions
        self.companions = companions
        # Companion that holds the memo data; without it most content is lost.
        self.required = required


KINDS: List[FileKind] = [
    FileKind("project", "Project", [".pjx"], [".pjt"], ".pjt"),
    FileKind("database", "Database container", [".dbc"], [".dct", ".dcx"], ".dct"),
    FileKind("form", "Form", [".scx"], [".sct"], ".sct"),
    FileKind("classlib", "Visual class library", [".vcx"], [".vct"], ".vct"),
    FileKind("report", "Report", [".frx"], [".frt"], ".frt"),
    FileKind("label", "Label", [".lbx"], [".lbt"], ".lbt"),
    FileKind("menu", "Menu", [".mnx"], [".mnt"], ".mnt"),
    FileKind("table", "Table", [".dbf"], [".fpt", ".dbt", ".cdx"], None),
    FileKind("program", "Program / source", [".prg", ".h", ".qpr", ".mpr", ".spr", ".fmt", ".act"], [], None),
    FileKind(
        "text_export",
        "Text export (SCCText / FoxBin2Prg)",
        [".sca", ".vca", ".fra", ".lba", ".mna", ".dba", ".pja",
         ".sc2", ".vc2", ".fr2", ".lb2", ".mn2", ".pj2", ".db2", ".dc2"],
        [],
        None,
    ),
    FileKind("config", "Configuration / text", [".fpw", ".ini", ".cfg", ".txt", ".xml", ".json", ".htm", ".html", ".css", ".js"], [], None),
]

KIND_BY_KEY: Dict[str, FileKind] = {k.key: k for k in KINDS}
KIND_BY_EXT: Dict[str, FileKind] = {ext: k for k in KINDS for ext in k.extensions}

# companion extension -> primary extensions that own it
COMPANION_OWNERS: Dict[str, List[str]] = {}
for _kind in KINDS:
    for _comp in _kind.companions:
        COMPANION_OWNERS.setdefault(_comp, []).extend(_kind.extensions)

# Files that cannot be meaningfully converted to text; reported but not listed.
COMPILED_EXTS = {".fxp", ".app", ".exe", ".dll", ".fll", ".mpx", ".qpx", ".spx", ".ocx", ".err", ".idx"}


class DiscoveredFile:
    def __init__(self, path: str, relpath: str, kind: FileKind, size: int):
        self.path = path
        self.relpath = relpath
        self.kind = kind
        self.size = size
        self.companions: Dict[str, str] = {}

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    @property
    def missing_required(self) -> Optional[str]:
        req = self.kind.required
        if req and req not in self.companions:
            return req
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return "DiscoveredFile({!r}, {})".format(self.relpath, self.kind.key)


class ScanResult:
    def __init__(self, root: str):
        self.root = root
        self.files: List[DiscoveredFile] = []
        self.orphans: List[str] = []  # companions without a primary
        self.skipped_compiled: List[str] = []
        self.errors: List[str] = []


def scan(root: str, recursive: bool = True, include_hidden: bool = False) -> ScanResult:
    """Walk ``root`` and return every convertible VFP file, sorted by kind then path."""
    root = os.path.abspath(root)
    result = ScanResult(root)

    def onerror(exc: OSError) -> None:
        result.errors.append("cannot read {}: {}".format(getattr(exc, "filename", "?"), exc.strerror or exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames.sort(key=str.lower)
        if not recursive:
            dirnames[:] = []

        by_lower: Dict[str, str] = {name.lower(): name for name in filenames}
        primaries: List[Tuple[str, FileKind]] = []
        claimed = set()
        for name in sorted(filenames, key=str.lower):
            if not include_hidden and name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            kind = KIND_BY_EXT.get(ext)
            if kind is not None:
                primaries.append((name, kind))
            elif ext in COMPILED_EXTS:
                result.skipped_compiled.append(os.path.relpath(os.path.join(dirpath, name), root))

        for name, kind in primaries:
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                result.errors.append("cannot stat {}: {}".format(path, exc))
                continue
            item = DiscoveredFile(path, os.path.relpath(path, root), kind, size)
            stem = os.path.splitext(name)[0].lower()
            for comp in kind.companions:
                actual = by_lower.get(stem + comp)
                if actual:
                    item.companions[comp] = os.path.join(dirpath, actual)
                    claimed.add(actual.lower())
            result.files.append(item)

        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in COMPANION_OWNERS and name.lower() not in claimed:
                result.orphans.append(os.path.relpath(os.path.join(dirpath, name), root))

    order = {k.key: i for i, k in enumerate(KINDS)}
    result.files.sort(key=lambda f: (order[f.kind.key], f.relpath.lower()))
    result.orphans.sort(key=str.lower)
    return result


# --------------------------------------------------------------------------
# Selection expressions
# --------------------------------------------------------------------------

SELECTION_HELP = """\
Selection syntax (combine with commas or spaces):
  all              every file
  3                file number 3
  2-7              files 2 through 7
  .scx             every file with that extension
  form, table ...  every file of a kind (project, database, form, classlib,
                   report, label, menu, table, program, text_export, config)
  *cust*           a wildcard pattern matched against the relative path
  !5  !.dbf  !*tmp*  exclude anything matched by the rest of the token
Examples:  all !table     1-4,9     .scx .vcx     *orders*"""


class SelectionError(ValueError):
    pass


def _match_token(token: str, files: List[DiscoveredFile]) -> List[int]:
    t = token.strip()
    low = t.lower()
    n = len(files)
    if low in ("all", "*"):
        return list(range(n))
    if t.isdigit():
        i = int(t)
        if not 1 <= i <= n:
            raise SelectionError("number {} is out of range (1-{})".format(i, n))
        return [i - 1]
    if "-" in t and all(p.strip().isdigit() for p in t.split("-", 1)):
        a, b = (int(p) for p in t.split("-", 1))
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            raise SelectionError("range {} is out of range (1-{})".format(t, n))
        return list(range(a - 1, b))
    if low in KIND_BY_KEY:
        return [i for i, f in enumerate(files) if f.kind.key == low]
    if low.startswith(".") and not any(c in low for c in "*?["):
        hits = [i for i, f in enumerate(files) if f.ext == low]
        if not hits:
            raise SelectionError("no listed files have the extension {}".format(t))
        return hits
    pattern = low.replace("\\", "/")
    hits = [
        i for i, f in enumerate(files)
        if fnmatch.fnmatch(f.relpath.lower().replace("\\", "/"), pattern)
        or fnmatch.fnmatch(os.path.basename(f.relpath).lower(), pattern)
    ]
    if not hits:
        raise SelectionError("nothing matches {!r}".format(t))
    return hits


def parse_selection(expr: str, files: List[DiscoveredFile]) -> List[DiscoveredFile]:
    """Turn a selection expression into an ordered list of files.

    Raises SelectionError with a user friendly message on bad input.
    """
    tokens = [t for t in expr.replace(",", " ").split() if t]
    if not tokens:
        raise SelectionError("nothing entered")
    include: List[int] = []
    exclude = set()
    only_excludes = all(t.startswith("!") for t in tokens)
    if only_excludes:
        include = list(range(len(files)))
    for token in tokens:
        if token.startswith("!"):
            rest = token[1:]
            if not rest:
                raise SelectionError("'!' must be followed by something to exclude")
            exclude.update(_match_token(rest, files))
        else:
            include.extend(_match_token(token, files))
    seen = set()
    chosen = []
    for i in include:
        if i not in seen and i not in exclude:
            seen.add(i)
            chosen.append(files[i])
    return chosen

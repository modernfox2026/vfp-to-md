"""File system helpers that cope with the case insensitive habits of VFP.

VFP projects come from Windows, where ``MAIN.SCX`` and ``main.sct`` are the
same family of files. On Linux/macOS the case differs on disk, so companion
lookups must ignore case.
"""

from __future__ import annotations

import os
from typing import Optional


def find_companion(path: str, ext: str) -> Optional[str]:
    """Find ``<stem><ext>`` next to ``path``, ignoring case. Returns a path or None."""
    directory = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    exact = os.path.join(directory, stem + ext)
    if os.path.isfile(exact):
        return exact
    wanted = (stem + ext).lower()
    try:
        for name in os.listdir(directory):
            if name.lower() == wanted and os.path.isfile(os.path.join(directory, name)):
                return os.path.join(directory, name)
    except OSError:
        return None
    return None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit == "B" else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{} B".format(size)  # pragma: no cover


_INVALID_CHARS = '<>:"|?*'


def safe_relpath(relpath: str) -> str:
    """Make a relative path safe to create on any OS (no '..', no reserved chars)."""
    parts = []
    for part in relpath.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        cleaned = "".join("_" if (c in _INVALID_CHARS or ord(c) < 32) else c for c in part).rstrip(" .")
        parts.append(cleaned or "_")
    return os.path.join(*parts) if parts else "_"

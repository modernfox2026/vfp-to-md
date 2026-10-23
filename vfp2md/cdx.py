"""Extract tag names and key expressions from FoxPro compound indexes (.cdx).

Index expressions tell a migration what the keys of a table are (primary
keys, sort orders, filtered indexes) even when the database container is
missing. Only the metadata is read; the B tree of key values is ignored.

Layout reference: the "Compound Index File Structure (.cdx)" chapter of the
Visual FoxPro documentation. Header and leaf fields are little endian,
pointers inside interior nodes are big endian.
"""

from __future__ import annotations

import os
import struct
from typing import List, Optional

PAGE = 512
HEADER = 1024
_NONE = 0xFFFFFFFF

OPT_UNIQUE = 0x01
OPT_FOR = 0x08
OPT_COMPACT = 0x20
OPT_COMPOUND = 0x40


class CdxTag:
    def __init__(self, name: str, expression: str, for_expression: str, options: int, descending: bool, key_size: int):
        self.name = name
        self.expression = expression
        self.for_expression = for_expression
        self.options = options
        self.descending = descending
        self.key_size = key_size

    @property
    def unique(self) -> bool:
        return bool(self.options & OPT_UNIQUE)


class CdxError(Exception):
    pass


def _cstr(data: bytes, encoding: str) -> str:
    return data.split(b"\x00", 1)[0].decode(encoding, "replace").strip()


def _parse_tag_header(buf: bytes, encoding: str):
    if len(buf) < HEADER:
        raise CdxError("truncated tag header")
    key_size = struct.unpack("<H", buf[12:14])[0]
    options = buf[14]
    descending = struct.unpack("<H", buf[502:504])[0] == 1
    for_pos, for_len, key_pos, key_len = struct.unpack("<HHHH", buf[504:512])
    pool = buf[512:1024]
    expr = ""
    for_expr = ""
    if key_len and key_pos + key_len <= len(pool):
        expr = _cstr(pool[key_pos : key_pos + key_len], encoding)
    if for_len > 1 and for_pos + for_len <= len(pool):
        for_expr = _cstr(pool[for_pos : for_pos + for_len], encoding)
    if not expr:
        parts = [p for p in pool.split(b"\x00") if p.strip()]
        if parts:
            expr = parts[0].decode(encoding, "replace").strip()
            if len(parts) > 1 and options & OPT_FOR:
                for_expr = parts[1].decode(encoding, "replace").strip()
    return key_size, options, descending, expr, for_expr


def _leaf_keys(node: bytes, key_size: int) -> List[tuple]:
    nkeys = struct.unpack("<H", node[2:4])[0]
    rec_mask = struct.unpack("<I", node[14:18])[0]
    dup_mask, trail_mask = node[18], node[19]
    rec_bits, dup_bits = node[20], node[21]
    nbytes = node[23]
    if nbytes == 0 or nbytes > 8 or 24 + nkeys * nbytes > PAGE:
        raise CdxError("implausible leaf node layout")
    keys = []
    prev = b""
    end = PAGE
    for i in range(nkeys):
        info = int.from_bytes(node[24 + i * nbytes : 24 + (i + 1) * nbytes], "little")
        recno = info & rec_mask
        dup = (info >> rec_bits) & dup_mask
        trail = (info >> (rec_bits + dup_bits)) & trail_mask
        stored = key_size - dup - trail
        if stored < 0 or dup > len(prev):
            raise CdxError("corrupt key compression data")
        end -= stored
        if end < 24 + nkeys * nbytes:
            raise CdxError("key data overlaps key info area")
        key = prev[:dup] + node[end : end + stored]
        prev = key + b" " * trail
        keys.append((prev, recno))
    return keys


def read_cdx(path: str, encoding: str = "cp1252") -> List[CdxTag]:
    """Return the tags stored in a .cdx file. Raises CdxError when unreadable."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise CdxError(str(exc)) from exc
    if size < HEADER + PAGE:
        raise CdxError("file is too small to be a compound index")

    root = struct.unpack("<I", data[0:4])[0]
    name_size = struct.unpack("<H", data[12:14])[0] or 10

    def node_at(offset: int) -> bytes:
        if offset % PAGE or offset + PAGE > size:
            raise CdxError("node pointer {} is invalid".format(offset))
        return data[offset : offset + PAGE]

    # Descend to the leftmost leaf of the tag name tree.
    node_offset = root
    for _ in range(32):
        node = node_at(node_offset)
        attrs = struct.unpack("<H", node[0:2])[0]
        if attrs & 0x02:
            break
        child_at = 12 + name_size + 4
        node_offset = struct.unpack(">I", node[child_at : child_at + 4])[0]
    else:
        raise CdxError("tag tree is too deep")

    entries = []
    visited = set()
    while node_offset not in visited and len(visited) < 4096:
        visited.add(node_offset)
        node = node_at(node_offset)
        entries.extend(_leaf_keys(node, name_size))
        right = struct.unpack("<I", node[8:12])[0]
        if right == _NONE or right == 0:
            break
        node_offset = right

    tags = []
    for raw_name, offset in entries:
        name = raw_name.split(b"\x00", 1)[0].decode(encoding, "replace").strip()
        if offset + HEADER > size:
            continue
        key_size, options, descending, expr, for_expr = _parse_tag_header(data[offset : offset + HEADER], encoding)
        tags.append(CdxTag(name, expr, for_expr, options, descending, key_size))
    if not tags:
        raise CdxError("no tags found")
    return tags


def read_cdx_safe(path: str, encoding: str = "cp1252") -> "tuple[List[CdxTag], Optional[str]]":
    """Like read_cdx but returns (tags, error_message) and never raises."""
    try:
        return read_cdx(path, encoding), None
    except (CdxError, struct.error, IndexError, ValueError) as exc:
        return [], str(exc)

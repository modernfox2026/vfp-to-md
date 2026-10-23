"""Tolerant reader for Visual FoxPro / FoxPro / dBase style tables.

Almost every VFP design file (.scx, .vcx, .frx, .lbx, .mnx, .pjx, .dbc) is
really a DBF table with a memo companion, so this module is the foundation
for every structured converter in the package.

The reader is deliberately forgiving: legacy files are frequently damaged
(wrong record counts, truncated memo files, odd code pages), so problems are
collected in ``DbfTable.warnings`` instead of aborting whenever a sensible
fallback exists.
"""

from __future__ import annotations

import datetime as _dt
import os
import struct
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple

from .errors import DbfFormatError, MemoError
from .fsutil import find_companion

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VERSION_NAMES = {
    0x02: "FoxBASE",
    0x03: "dBase III / FoxPro (no memo)",
    0x30: "Visual FoxPro",
    0x31: "Visual FoxPro (autoincrement)",
    0x32: "Visual FoxPro (varchar/varbinary)",
    0x43: "dBase IV SQL table",
    0x63: "dBase IV SQL system file",
    0x83: "dBase III with memo",
    0x8B: "dBase IV with memo",
    0xCB: "dBase IV SQL table with memo",
    0xF5: "FoxPro 2.x with memo",
    0xFB: "FoxBASE",
}

FIELD_TYPE_NAMES = {
    "C": "Character",
    "N": "Numeric",
    "F": "Float",
    "D": "Date",
    "T": "DateTime",
    "L": "Logical",
    "M": "Memo",
    "G": "General",
    "P": "Picture",
    "Y": "Currency",
    "I": "Integer",
    "B": "Double",
    "V": "Varchar",
    "Q": "Varbinary",
    "W": "Blob",
    "0": "NullFlags (system)",
    "+": "Autoincrement",
    "@": "Timestamp",
}

MEMO_TYPES = {"M", "G", "P", "W"}
BINARY_MEMO_TYPES = {"G", "P", "W"}

# Code page marks stored at offset 29 of the header, mapped to Python codecs.
# Entries that Python cannot decode natively fall back to a close relative.
CODEPAGE_MARKS = {
    0x01: "cp437",
    0x02: "cp850",
    0x03: "cp1252",
    0x04: "mac_roman",
    0x64: "cp852",
    0x65: "cp866",
    0x66: "cp865",
    0x67: "cp861",
    0x68: "cp852",  # Kamenicky (895) has no Python codec; cp852 is closest
    0x69: "cp852",  # Mazovia (620) has no Python codec; cp852 is closest
    0x6A: "cp737",
    0x6B: "cp857",
    0x78: "cp950",
    0x79: "cp949",
    0x7A: "cp936",
    0x7B: "cp932",
    0x7C: "cp874",
    0x7D: "cp1255",
    0x7E: "cp1256",
    0x96: "mac_cyrillic",
    0x97: "mac_latin2",
    0x98: "mac_greek",
    0xC8: "cp1250",
    0xC9: "cp1251",
    0xCA: "cp1254",
    0xCB: "cp1253",
    0xCC: "cp1257",
}

DEFAULT_ENCODING = "cp1252"

# Field flag bits (VFP field descriptor byte 18)
FLAG_SYSTEM = 0x01
FLAG_NULLABLE = 0x02
FLAG_BINARY = 0x04
FLAG_AUTOINC = 0x0C

# Days between the Julian Day Number epoch and Python's proleptic ordinal.
_JDN_OFFSET = 1721425


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


class Field:
    """One column described by the table header."""

    __slots__ = (
        "name",
        "type",
        "offset",
        "length",
        "decimals",
        "flags",
        "autoinc_next",
        "autoinc_step",
        "null_bit",
        "varlen_bit",
    )

    def __init__(self, name, ftype, offset, length, decimals, flags, autoinc_next=0, autoinc_step=0):
        self.name: str = name
        self.type: str = ftype
        self.offset: int = offset
        self.length: int = length
        self.decimals: int = decimals
        self.flags: int = flags
        self.autoinc_next: int = autoinc_next
        self.autoinc_step: int = autoinc_step
        self.null_bit: Optional[int] = None
        self.varlen_bit: Optional[int] = None

    @property
    def type_name(self) -> str:
        return FIELD_TYPE_NAMES.get(self.type, "Unknown ({!r})".format(self.type))

    @property
    def is_system(self) -> bool:
        return bool(self.flags & FLAG_SYSTEM) or self.type == "0"

    @property
    def nullable(self) -> bool:
        return bool(self.flags & FLAG_NULLABLE)

    @property
    def binary(self) -> bool:
        # Autoincrement integers set 0x0C, which overlaps the binary bit.
        return bool(self.flags & FLAG_BINARY) and not self.autoinc

    @property
    def autoinc(self) -> bool:
        return (self.flags & FLAG_AUTOINC) == FLAG_AUTOINC and self.type == "I"

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "Field({!r}, {!r}, len={}, dec={})".format(self.name, self.type, self.length, self.decimals)


class Record:
    """A decoded row. ``values`` maps upper case field name to Python value."""

    __slots__ = ("index", "deleted", "values", "errors")

    def __init__(self, index: int, deleted: bool, values: Dict[str, Any], errors: List[str]):
        self.index = index
        self.deleted = deleted
        self.values = values
        self.errors = errors

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name.upper(), default)

    def text(self, name: str) -> str:
        """Return a field as text, never ``None``; binary memos become ''."""
        value = self.values.get(name.upper())
        if value is None or isinstance(value, (bytes, bytearray)):
            return ""
        return str(value)


# --------------------------------------------------------------------------
# Memo files
# --------------------------------------------------------------------------


class MemoFile:
    """Reader for FoxPro .fpt style memo files and dBase .dbt memo files."""

    def __init__(self, path: str):
        self.path = path
        self.warnings: List[str] = []
        try:
            self._fh: Optional[BinaryIO] = open(path, "rb")
        except OSError as exc:
            raise MemoError("cannot open memo file {}: {}".format(path, exc)) from exc
        self.size = os.fstat(self._fh.fileno()).st_size
        header = self._fh.read(512)
        if len(header) < 8:
            self.close()
            raise MemoError("memo file {} is too small ({} bytes)".format(path, self.size))

        self.kind = "dbt" if path.lower().endswith(".dbt") else "fpt"
        if self.kind == "fpt":
            block_size = struct.unpack(">H", header[6:8])[0]
            # SET BLOCKSIZE TO 0 stores memos with single byte granularity.
            self.block_size = block_size if block_size else 1
        else:
            block_size = struct.unpack("<H", header[20:22])[0] if len(header) >= 22 else 0
            self.block_size = block_size if block_size >= 64 else 512

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def read(self, block: int) -> Tuple[int, bytes]:
        """Return ``(memo_type, raw_bytes)`` for a block number.

        memo_type follows the FPT convention: 0 picture/binary, 1 text,
        2 OLE object. dBase memos always report 1.
        """
        if self._fh is None:
            raise MemoError("memo file is closed")
        if block <= 0:
            return 1, b""
        pos = block * self.block_size
        if pos >= self.size:
            raise MemoError("memo block {} points past end of {} ({} bytes)".format(block, os.path.basename(self.path), self.size))
        self._fh.seek(pos)
        if self.kind == "fpt":
            head = self._fh.read(8)
            if len(head) < 8:
                raise MemoError("truncated memo block header at block {}".format(block))
            mtype, length = struct.unpack(">II", head)
            available = self.size - pos - 8
            if length > available:
                self.warnings.append(
                    "memo block {} claims {} bytes but only {} remain; data truncated".format(block, length, available)
                )
                length = max(available, 0)
            return mtype, self._fh.read(length)
        return 1, self._read_dbt()

    def _read_dbt(self) -> bytes:
        assert self._fh is not None
        start = self._fh.tell()
        head = self._fh.read(8)
        if head[:4] == b"\xff\xff\x08\x00":
            length = struct.unpack("<I", head[4:8])[0]
            return self._fh.read(max(length - 8, 0))
        self._fh.seek(start)
        chunks = []
        total = 0
        while True:
            chunk = self._fh.read(512)
            if not chunk:
                break
            end = chunk.find(b"\x1a")
            if end != -1:
                chunks.append(chunk[:end])
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 64 * 1024 * 1024:  # runaway guard for corrupt files
                self.warnings.append("dBase memo without terminator truncated at 64 MB")
                break
        return b"".join(chunks)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def encoding_for_mark(mark: int) -> Optional[str]:
    return CODEPAGE_MARKS.get(mark)


def julian_to_date(jdn: int) -> Optional[_dt.date]:
    if jdn <= 0:
        return None
    try:
        return _dt.date.fromordinal(jdn - _JDN_OFFSET)
    except (ValueError, OverflowError):
        return None


class DbfTable:
    """Read only access to a DBF file and (optionally) its memo file.

    Use as a context manager so file handles are always released::

        with DbfTable("customer.dbf") as table:
            for record in table.records():
                ...
    """

    def __init__(
        self,
        path: str,
        encoding: Optional[str] = None,
        memo_ext: Optional[str] = None,
        memo_path: Optional[str] = None,
    ):
        self.path = path
        self.warnings: List[str] = []
        self.memo: Optional[MemoFile] = None
        self.memo_path: Optional[str] = None
        self.memo_missing = False
        self._fh: Optional[BinaryIO] = None
        try:
            self._fh = open(path, "rb")
        except OSError as exc:
            raise DbfFormatError("cannot open {}: {}".format(path, exc)) from exc
        try:
            self._read_header(encoding)
            self._open_memo(memo_ext, memo_path)
        except Exception:
            self.close()
            raise

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "DbfTable":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.memo is not None:
            self.memo.close()
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # -- header parsing ----------------------------------------------------

    def _read_header(self, encoding: Optional[str]) -> None:
        assert self._fh is not None
        self.file_size = os.fstat(self._fh.fileno()).st_size
        head = self._fh.read(32)
        if len(head) < 32:
            raise DbfFormatError("file is too small to be a table ({} bytes)".format(self.file_size))

        self.version = head[0]
        if self.version & 0x07 == 0x04:
            raise DbfFormatError("dBase 7 tables are not supported")
        if self.version not in VERSION_NAMES:
            # Unknown signature: keep going but tell the user. Many tools
            # write slightly odd version bytes and the rest is still valid.
            self.warnings.append("unrecognised table signature 0x{:02X}".format(self.version))

        yy, mm, dd = head[1], head[2], head[3]
        # Writers disagree: some store "year - 1900" (125 = 2025), others "year % 100".
        year = 1900 + yy if yy >= 80 else 2000 + yy
        try:
            self.last_update: Optional[_dt.date] = _dt.date(year, mm, dd) if mm and dd else None
        except ValueError:
            self.last_update = None

        self.header_record_count, self.header_length, self.record_length = struct.unpack("<IHH", head[4:12])
        self.table_flags = head[28]
        self.codepage_mark = head[29]

        if self.header_length < 33 or self.header_length > self.file_size:
            raise DbfFormatError(
                "invalid header length {} for a {} byte file".format(self.header_length, self.file_size)
            )

        if encoding:
            self.encoding = encoding
        else:
            mapped = encoding_for_mark(self.codepage_mark)
            if mapped is None and self.codepage_mark:
                self.warnings.append(
                    "unknown code page mark 0x{:02X}; decoding text as {}".format(self.codepage_mark, DEFAULT_ENCODING)
                )
            self.encoding = mapped or DEFAULT_ENCODING

        raw = self._fh.read(self.header_length - 32)
        self.fields: List[Field] = []
        offset = 1  # byte 0 of every record is the deletion flag
        pos = 0
        while pos + 32 <= len(raw) and raw[pos] != 0x0D:
            desc = raw[pos : pos + 32]
            pos += 32
            name = desc[0:11].split(b"\x00", 1)[0].decode("ascii", "replace").strip().upper()
            ftype = chr(desc[11]).upper() if desc[11] else "?"
            length = desc[16]
            decimals = desc[17]
            flags = desc[18]
            if ftype == "C" and self.version in (0x02, 0x03, 0x83, 0xFB, 0xF5) and decimals:
                # Clipper/Fox 2 style long character fields store the high byte in "decimals"
                length = length + decimals * 256
                decimals = 0
            autoinc_next, autoinc_step = struct.unpack("<IB", desc[19:24])
            if not name:
                name = "FIELD{}".format(len(self.fields) + 1)
            self.fields.append(Field(name, ftype, offset, length, decimals, flags, autoinc_next, autoinc_step))
            offset += length
        if not self.fields:
            raise DbfFormatError("table header declares no fields")
        if pos >= len(raw) or raw[pos] != 0x0D:
            self.warnings.append("field list terminator (0x0D) missing; header may be damaged")

        # VFP stores a 263 byte back link to the owning database container.
        self.backlink = ""
        if self.version in (0x30, 0x31, 0x32) and pos + 1 + 263 <= len(raw):
            link = raw[pos + 1 : pos + 1 + 263].split(b"\x00", 1)[0]
            self.backlink = link.decode(self.encoding, "replace").strip()

        computed = offset
        if self.record_length < computed:
            self.warnings.append(
                "header record length {} is smaller than the field total {}; using {}".format(
                    self.record_length, computed, computed
                )
            )
            self.record_length = computed
        elif self.record_length > computed:
            self.warnings.append(
                "header record length {} exceeds field total {} (extra bytes ignored)".format(self.record_length, computed)
            )

        data_bytes = max(self.file_size - self.header_length, 0)
        available = data_bytes // self.record_length if self.record_length else 0
        self.record_count = self.header_record_count
        if available < self.header_record_count:
            self.warnings.append(
                "header says {} records but the file only holds {}; reading what is present".format(
                    self.header_record_count, available
                )
            )
            self.record_count = available
        elif available > self.header_record_count + 1:
            # One spare record worth of bytes is normal (EOF marker 0x1A).
            self.warnings.append(
                "file holds space for {} records but header says {}; extra data ignored".format(
                    available, self.header_record_count
                )
            )

        self._assign_null_bits()

    def _assign_null_bits(self) -> None:
        bit = 0
        for f in self.fields:
            if f.type == "0":
                continue
            if f.type in ("V", "Q"):
                f.varlen_bit = bit
                bit += 1
            if f.nullable:
                f.null_bit = bit
                bit += 1
        self._nullflags_field = next((f for f in self.fields if f.type == "0"), None)

    def _open_memo(self, memo_ext: Optional[str], memo_path: Optional[str]) -> None:
        if not any(f.type in MEMO_TYPES for f in self.fields):
            return
        candidates: List[str] = []
        if memo_path:
            candidates.append(memo_path)
        else:
            exts = [memo_ext] if memo_ext else []
            exts += [".fpt", ".dbt"]
            for ext in exts:
                found = find_companion(self.path, ext)
                if found:
                    candidates.append(found)
                    break
        for candidate in candidates:
            try:
                self.memo = MemoFile(candidate)
                self.memo_path = candidate
                return
            except MemoError as exc:
                self.warnings.append(str(exc))
        self.memo_missing = True
        self.warnings.append("memo file not found; memo fields will be shown as unavailable")

    # -- properties --------------------------------------------------------

    @property
    def version_name(self) -> str:
        return VERSION_NAMES.get(self.version, "Unknown (0x{:02X})".format(self.version))

    @property
    def has_cdx(self) -> bool:
        return bool(self.table_flags & 0x01)

    @property
    def is_dbc(self) -> bool:
        return bool(self.table_flags & 0x04)

    @property
    def visible_fields(self) -> List[Field]:
        return [f for f in self.fields if not f.is_system]

    def field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def has_field(self, name: str) -> bool:
        return any(f.name == name.upper() for f in self.fields)

    # -- record access -----------------------------------------------------

    def records(self, include_deleted: bool = False, limit: Optional[int] = None) -> Iterator[Record]:
        """Yield decoded records in physical order.

        ``limit`` caps the number of records *yielded* (after deleted rows
        are filtered out). Decoding problems in individual fields never stop
        the iteration; they are attached to ``Record.errors``.
        """
        if self._fh is None:
            raise DbfFormatError("table is closed")
        self._fh.seek(self.header_length)
        yielded = 0
        for index in range(self.record_count):
            raw = self._fh.read(self.record_length)
            if len(raw) < self.record_length:
                self.warnings.append("record {} is truncated; stopping".format(index + 1))
                break
            flag = raw[0:1]
            deleted = flag == b"*"
            if flag not in (b" ", b"*"):
                if flag == b"\x1a":
                    self.warnings.append("end of file marker found at record {}; stopping".format(index + 1))
                    break
                self.warnings.append(
                    "record {} has an unexpected deletion flag 0x{:02X}".format(index + 1, raw[0])
                )
            if deleted and not include_deleted:
                continue
            values, errors = self._decode_record(raw)
            yield Record(index + 1, deleted, values, errors)
            yielded += 1
            if limit is not None and yielded >= limit:
                break

    def count_deleted(self) -> int:
        """Count deleted rows by scanning only the deletion flag byte."""
        if self._fh is None:
            raise DbfFormatError("table is closed")
        deleted = 0
        per_chunk = max(1, (1 << 20) // self.record_length)
        self._fh.seek(self.header_length)
        remaining = self.record_count
        while remaining > 0:
            n = min(per_chunk, remaining)
            chunk = self._fh.read(n * self.record_length)
            if not chunk:
                break
            deleted += chunk[:: self.record_length].count(b"*")
            remaining -= n
        return deleted

    def _decode_record(self, raw: bytes) -> Tuple[Dict[str, Any], List[str]]:
        values: Dict[str, Any] = {}
        errors: List[str] = []
        nullflags = 0
        if self._nullflags_field is not None:
            nf = self._nullflags_field
            nullflags = int.from_bytes(raw[nf.offset : nf.offset + nf.length], "little")
        for f in self.fields:
            if f.type == "0":
                continue
            chunk = raw[f.offset : f.offset + f.length]
            if f.null_bit is not None and nullflags >> f.null_bit & 1:
                values[f.name] = None
                continue
            try:
                values[f.name] = self._decode_value(f, chunk, nullflags)
            except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the row
                errors.append("{}: {}".format(f.name, exc))
                values[f.name] = "<unreadable: {}>".format(exc)
        return values, errors

    def _decode_text(self, data: bytes) -> str:
        return data.decode(self.encoding, "replace")

    def _decode_value(self, f: Field, chunk: bytes, nullflags: int) -> Any:
        t = f.type
        if t == "C":
            return self._decode_text(chunk).rstrip(" \x00")
        if t in ("N", "F"):
            text = chunk.decode("ascii", "replace").strip(" \x00")
            if not text:
                return None
            if set(text) == {"*"}:
                return text  # numeric overflow marker written by FoxPro
            try:
                number = Decimal(text)
            except InvalidOperation:
                return text
            return number if number.is_finite() else text
        if t == "D":
            text = chunk.decode("ascii", "replace").strip(" \x00")
            if not text or text == "00000000":
                return None
            try:
                return _dt.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return text
        if t in ("T", "@"):
            if len(chunk) < 8 or not chunk.strip(b" \x00"):
                return None
            jdn, millis = struct.unpack("<ii", chunk[:8])
            day = julian_to_date(jdn)
            if day is None:
                return None
            millis = max(0, min(millis, 86399999))
            return _dt.datetime.combine(day, _dt.time()) + _dt.timedelta(milliseconds=millis)
        if t == "L":
            c = chunk[:1].upper()
            if c in (b"T", b"Y"):
                return True
            if c in (b"F", b"N"):
                return False
            return None
        if t in ("I", "+"):
            if len(chunk) < 4:
                return None
            return struct.unpack("<i", chunk[:4])[0]
        if t == "Y":
            if len(chunk) < 8:
                return None
            return Decimal(struct.unpack("<q", chunk[:8])[0]) / Decimal(10000)
        if t == "B":
            if len(chunk) < 8:
                return None
            return struct.unpack("<d", chunk[:8])[0]
        if t in ("V", "Q"):
            data = chunk
            if f.varlen_bit is not None and nullflags >> f.varlen_bit & 1 and data:
                actual = data[-1]
                if actual < len(data):
                    data = data[:actual]
            if t == "Q" or f.binary:
                return bytes(data)
            return self._decode_text(data).rstrip("\x00")
        if t in MEMO_TYPES:
            return self._read_memo(f, chunk)
        return chunk.hex()

    def _read_memo(self, f: Field, chunk: bytes) -> Any:
        if len(chunk) == 4:
            block = struct.unpack("<I", chunk)[0]
        else:
            text = chunk.decode("ascii", "replace").strip(" \x00")
            block = int(text) if text.isdigit() else 0
        if block == 0:
            return ""
        if self.memo is None:
            return "<memo unavailable: memo file missing>"
        mtype, data = self.memo.read(block)
        if f.type in BINARY_MEMO_TYPES or f.binary:
            return bytes(data)
        if mtype != 1 and b"\x00" in data[:4096]:
            return bytes(data)
        return self._decode_text(data)

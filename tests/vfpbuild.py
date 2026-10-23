"""Builders that write small but structurally faithful VFP files for tests.

No VFP installation is needed: tables and memo files are produced byte by
byte following the documented formats.
"""

from __future__ import annotations

import datetime as dt
import os
import struct
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

FieldSpec = Tuple[str, str, int, int, int]  # name, type, length, decimals, flags


def _memo_block_writer(block_size: int = 64):
    blocks: List[bytes] = []
    state = {"next": max(1, 512 // block_size)}

    def add(data: bytes, mtype: int = 1) -> int:
        start = state["next"]
        payload = struct.pack(">II", mtype, len(data)) + data
        padded = payload + b"\x00" * ((-len(payload)) % block_size)
        blocks.append(padded)
        state["next"] += len(padded) // block_size
        return start

    def render() -> bytes:
        header = struct.pack(">I", state["next"]) + b"\x00\x00" + struct.pack(">H", block_size)
        header += b"\x00" * (512 - len(header))
        return header + b"".join(blocks)

    return add, render


def write_dbf(
    path: str,
    fields: Sequence[FieldSpec],
    records: Sequence[Dict[str, Any]],
    memo_ext: Optional[str] = ".fpt",
    version: int = 0x30,
    codepage: int = 0x03,
    table_flags: int = 0,
    deleted: Sequence[int] = (),
    encoding: str = "cp1252",
    header_count: Optional[int] = None,
) -> None:
    has_memo = any(f[1] in "MGPW" for f in fields)
    if has_memo:
        table_flags |= 0x02
    add_memo, render_memo = _memo_block_writer()

    nullable = [f for f in fields if f[4] & 0x02]
    varlen = [f for f in fields if f[1] in "VQ"]
    nbits = len(nullable) + len(varlen)
    all_fields = list(fields)
    if nbits:
        all_fields.append(("_NullFlags", "0", (nbits + 7) // 8, 0, 0x05))

    # bit assignment mirrors the reader: per field, varlen bit then null bit
    bits: Dict[str, Dict[str, int]] = {}
    b = 0
    for f in fields:
        bits[f[0]] = {}
        if f[1] in "VQ":
            bits[f[0]]["var"] = b
            b += 1
        if f[4] & 0x02:
            bits[f[0]]["null"] = b
            b += 1

    record_len = 1 + sum(f[2] for f in all_fields)
    descs = b""
    offset = 1
    for name, ftype, length, dec, flags in all_fields:
        d = name.encode("ascii")[:10].ljust(11, b"\x00") + ftype.encode("ascii")
        d += struct.pack("<I", offset) + bytes([length, dec, flags]) + b"\x00" * 13
        descs += d
        offset += length
    backlink = b"\x00" * 263 if version in (0x30, 0x31, 0x32) else b""
    header_len = 32 + len(descs) + 1 + len(backlink)
    today = dt.date(2024, 5, 17)
    count = len(records) if header_count is None else header_count
    head = bytes([version, today.year - 1900, today.month, today.day])
    head += struct.pack("<IHH", count, header_len, record_len)
    head += b"\x00" * 16 + bytes([table_flags, codepage]) + b"\x00\x00"

    rows: List[bytes] = []
    for i, rec in enumerate(records):
        row = bytearray(b"*" if i in deleted else b" ")
        nullflags = 0
        for name, ftype, length, dec, flags in fields:
            value = rec.get(name)
            if value is None and flags & 0x02:
                nullflags |= 1 << bits[name]["null"]
                row += b"\x00" * length
                continue
            row += _encode(value, ftype, length, dec, add_memo, encoding, bits[name], lambda bit: None)
            if ftype in "VQ" and value is not None:
                raw = value if isinstance(value, bytes) else value.encode(encoding)
                if len(raw) < length:
                    nullflags |= 1 << bits[name]["var"]
        if nbits:
            row += nullflags.to_bytes((nbits + 7) // 8, "little")
        assert len(row) == record_len, (len(row), record_len)
        rows.append(bytes(row))
    body = b"".join(rows)

    with open(path, "wb") as fh:
        fh.write(head + descs + b"\x0d" + backlink + body + b"\x1a")
    if has_memo and memo_ext:
        memo_path = os.path.splitext(path)[0] + memo_ext
        with open(memo_path, "wb") as fh:
            fh.write(render_memo())


def _encode(value, ftype, length, dec, add_memo, encoding, bitinfo, _setbit) -> bytes:
    if ftype == "C":
        return (value or "").encode(encoding)[:length].ljust(length, b" ")
    if ftype in "NF":
        if value is None:
            return b" " * length
        text = "{:.{}f}".format(Decimal(str(value)), dec) if dec else str(int(value))
        return text.rjust(length).encode("ascii")[:length]
    if ftype == "D":
        return value.strftime("%Y%m%d").encode("ascii") if value else b" " * 8
    if ftype == "T":
        if not value:
            return b"\x00" * 8
        jdn = value.date().toordinal() + 1721425
        ms = ((value.hour * 60 + value.minute) * 60 + value.second) * 1000 + value.microsecond // 1000
        return struct.pack("<ii", jdn, ms)
    if ftype == "L":
        return b"T" if value is True else b"F" if value is False else b"?"
    if ftype == "I":
        return struct.pack("<i", value or 0)
    if ftype == "Y":
        return struct.pack("<q", int(Decimal(str(value or 0)) * 10000))
    if ftype == "B":
        return struct.pack("<d", value or 0.0)
    if ftype in "VQ":
        raw = value if isinstance(value, bytes) else (value or "").encode(encoding)
        raw = raw[:length]
        if len(raw) < length:
            return raw + b"\x00" * (length - len(raw) - 1) + bytes([len(raw)])
        return raw
    if ftype in "MGPW":
        if value in (None, "", b""):
            return struct.pack("<I", 0)
        if isinstance(value, bytes):
            return struct.pack("<I", add_memo(value, 0 if ftype != "M" else 1))
        return struct.pack("<I", add_memo(value.replace("\n", "\r\n").encode(encoding)))
    raise ValueError(ftype)


# --------------------------------------------------------------------------
# VFP design file layouts
# --------------------------------------------------------------------------

SCX_FIELDS: List[FieldSpec] = [
    ("PLATFORM", "C", 8, 0, 0), ("UNIQUEID", "C", 10, 0, 0), ("TIMESTAMP", "N", 10, 0, 0),
    ("CLASS", "M", 4, 0, 0), ("CLASSLOC", "M", 4, 0, 0), ("BASECLASS", "M", 4, 0, 0),
    ("OBJNAME", "M", 4, 0, 0), ("PARENT", "M", 4, 0, 0), ("PROPERTIES", "M", 4, 0, 0),
    ("PROTECTED", "M", 4, 0, 0), ("METHODS", "M", 4, 0, 0), ("OBJCODE", "M", 4, 0, 0x04),
    ("OLE", "M", 4, 0, 0x04), ("OLE2", "M", 4, 0, 0),
] + [("RESERVED{}".format(i), "M", 4, 0, 0) for i in range(1, 9)] + [("USER", "M", 4, 0, 0)]


def obj(**kw) -> Dict[str, Any]:
    base = {"PLATFORM": "WINDOWS", "UNIQUEID": "_1", "TIMESTAMP": 0}
    base.update({k.upper(): v for k, v in kw.items()})
    return base


def comment_row(name: str = "Screen") -> Dict[str, Any]:
    return {"PLATFORM": "COMMENT", "UNIQUEID": name, "TIMESTAMP": 0}


FRX_FIELDS: List[FieldSpec] = [
    ("PLATFORM", "C", 8, 0, 0), ("UNIQUEID", "C", 10, 0, 0), ("TIMESTAMP", "N", 10, 0, 0),
    ("OBJTYPE", "N", 2, 0, 0), ("OBJCODE", "N", 3, 0, 0), ("NAME", "M", 4, 0, 0),
    ("EXPR", "M", 4, 0, 0), ("VPOS", "N", 9, 3, 0), ("HPOS", "N", 9, 3, 0),
    ("HEIGHT", "N", 9, 3, 0), ("WIDTH", "N", 9, 3, 0), ("STYLE", "M", 4, 0, 0),
    ("PICTURE", "M", 4, 0, 0), ("TAG", "M", 4, 0, 0), ("TAG2", "M", 4, 0, 0x04),
    ("FONTFACE", "M", 4, 0, 0), ("FONTSIZE", "N", 3, 0, 0), ("TOTALTYPE", "N", 2, 0, 0),
    ("RESETTOTAL", "N", 2, 0, 0), ("SUPEXPR", "M", 4, 0, 0), ("EJECTBEFOR", "L", 1, 0, 0),
]

MNX_FIELDS: List[FieldSpec] = [
    ("OBJTYPE", "N", 2, 0, 0), ("OBJCODE", "N", 2, 0, 0), ("NAME", "M", 4, 0, 0),
    ("PROMPT", "M", 4, 0, 0), ("COMMAND", "M", 4, 0, 0), ("MESSAGE", "M", 4, 0, 0),
    ("PROCTYPE", "N", 1, 0, 0), ("PROCEDURE", "M", 4, 0, 0), ("SETUPTYPE", "N", 1, 0, 0),
    ("SETUP", "M", 4, 0, 0), ("CLEANTYPE", "N", 1, 0, 0), ("CLEANUP", "M", 4, 0, 0),
    ("MARK", "C", 1, 0, 0), ("KEYNAME", "M", 4, 0, 0), ("KEYLABEL", "M", 4, 0, 0),
    ("SKIPFOR", "M", 4, 0, 0), ("NAMECHANGE", "L", 1, 0, 0), ("NUMITEMS", "N", 2, 0, 0),
    ("LEVELNAME", "C", 10, 0, 0), ("ITEMNUM", "C", 3, 0, 0), ("COMMENT", "M", 4, 0, 0),
    ("LOCATION", "N", 2, 0, 0), ("SCHEME", "N", 2, 0, 0),
]

PJX_FIELDS: List[FieldSpec] = [
    ("NAME", "M", 4, 0, 0), ("TYPE", "C", 1, 0, 0), ("ID", "N", 10, 0, 0),
    ("TIMESTAMP", "N", 10, 0, 0), ("OUTFILE", "M", 4, 0, 0), ("HOMEDIR", "M", 4, 0, 0),
    ("EXCLUDE", "L", 1, 0, 0), ("MAINPROG", "L", 1, 0, 0), ("DEVINFO", "M", 4, 0, 0x04),
    ("COMMENTS", "M", 4, 0, 0),
]

DBC_FIELDS: List[FieldSpec] = [
    ("OBJECTID", "I", 4, 0, 0), ("PARENTID", "I", 4, 0, 0), ("OBJECTTYPE", "C", 10, 0, 0),
    ("OBJECTNAME", "C", 128, 0, 0), ("PROPERTY", "M", 4, 0, 0x04), ("CODE", "M", 4, 0, 0x04),
    ("RIINFO", "C", 6, 0, 0), ("USER", "M", 4, 0, 0),
]


def dbc_props(*entries: Tuple[int, bytes]) -> bytes:
    out = b""
    for pid, value in entries:
        out += struct.pack("<ih", 7 + len(value), 1) + bytes([pid]) + value
    return out


def write_cdx(path: str, tags: Sequence[Tuple[str, str, str, int]]) -> None:
    """Write a minimal compound index: header + one leaf of tag names + tag headers."""
    page = 512
    name_size = 10
    first_tag = 1536
    tag_offsets = [first_tag + i * 1024 for i in range(len(tags))]
    header = bytearray(1024)
    struct.pack_into("<IIIHBB", header, 0, 1024, 0xFFFFFFFF, 0, name_size, 0x60, 1)
    node = bytearray(page)
    names = sorted(zip((t[0].upper() for t in tags), tag_offsets))
    struct.pack_into("<HHii", node, 0, 3, len(names), -1, -1)
    rec_bits, dup_bits, trail_bits, nbytes = 16, 4, 4, 3
    struct.pack_into("<HI", node, 12, 0, (1 << rec_bits) - 1)
    node[18] = (1 << dup_bits) - 1
    node[19] = (1 << trail_bits) - 1
    node[20], node[21], node[22], node[23] = rec_bits, dup_bits, trail_bits, nbytes
    end = page
    prev = b""
    for i, (name, off) in enumerate(names):
        raw = name.encode("ascii").ljust(name_size, b" ")
        stored_full = raw.rstrip(b" ")
        trail = name_size - len(stored_full)
        dup = 0
        while dup < len(stored_full) and dup < len(prev) and prev[dup] == stored_full[dup]:
            dup += 1
        data = stored_full[dup:]
        end -= len(data)
        node[end : end + len(data)] = data
        info = off | (dup << rec_bits) | (trail << (rec_bits + dup_bits))
        node[24 + i * nbytes : 24 + (i + 1) * nbytes] = info.to_bytes(nbytes, "little")
        prev = raw
    out = bytes(header) + bytes(node)
    for name, expr, for_expr, options in tags:
        th = bytearray(1024)
        struct.pack_into("<IIIHBB", th, 0, 0, 0xFFFFFFFF, 0, 10, options | (0x08 if for_expr else 0), 1)
        key = expr.encode("ascii") + b"\x00"
        fx = for_expr.encode("ascii") + b"\x00" if for_expr else b""
        struct.pack_into("<HHHHH", th, 502, 0, len(key), len(fx), 0, len(key))
        th[512 : 512 + len(key)] = key
        th[512 + len(key) : 512 + len(key) + len(fx)] = fx
        out += bytes(th)
    with open(path, "wb") as fh:
        fh.write(out)


def build_sample_app(root: str) -> None:
    """Create a miniature VFP application covering every supported file kind."""
    os.makedirs(os.path.join(root, "forms"), exist_ok=True)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    os.makedirs(os.path.join(root, "libs"), exist_ok=True)
    os.makedirs(os.path.join(root, "reports"), exist_ok=True)
    os.makedirs(os.path.join(root, "menus"), exist_ok=True)
    os.makedirs(os.path.join(root, "progs"), exist_ok=True)

    # Data table with every common field type plus a structural CDX
    write_dbf(
        os.path.join(root, "data", "customer.dbf"),
        [
            ("CUST_ID", "I", 4, 0, 0x0C),
            ("COMPANY", "C", 30, 0, 0),
            ("BALANCE", "N", 10, 2, 0),
            ("CREDIT", "Y", 8, 0, 0),
            ("SINCE", "D", 8, 0, 0),
            ("UPDATED", "T", 8, 0, 0),
            ("ACTIVE", "L", 1, 0, 0),
            ("NOTES", "M", 4, 0, 0),
            ("REGION", "C", 10, 0, 0x02),
            ("EMAIL", "V", 40, 0, 0),
        ],
        [
            {"CUST_ID": 1, "COMPANY": "Alfreds | Futterkiste", "BALANCE": "1234.50", "CREDIT": "5000",
             "SINCE": dt.date(1998, 3, 1), "UPDATED": dt.datetime(2004, 7, 9, 13, 45, 10), "ACTIVE": True,
             "NOTES": "Line one\nLine two with ``` fence", "REGION": None, "EMAIL": "a@x.de"},
            {"CUST_ID": 2, "COMPANY": "Ana Trujillo", "BALANCE": "-10.00", "CREDIT": "0",
             "SINCE": None, "UPDATED": None, "ACTIVE": False, "NOTES": "", "REGION": "MX", "EMAIL": ""},
            {"CUST_ID": 3, "COMPANY": "Deleted Co", "BALANCE": "0", "CREDIT": "0", "SINCE": None,
             "UPDATED": None, "ACTIVE": True, "NOTES": "", "REGION": "US", "EMAIL": "gone@x.com"},
        ],
        deleted=[2],
        table_flags=0x01,
    )
    write_cdx(os.path.join(root, "data", "customer.cdx"), [
        ("cust_id", "CUST_ID", "", 0x01),
        ("company", "UPPER(COMPANY)", "!DELETED()", 0x00),
    ])

    # Database container
    write_dbf(
        os.path.join(root, "data", "sales.dbc"),
        DBC_FIELDS,
        [
            {"OBJECTID": 1, "PARENTID": 1, "OBJECTTYPE": "Database", "OBJECTNAME": "Database"},
            {"OBJECTID": 2, "PARENTID": 1, "OBJECTTYPE": "Database", "OBJECTNAME": "StoredProceduresSource",
             "CODE": b"PROCEDURE NewId(tcTable)\r\n  RETURN 1\r\nENDPROC\r\n"},
            {"OBJECTID": 5, "PARENTID": 1, "OBJECTTYPE": "Table", "OBJECTNAME": "customer",
             "PROPERTY": dbc_props((1, b"customer.dbf\x00"), (84, b"Customer master\x00"))},
            {"OBJECTID": 6, "PARENTID": 5, "OBJECTTYPE": "Field", "OBJECTNAME": "cust_id",
             "PROPERTY": dbc_props((9, b"Customer number\x00"))},
            {"OBJECTID": 7, "PARENTID": 5, "OBJECTTYPE": "Index", "OBJECTNAME": "cust_id",
             "PROPERTY": dbc_props((2, b"\x01"))},
            {"OBJECTID": 8, "PARENTID": 1, "OBJECTTYPE": "View", "OBJECTNAME": "v_active",
             "PROPERTY": dbc_props((84, b"SELECT * FROM customer WHERE active\x00"))},
            {"OBJECTID": 9, "PARENTID": 5, "OBJECTTYPE": "Relation", "OBJECTNAME": "Relation 1",
             "PROPERTY": dbc_props((13, b"cust_id\x00"), (18, b"orders\x00")), "RIINFO": "CRI"},
        ],
        memo_ext=".dct",
        table_flags=0x04,
    )

    # Form
    write_dbf(
        os.path.join(root, "forms", "custedit.scx"),
        SCX_FIELDS,
        [
            comment_row(),
            obj(CLASS="dataenvironment", BASECLASS="dataenvironment", OBJNAME="Dataenvironment",
                PROPERTIES="Top = 0\nLeft = 0\nName = \"Dataenvironment\"\n"),
            obj(CLASS="cursor", BASECLASS="cursor", OBJNAME="Cursor1", PARENT="Dataenvironment",
                PROPERTIES="Alias = \"customer\"\nDatabase = ..\\data\\sales.dbc\nCursorSource = \"customer\"\nName = \"Cursor1\"\n"),
            obj(CLASS="form", BASECLASS="form", OBJNAME="frmCustomer",
                PROPERTIES="Height = 250\nWidth = 375\nCaption = \"Edit customer\"\nName = \"frmCustomer\"\n",
                METHODS="PROCEDURE Init\nLPARAMETERS tnId\nThisForm.txtCompany.Value = customer.company\nENDPROC\nPROCEDURE Unload\nRETURN .T.\nENDPROC\n",
                RESERVED3="*validatecustomer Checks the record before saving\nnmode Edit mode\n^aitems[1,0] Lookup items\n",
                PROTECTED="nmode\nvalidatecustomer^\n"),
            obj(CLASS="textbox", BASECLASS="textbox", OBJNAME="txtCompany", PARENT="frmCustomer",
                PROPERTIES="ControlSource = \"customer.company\"\nHeight = 23\nLeft = 12\nTop = 12\nWidth = 200\nName = \"txtCompany\"\n"),
            obj(CLASS="mybutton", CLASSLOC="..\\libs\\controls.vcx", BASECLASS="commandbutton", OBJNAME="cmdSave",
                PARENT="frmCustomer", PROPERTIES="Caption = \"\\<Save\"\nName = \"cmdSave\"\n",
                METHODS="PROCEDURE Click\nIF ThisForm.ValidateCustomer()\n  TABLEUPDATE(.T.)\nENDIF\nENDPROC\n"),
            comment_row("RESERVED"),
        ],
        memo_ext=".sct",
    )

    # Class library
    write_dbf(
        os.path.join(root, "libs", "controls.vcx"),
        SCX_FIELDS,
        [
            comment_row(),
            obj(CLASS="commandbutton", BASECLASS="commandbutton", OBJNAME="mybutton",
                PROPERTIES="Height = 27\nWidth = 84\nName = \"mybutton\"\n",
                METHODS="PROCEDURE Click\nWAIT WINDOW \"clicked\" NOWAIT\nENDPROC\n",
                RESERVED1="Class", RESERVED2="1", RESERVED7="Standard command button"),
            comment_row("RESERVED"),
            obj(CLASS="container", BASECLASS="container", OBJNAME="addressbox",
                PROPERTIES="Width = 300\nName = \"addressbox\"\n",
                METHODS="PROCEDURE txtStreet.Valid\nRETURN !EMPTY(This.Value)\nENDPROC\n",
                RESERVED1="Class", RESERVED2="2", RESERVED3="street Street line\n*refresh_address \n",
                RESERVED8="..\\include\\app.h"),
            obj(CLASS="textbox", BASECLASS="textbox", OBJNAME="txtStreet", PARENT="addressbox",
                PROPERTIES="Left = 5\nName = \"txtStreet\"\n"),
            comment_row("RESERVED"),
        ],
        memo_ext=".vct",
    )

    # Report
    write_dbf(
        os.path.join(root, "reports", "custlist.frx"),
        FRX_FIELDS,
        [
            {"PLATFORM": "WINDOWS", "OBJTYPE": 1, "OBJCODE": 53, "VPOS": 1, "EXPR": "ORIENTATION=1\nPAPERSIZE=9"},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 9, "OBJCODE": 1, "HEIGHT": 3000},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 9, "OBJCODE": 3, "HEIGHT": 2000, "EXPR": "customer.region"},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 9, "OBJCODE": 4, "HEIGHT": 2000},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 9, "OBJCODE": 5, "HEIGHT": 2000, "EXPR": "customer.region"},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 9, "OBJCODE": 7, "HEIGHT": 2000},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 5, "VPOS": 500, "HPOS": 100, "HEIGHT": 200, "WIDTH": 3000,
             "EXPR": "\"Customer list\"", "FONTFACE": "Arial", "FONTSIZE": 14},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 8, "VPOS": 9800, "HPOS": 100, "HEIGHT": 200, "WIDTH": 3000,
             "EXPR": "customer.company", "FONTFACE": "Arial", "FONTSIZE": 10},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 8, "VPOS": 9800, "HPOS": 4000, "HEIGHT": 200, "WIDTH": 1500,
             "EXPR": "customer.balance", "PICTURE": "\"999,999.99\"", "FONTFACE": "Arial", "FONTSIZE": 10},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 8, "VPOS": 12000, "HPOS": 4000, "HEIGHT": 200, "WIDTH": 1500,
             "EXPR": "customer.balance", "TOTALTYPE": 2, "RESETTOTAL": 6, "FONTFACE": "Arial", "FONTSIZE": 10},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 10, "EXPR": "customer.region", "EJECTBEFOR": True},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 18, "NAME": "nTotal", "EXPR": "customer.balance", "TAG": "0",
             "TOTALTYPE": 2, "RESETTOTAL": 1},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 25, "NAME": "dataenvironment",
             "TAG": "Name = \"Dataenvironment\"\n"},
            {"PLATFORM": "WINDOWS", "OBJTYPE": 26, "NAME": "cursor",
             "TAG": "Alias = \"customer\"\nCursorSource = \"customer\"\nName = \"Cursor1\"\n"},
        ],
        memo_ext=".frt",
    )

    # Menu
    write_dbf(
        os.path.join(root, "menus", "main.mnx"),
        MNX_FIELDS,
        [
            {"OBJTYPE": 1, "OBJCODE": 22, "SETUP": "PUBLIC gnUser", "CLEANUP": "RELEASE gnUser", "LOCATION": 0},
            {"OBJTYPE": 2, "OBJCODE": 1, "NAME": "_MSYSMENU", "NUMITEMS": 2, "LEVELNAME": "_MSYSMENU"},
            {"OBJTYPE": 3, "OBJCODE": 77, "PROMPT": "\\<File", "NAME": "File", "LEVELNAME": "_MSYSMENU",
             "KEYNAME": "ALT+F", "KEYLABEL": "Alt+F"},
            {"OBJTYPE": 2, "OBJCODE": 0, "NAME": "File", "NUMITEMS": 3, "LEVELNAME": "File"},
            {"OBJTYPE": 3, "OBJCODE": 67, "PROMPT": "\\<Customers...", "COMMAND": "DO FORM custedit",
             "LEVELNAME": "File", "MESSAGE": "\"Edit customers\""},
            {"OBJTYPE": 3, "OBJCODE": 67, "PROMPT": "\\-", "LEVELNAME": "File"},
            {"OBJTYPE": 3, "OBJCODE": 80, "PROMPT": "E\\<xit", "LEVELNAME": "File",
             "PROCEDURE": "CLEAR EVENTS\nQUIT", "SKIPFOR": "gnUser = 0"},
            {"OBJTYPE": 3, "OBJCODE": 78, "PROMPT": "\\<Copy", "NAME": "_MED_COPY", "LEVELNAME": "_MSYSMENU"},
        ],
        memo_ext=".mnt",
    )

    # Project
    write_dbf(
        os.path.join(root, "sales.pjx"),
        PJX_FIELDS,
        [
            {"NAME": "c:\\apps\\sales\\sales.pjx\x00", "TYPE": "H", "HOMEDIR": "c:\\apps\\sales\x00",
             "OUTFILE": "sales.exe\x00", "DEVINFO": b"John Doe\x00\x00Acme Corp\x00"},
            {"NAME": "progs\\main.prg\x00", "TYPE": "P", "MAINPROG": True},
            {"NAME": "forms\\custedit.scx\x00", "TYPE": "K"},
            {"NAME": "libs\\controls.vcx\x00", "TYPE": "V"},
            {"NAME": "reports\\custlist.frx\x00", "TYPE": "R"},
            {"NAME": "menus\\main.mnx\x00", "TYPE": "M"},
            {"NAME": "data\\sales.dbc\x00", "TYPE": "d"},
            {"NAME": "config.fpw\x00", "TYPE": "T", "EXCLUDE": True, "COMMENTS": "runtime settings"},
        ],
        memo_ext=".pjt",
    )

    with open(os.path.join(root, "progs", "main.prg"), "wb") as fh:
        fh.write(
            "* Main program\r\nSET DELETED ON\r\nDO menus\\main.mpr\r\nREAD EVENTS\r\n\r\n"
            "FUNCTION FormatName(tcName)\r\n  RETURN PROPER(tcName) && caf\xe9\r\nENDFUNC\r\n".encode("cp1252")
        )
    with open(os.path.join(root, "config.fpw"), "w", encoding="ascii") as fh:
        fh.write("SCREEN=OFF\nRESOURCE=OFF\n")
    with open(os.path.join(root, "progs", "main.fxp"), "wb") as fh:
        fh.write(b"\xfe\xf2\xff\x00compiled")

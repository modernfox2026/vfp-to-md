"""End to end and unit tests. Run with:  python -m unittest discover -s tests"""

from __future__ import annotations

import builtins
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vfpbuild  # noqa: E402
from vfp2md import cli  # noqa: E402
from vfp2md.cdx import read_cdx  # noqa: E402
from vfp2md.converters import Options  # noqa: E402
from vfp2md.converters.base import parse_properties, split_procedures  # noqa: E402
from vfp2md.dbf import DbfTable  # noqa: E402
from vfp2md.discovery import SelectionError, parse_selection, scan  # noqa: E402
from vfp2md.engine import STATUS_FAILED, STATUS_OK, STATUS_SKIPPED, STATUS_WARN, convert_all  # noqa: E402
from vfp2md.errors import DbfFormatError  # noqa: E402
from vfp2md.markdown import MarkdownDoc, cell, inline_code  # noqa: E402


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="vfp2md-test-")
        self.app = os.path.join(self.tmp, "app")
        self.out = os.path.join(self.tmp, "out")
        vfpbuild.build_sample_app(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def convert(self, select: str = "all", **opts):
        result = scan(self.app)
        items = parse_selection(select, result.files)
        return convert_all(items, self.out, Options(**opts), source_root=self.app)

    def read_out(self, rel: str) -> str:
        with open(os.path.join(self.out, rel), encoding="utf-8") as fh:
            return fh.read()


class DbfReaderTests(TempDirTest):
    def test_field_types_decode(self):
        with DbfTable(os.path.join(self.app, "data", "customer.dbf")) as t:
            recs = list(t.records(include_deleted=True))
            self.assertEqual(len(recs), 3)
            first = recs[0]
            self.assertEqual(first.get("CUST_ID"), 1)
            self.assertEqual(first.get("BALANCE"), Decimal("1234.50"))
            self.assertEqual(first.get("CREDIT"), Decimal("5000"))
            self.assertEqual(first.get("SINCE"), dt.date(1998, 3, 1))
            self.assertEqual(first.get("UPDATED"), dt.datetime(2004, 7, 9, 13, 45, 10))
            self.assertIs(first.get("ACTIVE"), True)
            self.assertIn("Line two", first.get("NOTES"))
            self.assertIsNone(first.get("REGION"))  # nullable field holding .NULL.
            self.assertEqual(first.get("EMAIL"), "a@x.de")  # varchar trimmed via length byte
            self.assertTrue(recs[2].deleted)
            self.assertEqual(t.count_deleted(), 1)
            self.assertEqual(t.warnings, [])

    def test_header_record_count_too_large_is_tolerated(self):
        path = os.path.join(self.tmp, "bad.dbf")
        vfpbuild.write_dbf(path, [("A", "C", 5, 0, 0)], [{"A": "x"}, {"A": "y"}], header_count=50)
        with DbfTable(path) as t:
            self.assertEqual([r.get("A") for r in t.records()], ["x", "y"])
            self.assertTrue(any("header says 50" in w for w in t.warnings))

    def test_garbage_file_raises_format_error(self):
        path = os.path.join(self.tmp, "junk.dbf")
        with open(path, "wb") as fh:
            fh.write(b"this is not a table at all")
        with self.assertRaises(DbfFormatError):
            DbfTable(path)

    def test_missing_memo_file_degrades(self):
        os.remove(os.path.join(self.app, "data", "customer.fpt"))
        with DbfTable(os.path.join(self.app, "data", "customer.dbf")) as t:
            self.assertTrue(t.memo_missing)
            rec = next(t.records())
            self.assertIn("memo unavailable", rec.get("NOTES"))

    def test_truncated_memo_block_is_reported_per_cell(self):
        fpt = os.path.join(self.app, "data", "customer.fpt")
        with open(fpt, "r+b") as fh:
            fh.truncate(520)
        with DbfTable(os.path.join(self.app, "data", "customer.dbf")) as t:
            rec = next(t.records())
            self.assertIsInstance(rec.get("NOTES"), str)
            self.assertEqual(rec.get("COMPANY"), "Alfreds | Futterkiste")

    def test_encoding_override(self):
        path = os.path.join(self.tmp, "cyr.dbf")
        vfpbuild.write_dbf(path, [("NAME", "C", 10, 0, 0)], [{"NAME": "Привет"}], codepage=0xC9, encoding="cp1251")
        with DbfTable(path) as t:
            self.assertEqual(t.encoding, "cp1251")
            self.assertEqual(next(t.records()).get("NAME"), "Привет")


class CdxTests(TempDirTest):
    def test_reads_tags(self):
        tags = read_cdx(os.path.join(self.app, "data", "customer.cdx"))
        by_name = {t.name: t for t in tags}
        self.assertEqual(by_name["CUST_ID"].expression, "CUST_ID")
        self.assertTrue(by_name["CUST_ID"].unique)
        self.assertEqual(by_name["COMPANY"].for_expression, "!DELETED()")


class MarkdownTests(unittest.TestCase):
    def test_cell_escaping(self):
        self.assertEqual(cell("a|b\nc"), "a\\|b<br>c")
        self.assertEqual(cell("c:\\dir\\f"), "c:\\dir\\f")
        self.assertEqual(cell(None), ".NULL.")
        self.assertEqual(cell("x" * 20, limit=5), "xxxxx ...[+15 chars]")

    def test_code_fence_grows_past_embedded_backticks(self):
        out = MarkdownDoc().code("a = ```\nb", "foxpro").render()
        self.assertTrue(out.startswith("````foxpro\n"))
        self.assertTrue(out.rstrip().endswith("````"))

    def test_inline_code_with_backticks(self):
        self.assertEqual(inline_code("a`b"), "``a`b``")

    def test_control_characters_removed(self):
        out = MarkdownDoc().para("bad\x00\x07text\r\nnext").render()
        self.assertEqual(out, "badtext\nnext\n")


class ParsingTests(unittest.TestCase):
    def test_split_procedures(self):
        code = "* header\nPROCEDURE A\nx=1\nENDPROC\nPROCEDURE B\ny=2\n"
        pre, procs = split_procedures(code)
        self.assertEqual(pre, "* header")
        self.assertEqual([n for n, _ in procs], ["A", "B"])  # B has no ENDPROC

    def test_parse_properties_with_continuation(self):
        props = parse_properties("Caption = \"x\"\r\nComment = line1\r\nline2\r\nName = \"y\"")
        self.assertEqual(props[1], ("Comment", "line1\nline2"))
        self.assertEqual(len(props), 3)


class SelectionTests(TempDirTest):
    def test_selection_expressions(self):
        files = scan(self.app).files
        self.assertEqual(len(parse_selection("all", files)), len(files))
        self.assertEqual([f.ext for f in parse_selection(".scx", files)], [".scx"])
        self.assertEqual(len(parse_selection("1-3", files)), 3)
        self.assertNotIn(".dbf", [f.ext for f in parse_selection("all !table", files)])
        self.assertEqual(len(parse_selection("!1", files)), len(files) - 1)
        self.assertEqual([f.ext for f in parse_selection("*cust*", files)], [".scx", ".frx", ".dbf"])
        for bad in ("999", "0", ".xyz", "nomatch*", "", "!"):
            with self.assertRaises(SelectionError):
                parse_selection(bad, files)

    def test_scan_groups_companions(self):
        result = scan(self.app)
        scx = next(f for f in result.files if f.ext == ".scx")
        self.assertIn(".sct", scx.companions)
        self.assertEqual(len(result.skipped_compiled), 1)
        self.assertFalse(any(f.ext == ".sct" for f in result.files))

    def test_case_insensitive_companions(self):
        os.rename(os.path.join(self.app, "forms", "custedit.sct"), os.path.join(self.app, "forms", "CUSTEDIT.SCT"))
        scx = next(f for f in scan(self.app).files if f.ext == ".scx")
        self.assertIn(".sct", scx.companions)

    def test_orphan_companion_reported(self):
        os.remove(os.path.join(self.app, "menus", "main.mnx"))
        result = scan(self.app)
        self.assertIn(os.path.join("menus", "main.mnt"), result.orphans)


class ConversionTests(TempDirTest):
    def test_full_conversion(self):
        outcomes = self.convert()
        self.assertTrue(all(o.status == STATUS_OK for o in outcomes), [(o.item.relpath, o.warnings, o.error) for o in outcomes])
        form = self.read_out("forms/custedit.scx.md")
        self.assertIn("ThisForm.txtCompany.Value = customer.company", form)
        self.assertIn("| customer | customer | ..\\data\\sales.dbc |", form)
        self.assertIn("`cmdSave` (commandbutton, class mybutton from controls.vcx)", form)
        vcx = self.read_out("libs/controls.vcx.md")
        self.assertIn("## Class addressbox", vcx)
        self.assertIn("PROCEDURE txtStreet.Valid", vcx)
        report = self.read_out("reports/custlist.frx.md")
        self.assertIn("| nTotal | customer.balance | 0 | Sum | End of report |", report)
        menu = self.read_out("menus/main.mnx.md")
        self.assertIn("  - **Customers...**: command `DO FORM custedit`", menu)
        project = self.read_out("sales.pjx.md")
        self.assertIn("**Main file:** progs\\main.prg", project)
        dbc = self.read_out("data/sales.dbc.md")
        self.assertIn("SELECT * FROM customer WHERE active", dbc)
        self.assertIn("update cascade, delete restrict, insert ignore", dbc)
        table = self.read_out("data/customer.dbf.md")
        self.assertIn("Alfreds \\| Futterkiste", table)
        self.assertNotIn("Deleted Co", table)
        prg = self.read_out("progs/main.prg.md")
        self.assertIn("café", prg)
        index = self.read_out("INDEX.md")
        self.assertIn("[forms/custedit.scx.md](forms/custedit.scx.md)", index)

    def test_include_deleted_and_row_limit(self):
        self.convert("table", include_deleted=True, max_rows=0)
        self.assertIn("Deleted Co", self.read_out("data/customer.dbf.md"))
        self.convert("table", max_rows=1)
        self.assertIn("Showing 1 of 2 record(s)", self.read_out("data/customer.dbf.md"))

    def test_corrupt_file_produces_failure_document_and_continues(self):
        with open(os.path.join(self.app, "forms", "broken.scx"), "wb") as fh:
            fh.write(b"\x30garbage")
        outcomes = self.convert()
        broken = next(o for o in outcomes if o.item.relpath.endswith("broken.scx"))
        self.assertEqual(broken.status, STATUS_FAILED)
        self.assertIn("Conversion failed", self.read_out("forms/broken.scx.md"))
        self.assertEqual(sum(1 for o in outcomes if o.status == STATUS_OK), len(outcomes) - 1)
        self.assertIn("ERROR:", self.read_out("INDEX.md"))

    def test_missing_memo_companion_gives_warning(self):
        os.remove(os.path.join(self.app, "forms", "custedit.sct"))
        outcome = self.convert(".scx")[0]
        self.assertEqual(outcome.status, STATUS_WARN)
        self.assertTrue(any(".sct" in w for w in outcome.warnings))

    def test_binary_program_is_not_dumped(self):
        with open(os.path.join(self.app, "progs", "enc.prg"), "wb") as fh:
            fh.write(b"\x00\x01\x02" * 400)
        outcome = self.convert("*enc*")[0]
        self.assertEqual(outcome.status, STATUS_WARN)

    def test_conflict_skip(self):
        self.convert(".prg")
        outcome = convert_all(scan(self.app).files[-2:-1], self.out, Options(), on_conflict="skip")[0]
        self.assertEqual(outcome.status, STATUS_SKIPPED)

    def test_conflict_rename(self):
        self.convert(".prg")
        files = parse_selection(".prg", scan(self.app).files)
        outcome = convert_all(files, self.out, Options(), on_conflict="rename")[0]
        self.assertTrue(outcome.output.endswith("main.prg_2.md"))


class CliTests(TempDirTest):
    def run_cli(self, argv, inputs=None, tty=False):
        buf = io.StringIO()
        answers = iter(inputs or [])

        def fake_input(prompt=""):
            buf.write(prompt)
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        with mock.patch.object(builtins, "input", fake_input), \
                mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_non_interactive(self):
        code, out = self.run_cli([self.app, "-o", self.out, "-s", "form report", "--yes", "--no-color"])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.out, "forms", "custedit.scx.md")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "data", "customer.dbf.md")))

    def test_errors_are_reported_cleanly(self):
        code, _ = self.run_cli([os.path.join(self.tmp, "nope"), "-o", self.out, "--yes"])
        self.assertEqual(code, cli.EXIT_USAGE)
        code, _ = self.run_cli([self.app, "-o", self.out, "-s", "999", "--yes"])
        self.assertEqual(code, cli.EXIT_USAGE)
        code, _ = self.run_cli([self.app, "--yes"])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_interactive_flow_with_retries(self):
        inputs = [
            os.path.join(self.tmp, "missing"),  # bad folder, asked again
            '"{}"'.format(self.app),              # quoted path is accepted
            "y",                                  # include sub folders
            "help",                               # show syntax
            "bogus*",                             # no match, asked again
            ".scx .mnx",                          # selection
            "y",                                  # confirm selection
            self.out,                             # output folder
            "y",                                  # create it
            "y",                                  # start conversion
        ]
        code, out = self.run_cli(["--no-color"], inputs, tty=True)
        self.assertEqual(code, 0, out)
        self.assertIn("folder does not exist", out)
        self.assertIn("nothing matches", out)
        self.assertTrue(os.path.exists(os.path.join(self.out, "menus", "main.mnx.md")))

    def test_interactive_quit(self):
        code, out = self.run_cli(["--no-color"], ["q"], tty=True)
        self.assertEqual(code, cli.EXIT_INTERRUPTED)
        self.assertIn("Cancelled", out)


if __name__ == "__main__":
    unittest.main()

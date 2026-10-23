"""Interactive and scriptable command line front end.

Interactive use (prompts for everything)::

    python -m vfp2md

Scripted use (no prompts)::

    python -m vfp2md C:\\legacy\\app -o docs\\app -s "all !table" --yes
"""

from __future__ import annotations

import argparse
import codecs
import os
import sys
import tempfile
from typing import List, Optional

from . import __version__
from .converters import Options
from .discovery import KINDS, SELECTION_HELP, DiscoveredFile, ScanResult, SelectionError, parse_selection, scan
from .engine import (
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CONFLICT_SKIP,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WARN,
    INDEX_NAME,
    Outcome,
    convert_all,
    output_relpath,
)
from .fsutil import human_size

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class Cancelled(Exception):
    """User chose to quit (typed q, or closed the input stream)."""


# --------------------------------------------------------------------------
# Terminal output
# --------------------------------------------------------------------------


class Term:
    def __init__(self, color: bool):
        self.color = color

    def _wrap(self, code: str, text: str) -> str:
        return "\033[{}m{}\033[0m".format(code, text) if self.color else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)


def _supports_color(disabled: bool) -> bool:
    if disabled or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # enables ANSI escape processing on Windows 10+ consoles
    return True


def _configure_stdout() -> None:
    """Never crash on characters the console code page cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _enable_path_completion() -> None:
    """Tab completion of paths at prompts, where readline is available."""
    try:
        import glob
        import readline
    except ImportError:
        return

    def complete(text: str, state: int):
        expanded = os.path.expanduser(os.path.expandvars(text))
        matches = [m + (os.sep if os.path.isdir(m) else "") for m in glob.glob(expanded + "*")]
        return matches[state] if state < len(matches) else None

    readline.set_completer_delims(" \t\n;")
    readline.set_completer(complete)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = " [{}]".format(default) if default else ""
    try:
        answer = input("{}{}: ".format(prompt, suffix)).strip()
    except EOFError:
        raise Cancelled()
    if answer.lower() in ("q", "quit", "exit"):
        raise Cancelled()
    return answer or (default or "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask("{} ({})".format(prompt, hint)).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n (or q to quit).")


def clean_path_input(text: str) -> str:
    """Accept pasted/dragged paths: strip quotes, expand ~ and env vars."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if text.startswith("& '") and text.endswith("'"):  # PowerShell drag and drop
        text = text[3:-1]
    return os.path.abspath(os.path.expanduser(os.path.expandvars(text)))


def check_source_dir(path: str) -> Optional[str]:
    """Return an error message if ``path`` cannot be used as the source folder."""
    if not os.path.exists(path):
        return "folder does not exist: {}".format(path)
    if not os.path.isdir(path):
        return "not a folder: {}".format(path)
    if not os.access(path, os.R_OK | os.X_OK):
        return "folder is not readable (permission denied): {}".format(path)
    return None


def check_output_dir(path: str) -> Optional[str]:
    """Create ``path`` if needed and prove it is writable. Returns an error or None."""
    if os.path.exists(path) and not os.path.isdir(path):
        return "a file with that name already exists: {}".format(path)
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".vfp2md-probe-", dir=path)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return "cannot write to {}: {}".format(path, exc.strerror or exc)
    return None


# --------------------------------------------------------------------------
# Listing and selection
# --------------------------------------------------------------------------


def print_listing(term: Term, result: ScanResult) -> None:
    files = result.files
    width = len(str(len(files)))
    current = None
    for i, f in enumerate(files, 1):
        if f.kind.key != current:
            current = f.kind.key
            count = sum(1 for x in files if x.kind.key == current)
            print()
            print(term.bold("{} ({})".format(f.kind.label, count)) + term.dim("   select with: {}".format(f.kind.key)))
        comps = ""
        if f.companions:
            comps = term.dim(" + " + ", ".join(sorted(f.companions)))
        missing = ""
        if f.missing_required:
            missing = term.yellow("  (missing {})".format(f.missing_required))
        print("  {}  {}{}  {}{}".format(
            term.cyan(str(i).rjust(width)), f.relpath, comps, term.dim(human_size(f.size)), missing))
    print()
    if result.orphans:
        print(term.yellow("Companion files without their main file (cannot be converted alone): {}".format(
            ", ".join(result.orphans[:10]) + (" ..." if len(result.orphans) > 10 else ""))))
    if result.skipped_compiled:
        print(term.dim("Compiled/binary files ignored: {}".format(len(result.skipped_compiled))))
    for err in result.errors:
        print(term.yellow("Warning: " + err))


def choose_files(term: Term, result: ScanResult) -> List[DiscoveredFile]:
    print_listing(term, result)
    print(term.dim("Type 'help' for selection syntax, 'list' to show the files again, 'q' to quit."))
    while True:
        expr = ask("Select files to convert", "all")
        low = expr.lower()
        if low in ("help", "?", "h"):
            print(SELECTION_HELP)
            continue
        if low in ("list", "ls", "l"):
            print_listing(term, result)
            continue
        try:
            chosen = parse_selection(expr, result.files)
        except SelectionError as exc:
            print(term.red("  {}".format(exc)) + "  (type 'help' for the syntax)")
            continue
        if not chosen:
            print(term.yellow("  That selection is empty. Try again."))
            continue
        print()
        print("Selected {} file(s):".format(len(chosen)))
        for f in chosen[:25]:
            print("  - {}".format(f.relpath))
        if len(chosen) > 25:
            print("  ... and {} more".format(len(chosen) - 25))
        if ask_yes_no("Use this selection?", True):
            return chosen


def ask_source(term: Term, initial: Optional[str]) -> str:
    candidate = initial
    while True:
        if not candidate:
            candidate = ask("Folder containing the VFP files", os.getcwd())
        path = clean_path_input(candidate)
        error = check_source_dir(path)
        if error is None:
            return path
        print(term.red("  " + error))
        candidate = None


def ask_output(term: Term, initial: Optional[str], source: str) -> str:
    default = os.path.join(os.path.dirname(source.rstrip(os.sep)) or source, os.path.basename(source.rstrip(os.sep)) + "_markdown")
    candidate = initial
    while True:
        if not candidate:
            candidate = ask("Folder to save the Markdown files in", default)
        path = clean_path_input(candidate)
        if not os.path.exists(path):
            if not ask_yes_no("{} does not exist. Create it?".format(path), True):
                candidate = None
                continue
        error = check_output_dir(path)
        if error is None:
            return path
        print(term.red("  " + error))
        candidate = None


def ask_conflict(term: Term, existing: int) -> str:
    print(term.yellow("{} output file(s) already exist in that folder.".format(existing)))
    while True:
        answer = ask("Overwrite them, skip them, or keep both (o/s/k)", "o").lower()
        if answer in ("o", "overwrite"):
            return CONFLICT_OVERWRITE
        if answer in ("s", "skip"):
            return CONFLICT_SKIP
        if answer in ("k", "keep", "rename", "r"):
            return CONFLICT_RENAME
        print("  Please type o, s or k.")


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def make_progress(term: Term, quiet: bool):
    def progress(n: int, total: int, item: DiscoveredFile, outcome: Optional[Outcome]) -> None:
        if quiet:
            return
        prefix = "[{}/{}]".format(str(n).rjust(len(str(total))), total)
        if outcome is None:
            sys.stdout.write("{} {} ... ".format(term.dim(prefix), item.relpath))
            sys.stdout.flush()
            return
        if outcome.status == STATUS_OK:
            status = term.green("ok")
        elif outcome.status == STATUS_WARN:
            status = term.yellow("ok, {} note(s)".format(len(outcome.warnings)))
        elif outcome.status == STATUS_SKIPPED:
            status = term.dim("skipped (exists)")
        else:
            status = term.red("FAILED: {}".format(outcome.error))
        print(status)

    return progress


def print_summary(term: Term, outcomes: List[Outcome], out_dir: str) -> None:
    def n(status: str) -> int:
        return sum(1 for o in outcomes if o.status == status)

    print()
    print(term.bold("Done."))
    print("  Converted cleanly : {}".format(term.green(str(n(STATUS_OK)))))
    print("  With notes        : {}".format(term.yellow(str(n(STATUS_WARN))) if n(STATUS_WARN) else "0"))
    print("  Failed            : {}".format(term.red(str(n(STATUS_FAILED))) if n(STATUS_FAILED) else "0"))
    if n(STATUS_SKIPPED):
        print("  Skipped           : {}".format(n(STATUS_SKIPPED)))
    print("  Output folder     : {}".format(out_dir))
    print("  Index             : {}".format(os.path.join(out_dir, INDEX_NAME)))
    if n(STATUS_WARN) or n(STATUS_FAILED):
        print(term.dim("  Details of every note and failure are listed in the index's conversion log."))


def build_parser() -> argparse.ArgumentParser:
    kinds = ", ".join(k.key for k in KINDS)
    p = argparse.ArgumentParser(
        prog="vfp2md",
        description="Convert Visual FoxPro files (forms, class libraries, reports, menus, projects, "
                    "databases, tables and programs) into Markdown that people and AI tools can read. "
                    "Run without arguments for interactive mode.",
        epilog=SELECTION_HELP + "\n\nFile kinds: " + kinds,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", nargs="?", help="folder containing the VFP files (prompted if omitted)")
    p.add_argument("-o", "--output", help="folder to write Markdown into (prompted if omitted)")
    p.add_argument("-s", "--select", help="selection expression, e.g. 'all' or '1-5 .scx' (prompted if omitted)")
    p.add_argument("-y", "--yes", action="store_true", help="never prompt; use defaults for anything not given")
    p.add_argument("-l", "--list", action="store_true", help="only list the convertible files and exit")
    p.add_argument("--no-recursive", action="store_true", help="do not look inside sub folders")
    p.add_argument("--include-hidden", action="store_true", help="include hidden files and folders")
    p.add_argument("--encoding", help="force a text encoding such as cp1252, cp1250, cp437 (default: per table code page)")
    p.add_argument("--max-rows", type=int, default=100, metavar="N", help="data rows to include per table (0 = all, default 100)")
    p.add_argument("--max-cell", type=int, default=300, metavar="N", help="truncate table cells longer than N characters (0 = never, default 300)")
    p.add_argument("--include-deleted", action="store_true", help="include records marked as deleted")
    p.add_argument("--on-conflict", choices=[CONFLICT_OVERWRITE, CONFLICT_SKIP, CONFLICT_RENAME],
                   help="what to do when an output file already exists (default: ask, or overwrite with --yes)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    p.add_argument("--no-color", action="store_true", help="disable coloured output")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return p


def validate_args(args: argparse.Namespace) -> Optional[str]:
    if args.encoding:
        try:
            codecs.lookup(args.encoding)
        except LookupError:
            return "unknown encoding: {}".format(args.encoding)
    if args.max_rows < 0:
        return "--max-rows cannot be negative"
    if args.max_cell < 0:
        return "--max-cell cannot be negative"
    return None


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    term = Term(_supports_color(args.no_color))
    error = validate_args(args)
    if error:
        parser.error(error)

    interactive = not args.yes and sys.stdin.isatty()
    if not interactive and not args.source:
        print(term.red("error: a source folder is required when not running interactively"), file=sys.stderr)
        return EXIT_USAGE

    try:
        if interactive:
            _enable_path_completion()
            print(term.bold("VFP to Markdown converter {}".format(__version__)))
            print(term.dim("Type q at any prompt to quit. Tab completes paths where supported."))
            print()
            source = ask_source(term, args.source)
        else:
            source = clean_path_input(args.source)
            problem = check_source_dir(source)
            if problem:
                print(term.red("error: " + problem), file=sys.stderr)
                return EXIT_USAGE

        recursive = not args.no_recursive
        if interactive and not args.no_recursive and args.source is None:
            recursive = ask_yes_no("Include sub folders?", True)

        print("Scanning {} ...".format(source))
        result = scan(source, recursive=recursive, include_hidden=args.include_hidden)
        if not result.files:
            print(term.yellow("No convertible VFP files were found in {}.".format(source)))
            for err in result.errors:
                print(term.yellow("Warning: " + err))
            return EXIT_OK if args.list else EXIT_USAGE

        if args.list:
            print_listing(term, result)
            return EXIT_OK

        if args.select:
            try:
                chosen = parse_selection(args.select, result.files)
            except SelectionError as exc:
                print(term.red("error: invalid selection: {}".format(exc)), file=sys.stderr)
                return EXIT_USAGE
            if not chosen:
                print(term.red("error: the selection matched no files"), file=sys.stderr)
                return EXIT_USAGE
        elif interactive:
            chosen = choose_files(term, result)
        else:
            chosen = list(result.files)

        if interactive:
            out_dir = ask_output(term, args.output, source)
        else:
            if not args.output:
                print(term.red("error: --output is required when not running interactively"), file=sys.stderr)
                return EXIT_USAGE
            out_dir = clean_path_input(args.output)
            problem = check_output_dir(out_dir)
            if problem:
                print(term.red("error: " + problem), file=sys.stderr)
                return EXIT_USAGE

        on_conflict = args.on_conflict
        if on_conflict is None:
            existing = sum(1 for f in chosen if os.path.exists(os.path.join(out_dir, output_relpath(f))))
            on_conflict = ask_conflict(term, existing) if existing and interactive else CONFLICT_OVERWRITE

        if interactive and not ask_yes_no("Convert {} file(s) into {}?".format(len(chosen), out_dir), True):
            raise Cancelled()

        options = Options(
            encoding=args.encoding,
            max_rows=args.max_rows,
            max_cell=args.max_cell,
            include_deleted=args.include_deleted,
        )
        print()
        outcomes = convert_all(chosen, out_dir, options, on_conflict, make_progress(term, args.quiet), source)
        print_summary(term, outcomes, out_dir)
        return EXIT_FAILURES if any(o.status == STATUS_FAILED for o in outcomes) else EXIT_OK
    except Cancelled:
        print()
        print("Cancelled. Nothing more was written.")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        print()
        print(term.yellow("Interrupted. Files converted so far were kept; the index may be incomplete."))
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

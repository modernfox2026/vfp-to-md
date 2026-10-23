# vfp2md: Visual FoxPro to Markdown

A command line tool that reads a legacy Visual FoxPro (VFP) application and
turns its files into clean Markdown, so the application can be studied,
documented, or handed to an AI assistant as context for a migration to a
modern stack.

It is pure Python (standard library only, Python 3.8+). VFP does **not** need
to be installed and it runs on Windows, macOS and Linux.

## What it converts

| Kind | Files | What ends up in the Markdown |
|---|---|---|
| Project | `.pjx` + `.pjt` | Every file in the project grouped by type, main program, excluded files, build output, developer info |
| Database container | `.dbc` + `.dct` (+ `.dcx`) | Tables, fields, indexes, views (SQL), connections, persistent relations with referential integrity rules, stored procedures |
| Form | `.scx` + `.sct` | Data environment (cursors, relations, cursor adapters), object hierarchy, every object's class, properties, custom members, protected members and event/method code |
| Visual class library | `.vcx` + `.vct` | Class summary, each class with parent class, base class, include file, custom properties and methods, member objects and all method code |
| Report / label | `.frx` + `.frt`, `.lbx` + `.lbt` | Data environment, grouping, report variables, and every band with its fields, labels, expressions, formats, totals and "print when" conditions |
| Menu | `.mnx` + `.mnt` | The full menu tree (pads, popups, bars, separators, shortcuts, skip for, messages), setup/cleanup code and item procedures |
| Table | `.dbf` + `.fpt`/`.dbt` + `.cdx` | Header info, field structure (all VFP types incl. nullable, varchar, autoincrement), index tags with key and FOR expressions, and a data sample |
| Source code | `.prg` `.h` `.qpr` `.mpr` `.spr` `.fmt` `.act` | The code with procedure/function list |
| Text exports | SCCText (`.sca` `.vca` `.fra` `.lba` `.mna` `.dba`) and FoxBin2Prg (`.sc2` `.vc2` `.fr2` ...) | The text as a code block |
| Configuration | `.fpw` `.ini` `.cfg` `.txt` `.xml` `.json` `.htm` ... | The text as a code block |

Memo companions (`.sct`, `.vct`, `.fpt`, ...) are paired with their main file
automatically, even if the case differs (`MAIN.SCX` + `main.sct`). Compiled
files (`.fxp`, `.app`, `.exe`, `.dll`, `.fll`, ...) are counted but skipped,
because they contain no recoverable source.

## Running it

No installation is needed:

```bash
python vfp_to_md.py            # interactive mode
python -m vfp2md               # same thing
```

Or install it to get a `vfp2md` command:

```bash
pip install .
vfp2md
```

### Interactive mode

Run without arguments and the tool walks you through:

1. **Choose the source folder.** Paste or drag a path (quotes are fine, `~`
   and environment variables are expanded, Tab completes paths where the
   terminal supports it). Choose whether to include sub folders.
2. **Pick the files.** Every convertible file is listed with a number,
   grouped by kind, with its companions and size. Missing memo files are
   flagged. Type a selection (see below), review it, confirm.
3. **Choose the output folder.** It is created if needed and checked for
   write access. If outputs already exist you can overwrite, skip, or keep
   both.
4. **Convert.** Progress is shown per file; a summary follows.

Type `q` at any prompt to quit, `help` at the selection prompt for the syntax,
and `list` to see the files again.

### Selection syntax

Combine tokens with commas or spaces:

| Token | Meaning |
|---|---|
| `all` | every file |
| `3` | file number 3 |
| `2-7` | files 2 through 7 |
| `.scx` | every file with that extension |
| `form`, `table`, `classlib` ... | every file of a kind (`project`, `database`, `form`, `classlib`, `report`, `label`, `menu`, `table`, `program`, `text_export`, `config`) |
| `*cust*` | wildcard pattern matched against the relative path |
| `!token` | exclude whatever the token matches |

Examples: `all !table`, `1-4,9`, `.scx .vcx`, `*orders* !*.dbf`.

### Scripted mode

Everything can be passed as arguments so the tool can run unattended:

```bash
vfp2md C:\legacy\sales -o C:\docs\sales -s "all !table" --yes
vfp2md ./app --list                      # just show what would be converted
vfp2md ./app -o out -s table --max-rows 0 --yes   # full data dump of all tables
```

| Option | Purpose |
|---|---|
| `-o, --output DIR` | output folder |
| `-s, --select EXPR` | selection expression (default in scripted mode: all) |
| `-y, --yes` | never prompt |
| `-l, --list` | list convertible files and exit |
| `--no-recursive` | ignore sub folders |
| `--include-hidden` | include hidden files and folders |
| `--encoding CP` | force a code page (for example `cp1250`, `cp437`); default is the code page stored in each table |
| `--max-rows N` | rows of data per table (default 100, `0` = all) |
| `--max-cell N` | truncate long table cells (default 300, `0` = never) |
| `--include-deleted` | include records marked as deleted |
| `--on-conflict` | `overwrite`, `skip` or `rename` when outputs exist |
| `-q, --quiet` | only print the summary |
| `--no-color` | plain output (also honours `NO_COLOR`) |

Exit codes: `0` success, `1` at least one file failed, `2` usage error, `130`
cancelled or interrupted.

## Output layout

The source folder structure is mirrored and the original extension is kept
so names never collide:

```
out/
  INDEX.md                 table of contents + conversion log
  sales.pjx.md
  data/customer.dbf.md
  data/sales.dbc.md
  forms/custedit.scx.md
  libs/controls.vcx.md
  ...
```

Every document starts with a short description of the file kind and its
metadata, so each file makes sense on its own when given to an AI tool.
`INDEX.md` links every document and lists every warning or failure.

## How errors and odd data are handled

Legacy files are often damaged or unusual, so the tool is built to keep
going and tell you what happened instead of stopping:

* **One bad file never stops the run.** A file that cannot be parsed still
  gets a Markdown document explaining the failure, and it is listed in the
  index's conversion log. The exit code is `1` so scripts can notice.
* **Missing memo files** (`.sct`, `.fpt`, ...) are flagged in the listing; the
  rest of the file is still converted and the gaps are marked.
* **Corrupt tables:** wrong record counts, truncated records, bad record
  lengths, missing header terminators, unreadable cells, and memo pointers
  past the end of the memo file are detected and reported as notes. Readable
  data is still shown.
* **Character sets:** the table's code page byte picks the decoder (Windows,
  DOS, Mac, Eastern European, Cyrillic, Greek, Turkish, Hebrew, Arabic, Baltic,
  Thai, Chinese, Japanese, Korean). Undecodable bytes are replaced rather than
  crashing. Text files are read as UTF-8 when valid, otherwise with the
  selected code page. Use `--encoding` to override.
* **Markdown safety:** pipes in data are escaped, line breaks inside table
  cells become `<br>`, control characters are removed, CR/CRLF line endings
  are normalised, and code fences grow automatically when code itself
  contains backticks.
* **Binary content** (compiled code, OLE objects, pictures) is summarised by
  size rather than dumped.
* **Safe writing:** output files are written atomically (temp file + rename),
  output paths are sanitised, write permissions are checked before
  converting, and Ctrl+C stops cleanly keeping finished files.

## Limitations

* Compiled code (`.fxp`, `.app`, `.exe`) cannot be decompiled.
* DBC object properties are stored in an undocumented binary format; the
  tool extracts their text values (paths, captions, comments, defaults,
  rules, SQL) but does not name each property.
* ActiveX/OLE control state and embedded pictures are not decoded.
* Standalone `.idx` indexes are not read (structural `.cdx` indexes are).

## Development

```bash
python -m unittest discover -s tests
```

The tests build small, byte accurate VFP files (tables, memo files, forms,
class libraries, reports, menus, projects, database containers and compound
indexes) on the fly, so no sample files from a real application are needed.

"""Exception types shared across the package."""


class VfpError(Exception):
    """Base class for every error raised deliberately by vfp2md."""


class DbfFormatError(VfpError):
    """The file is not a readable DBF style table (bad or truncated header)."""


class MemoError(VfpError):
    """A memo file (.fpt, .sct, .vct, ...) is missing or unreadable."""

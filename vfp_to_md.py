#!/usr/bin/env python3
"""Convenience launcher: ``python vfp_to_md.py`` works without installing the package."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vfp2md.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

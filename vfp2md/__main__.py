"""Allow ``python -m vfp2md``."""

import sys

from .cli import main

sys.exit(main())

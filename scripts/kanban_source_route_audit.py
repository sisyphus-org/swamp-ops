#!/usr/bin/env python3
"""CLI/Swamp wrapper for the bundled SWE source-route audit."""

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.swe_linear_route.audit import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())

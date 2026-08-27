#!/usr/bin/env python3
from __future__ import annotations

import json


def main() -> int:
    print(
        json.dumps(
            {
                "mode": "read-only",
                "status": "ok",
                "checks": ["broker-runtime-available"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

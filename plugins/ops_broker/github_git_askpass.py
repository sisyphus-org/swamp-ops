#!/usr/bin/env python3
"""Fixed Git askpass bridge for the Operations Manager GitHub credential."""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit


def main() -> int:
    prompt = " ".join(sys.argv[1:]).lower()
    allowed_raw = os.environ.get("GIT_ASKPASS_ALLOWED_URL", "")
    try:
        allowed = urlsplit(allowed_raw)
    except ValueError:
        return 1
    if (
        allowed.scheme != "https"
        or allowed.hostname != "github.com"
        or allowed.port not in (None, 443)
        or allowed.username is not None
        or allowed.password is not None
        or not allowed.path.endswith(".git")
        or allowed.query
        or allowed.fragment
    ):
        return 1
    accepted = False
    for raw in re.findall(r"https://[^\s'\"<>]+", prompt):
        try:
            candidate = urlsplit(raw)
            port = candidate.port
        except ValueError:
            continue
        if (
            candidate.scheme == "https"
            and candidate.hostname == "github.com"
            and port in (None, 443)
            and not candidate.query
            and not candidate.fragment
            and candidate.path in ("", "/", allowed.path)
        ):
            accepted = True
            break
    if not accepted:
        return 1
    if "username" in prompt:
        print("x-access-token")
        return 0
    if "password" in prompt:
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            return 1
        print(token)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared fail-closed validation policy for the Project Manager Linear lane."""

from __future__ import annotations

import re

SAFE_STATES = frozenset({"Backlog", "Todo", "Research", "In Progress", "In Review"})
CREDENTIAL_SHAPES = (
    re.compile(r"Authorization:\s*(?:Bearer|Basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\blin_api_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[bap]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
RESERVED_MARKER = "<!-- linear-command"
RESERVED_COMMENT_MARKER = f"{RESERVED_MARKER}:v2"
RESERVED_CREATE_MARKER = f"{RESERVED_MARKER}:create:v2"

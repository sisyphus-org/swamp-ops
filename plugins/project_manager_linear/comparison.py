"""Shared, narrow comparison rules for Linear exact read-back."""

from __future__ import annotations

import re
from typing import Any, Iterable


SAFE_MISMATCH_FIELDS = (
    "id/title",
    "description",
    "state",
    "priority",
    "assignee",
    "labels",
    "parent",
    "project",
    "milestone",
    "team",
)


def description_matches(desired: str, live: Any) -> bool:
    """Match exact text or Linear's confirmed deterministic URL autolinking.

    Mutation payloads stay byte-for-byte unchanged. The only accepted alternate
    serialization is replacing every unambiguous plain HTTP(S) URL with
    ``[url](<url>)`` while preserving every other byte.
    """
    if live == desired:
        return True
    if not isinstance(live, str):
        return False
    urls = list(re.finditer(r"https?://[^\s\[\]<>]+", desired))
    if not urls or any(
        match.group(0).endswith(
            (".", ",", ";", ":", "!", "?", ")", "]", "}", "'", '"')
        )
        for match in urls
    ):
        return False
    plain_context = "".join(
        desired[end : match.start()]
        for end, match in zip(
            [0, *(item.end() for item in urls[:-1])],
            urls,
        )
    ) + desired[urls[-1].end() :]
    if re.search(r"[\[\]()<>`*_~|{}#\\]", plain_context):
        return False
    canonical = re.sub(
        r"https?://[^\s\[\]<>]+",
        lambda match: f"[{match.group(0)}](<{match.group(0)}>)",
        desired,
    )
    return live == canonical


def ordered_mismatch_fields(fields: Iterable[str]) -> list[str]:
    """Return a deduplicated allowlisted field list in stable public order."""
    found = set(fields)
    unknown = found.difference(SAFE_MISMATCH_FIELDS)
    if unknown:
        raise ValueError("read-back mismatch contains a non-allowlisted field")
    return [field for field in SAFE_MISMATCH_FIELDS if field in found]


def mismatch_message(operation: str, fields: Iterable[str]) -> str:
    """Render a safe blocker without live values or internal identifiers."""
    ordered = ordered_mismatch_fields(fields)
    if not ordered:
        raise ValueError("read-back mismatch requires at least one field")
    return f"{operation} read-back mismatched fields: {', '.join(ordered)}"

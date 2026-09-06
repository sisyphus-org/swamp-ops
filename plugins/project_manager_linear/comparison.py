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
    "due_date",
    "estimate",
    "parent",
    "project",
    "milestone",
    "team",
)


def _canonicalize_unordered_list_markers(desired: str) -> str | None:
    """Return Linear's observed ``- `` to ``* `` list serialization.

    Only complete Markdown list-item lines are changed. A deeply indented item
    is accepted only beneath an earlier, less-indented list item so indented
    code is not reinterpreted as a list. Fences and task-list markers stay
    outside this proven equivalence class.
    """
    if re.search(r"(?m)^[ ]*(?:```|~~~)", desired):
        return None

    canonical: list[str] = []
    list_indents: list[int] = []
    changed = False
    for line in desired.splitlines(keepends=True):
        match = re.match(r"^( *)(-) ([^\r\n]+)(\r?\n)?$", line)
        if match is None:
            canonical.append(line)
            continue

        indent = len(match.group(1))
        content = match.group(3)
        if content.startswith(("[ ] ", "[x] ", "[X] ")):
            return None
        if re.fullmatch(r"(?:-\s*){2,}", content):
            return None
        if indent >= 4 and not any(parent_indent < indent for parent_indent in list_indents):
            return None

        canonical.append(
            f"{match.group(1)}* {content}{match.group(4) or ''}"
        )
        list_indents.append(indent)
        changed = True

    return "".join(canonical) if changed else None


def description_matches(desired: str, live: Any) -> bool:
    """Match exact text or a narrowly confirmed Linear serialization.

    Mutation payloads stay byte-for-byte unchanged. Accepted alternate whole-
    value serializations are deterministic plain-URL autolinking and unordered
    Markdown list markers changing from ``- `` to ``* ``.
    """
    if live == desired:
        return True
    if not isinstance(live, str):
        return False

    canonical_list = _canonicalize_unordered_list_markers(desired)
    if canonical_list is not None and live == canonical_list:
        return True

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

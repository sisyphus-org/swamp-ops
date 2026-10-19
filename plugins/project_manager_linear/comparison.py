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
    "url",
    "archived",
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
        if indent > 4 or (
            indent >= 4
            and not any(parent_indent < indent for parent_indent in list_indents)
        ):
            return None

        canonical.append(
            f"{match.group(1)}* {content}{match.group(4) or ''}"
        )
        list_indents.append(indent)
        changed = True

    return "".join(canonical) if changed else None


# Characters Linear's Markdown serializer is observed to backslash-escape in
# plain prose (e.g. ``~114`` read back as ``\~114``).
_LINEAR_ESCAPED_PUNCTUATION = "~*_"


def _unescape_linear_punctuation(desired: str, live: str) -> str | None:
    """Undo Linear's backslash escaping of literal prose punctuation.

    Applies only when the desired text contains no backslash at all, so a
    caller-authored escape can never be confused with a provider escape, and
    only for the narrow ``~``/``*``/``_`` set. Returns ``None`` when the live
    value carries no such escape.
    """
    if "\\" in desired or "\\" not in live:
        return None
    unescaped = re.sub(
        rf"\\([{re.escape(_LINEAR_ESCAPED_PUNCTUATION)}])", r"\1", live
    )
    if "\\" in unescaped or unescaped == live:
        return None
    return unescaped


def _desired_variants(desired: str) -> list[str]:
    """Return desired plus Linear's whole-value line-ending/trailing trim."""
    variants = [desired]
    trimmed = desired.replace("\r\n", "\n").rstrip()
    if trimmed != desired:
        variants.append(trimmed)
    return variants


def description_matches(desired: str, live: Any) -> bool:
    """Match exact text or a narrowly confirmed Linear serialization.

    Mutation payloads stay byte-for-byte unchanged. Accepted alternate whole-
    value serializations are deterministic plain-URL autolinking, unordered
    Markdown list markers changing from ``- `` to ``* ``, backslash escaping
    of literal ``~``/``*``/``_`` in a backslash-free desired value, CRLF/
    trailing-whitespace trimming of the desired value, and their composition.
    Autolinking recognizes only plain prose and narrow heading/list prefixes;
    ambiguous code, markup, token boundaries, and punctuation fail closed.
    """
    if live == desired:
        return True
    if not isinstance(live, str):
        return False
    for variant in _desired_variants(desired):
        candidates = [live]
        unescaped = _unescape_linear_punctuation(variant, live)
        if unescaped is not None:
            candidates.append(unescaped)
        if any(_serialization_matches(variant, item) for item in candidates):
            return True
    return False


def _serialization_matches(desired: str, live: str) -> bool:
    """Exact, list-marker, and plain-URL serializations of one desired value."""
    if live == desired:
        return True

    canonical_list = _canonicalize_unordered_list_markers(desired)
    if canonical_list is not None and live == canonical_list:
        return True

    # Compose marker serialization and autolinking over the entire value.
    source = canonical_list if canonical_list is not None else desired
    urls = list(re.finditer(r"https?://[^\s\[\]<>]+", source))
    if not urls:
        return False
    if any(
        match.group(0).endswith(
            (".", ",", ";", ":", "!", "?", ")", "]", "}", "'", '"', "_")
        )
        or re.search(r"[()<>`*~|{}\\\"']", match.group(0))
        or (match.start() > 0 and not source[match.start() - 1].isspace())
        for match in urls
    ):
        return False
    # Inspect the original lines before removing URLs: removal must not turn
    # inline punctuation after a URL into an apparent heading/list prefix.
    context_lines: list[str] = []
    list_indents: list[int] = []
    for line in source.splitlines(keepends=True):
        if re.match(r"^ *\+ ", line) or re.fullmatch(r" *(?:[-*] *){3,}(?:\r?\n)?", line):
            return False
        item = re.match(r"^( *)([-*]) ", line)
        if item is not None:
            indent = len(item.group(1))
            if indent > 4 or (
                indent >= 4
                and not any(parent < indent for parent in list_indents)
            ):
                return False
            list_indents.append(indent)
            line = line[item.end() :]
        else:
            if line.startswith("    ") or re.match(r"^ *\t", line):
                return False
            if line.strip():
                list_indents.clear()
            line = re.sub(r"^ {0,3}#{1,6} ", "", line)
        context_lines.append(line)
    plain_context = re.sub(r"https?://[^\s\[\]<>]+", "", "".join(context_lines))
    if re.search(r"[\[\]()<>`*_~|{}#\\\"']", plain_context):
        return False
    canonical = re.sub(
        r"https?://[^\s\[\]<>]+",
        lambda match: f"[{match.group(0)}](<{match.group(0)}>)",
        source,
    )
    if live == canonical:
        return True
    # Keep the preexisting URL-only alternative for original plain contexts
    # (including hyphen lists). Rich heading/list contexts use the complete
    # composed representation above, not partial marker conversion.
    original_context = re.sub(r"https?://[^\s\[\]<>]+", "", desired)
    if canonical_list is not None and not re.search(
        r"[\[\]()<>`*_~|{}#\\]", original_context
    ):
        canonical_urls = re.sub(
            r"https?://[^\s\[\]<>]+",
            lambda match: f"[{match.group(0)}](<{match.group(0)}>)",
            desired,
        )
        return live == canonical_urls
    return False


def ordered_mismatch_fields(fields: Iterable[str]) -> list[str]:
    """Return a deduplicated allowlisted field list in stable public order."""
    found = set(fields)
    unknown = found.difference(SAFE_MISMATCH_FIELDS)
    if unknown:
        raise ValueError("read-back mismatch contains a non-allowlisted field")
    return [field for field in SAFE_MISMATCH_FIELDS if field in found]


def mismatch_message(
    operation: str, fields: Iterable[str], identifier: Any = None
) -> str:
    """Render a safe blocker without live values or internal identifiers.

    A public ``SIS-N`` identifier of the mismatched issue may be appended so the
    source can reconcile the exact issue; any other identifier shape is dropped.
    """
    ordered = ordered_mismatch_fields(fields)
    if not ordered:
        raise ValueError("read-back mismatch requires at least one field")
    message = f"{operation} read-back mismatched fields: {', '.join(ordered)}"
    if isinstance(identifier, str) and re.fullmatch(r"SIS-[1-9][0-9]*", identifier):
        message += f" ({identifier})"
    return message

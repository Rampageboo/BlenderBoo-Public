#!/usr/bin/env python3
"""Validate freshness metadata in a Project Context exact-facts YAML file.

This deliberately uses only the Python standard library. It is not a general
YAML parser; it scans the small metadata vocabulary owned by the protocol while
leaving project-specific fact shapes untouched.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


KEY_VALUE = re.compile(
    r"^(?P<indent>[ ]*)(?:-[ ]+)?(?P<key>[A-Za-z_][A-Za-z0-9_-]*):"
    r"[ ]*(?P<value>.*?)[ ]*$"
)
REVISION_KEY = "source_revision_represented"
TRIGGER_KEYS = {"refresh_trigger", "update_trigger"}
EMPTY_VALUES = {"", "null", "~"}
MANUAL_VALUES = {"manual", "manually", "on request", "on-request"}


@dataclass(frozen=True)
class Entry:
    line: int
    index: int
    indent: int
    key: str
    value: str
    list_item: bool


def _scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


def _entries(lines: list[str]) -> list[Entry]:
    parsed: list[Entry] = []
    for index, raw in enumerate(lines):
        match = KEY_VALUE.match(raw)
        if not match:
            continue
        parsed.append(
            Entry(
                line=index + 1,
                index=index,
                indent=len(match.group("indent")),
                key=match.group("key"),
                value=_scalar(match.group("value")),
                list_item=raw.lstrip().startswith("- "),
            )
        )
    return parsed


def _record_entries(status: Entry, entries: list[Entry], line_count: int) -> list[Entry]:
    start = 0
    boundary_indent = -1
    boundary_is_list = False

    if status.list_item:
        start = status.index
        boundary_indent = status.indent
        boundary_is_list = True
    else:
        for candidate in reversed(entries):
            if candidate.index >= status.index:
                continue
            if candidate.list_item and candidate.indent < status.indent:
                start = candidate.index
                boundary_indent = candidate.indent
                boundary_is_list = True
                break
            if candidate.indent < status.indent:
                start = candidate.index + 1
                boundary_indent = candidate.indent
                break

    end = line_count
    for candidate in entries:
        if candidate.index <= status.index:
            continue
        if (
            boundary_indent >= 0
            and not boundary_is_list
            and candidate.indent <= boundary_indent
        ):
            end = candidate.index
            break
        if boundary_is_list and candidate.indent < boundary_indent:
            end = candidate.index
            break
        if (
            boundary_is_list
            and candidate.list_item
            and candidate.indent == boundary_indent
        ):
            end = candidate.index
            break
    return [entry for entry in entries if start <= entry.index < end]


def _revision_matches(recorded: str, actual: str) -> bool:
    recorded = recorded.lower()
    actual = actual.lower()
    return len(recorded) >= 7 and actual.startswith(recorded)


def validate_text(
    text: str, actual_revision: str, *, now: datetime | None = None
) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    entries = _entries(lines)
    errors: list[str] = []
    warnings: list[str] = []
    now = now or datetime.now(timezone.utc)

    parsed_lines = {(entry.line, entry.key) for entry in entries}
    watched = {
        "status": re.compile(r"\bstatus\s*:\s*['\"]?current\b", re.IGNORECASE),
        REVISION_KEY: re.compile(rf"\b{REVISION_KEY}\s*:"),
    }
    for line_number, raw in enumerate(lines, start=1):
        if raw.lstrip().startswith("#"):
            continue
        for key, pattern in watched.items():
            if pattern.search(raw) and (line_number, key) not in parsed_lines:
                errors.append(
                    f"line {line_number}: unsupported inline or tab-indented "
                    f"{key} metadata; use block-style YAML"
                )

    revisions = [
        entry
        for entry in entries
        if entry.key == REVISION_KEY and entry.value not in EMPTY_VALUES
    ]
    for entry in revisions:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", entry.value):
            errors.append(
                f"line {entry.line}: {REVISION_KEY} is not a Git revision"
            )
        elif not _revision_matches(entry.value, actual_revision):
            errors.append(
                f"line {entry.line}: represented revision {entry.value} is stale; "
                f"workspace is {actual_revision}"
            )

    for status in (
        entry
        for entry in entries
        if entry.key == "status" and entry.value.lower() == "current"
    ):
        record = _record_entries(status, entries, len(lines))
        local_revisions = [
            entry
            for entry in record
            if entry.key == REVISION_KEY and entry.value not in EMPTY_VALUES
        ]
        conditions = [
            entry
            for entry in record
            if entry.key == "validity_condition" and entry.value not in EMPTY_VALUES
        ]
        triggers = [
            entry
            for entry in record
            if entry.key in TRIGGER_KEYS
            and entry.value.lower() not in EMPTY_VALUES | MANUAL_VALUES
        ]
        expiries = [
            entry
            for entry in record
            if entry.key == "valid_until" and entry.value not in EMPTY_VALUES
        ]

        if not (local_revisions or conditions or triggers or expiries):
            errors.append(
                f"line {status.line}: status current has no checkable revision, "
                "finite validity, condition, or non-manual refresh trigger"
            )

        for expiry in expiries:
            try:
                parsed = datetime.fromisoformat(expiry.value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                errors.append(f"line {expiry.line}: valid_until is not ISO-8601")
                continue
            if parsed <= now:
                errors.append(f"line {expiry.line}: validity expired at {expiry.value}")

        if conditions or triggers:
            warnings.append(
                f"line {status.line}: current status depends on a condition or "
                "refresh trigger; verify it before consequential use"
            )

    return errors, warnings


def git_revision(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    revision = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise RuntimeError("git rev-parse did not return a full revision")
    return revision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts_file", type=Path)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--revision",
        help="compare against this full Git revision instead of reading --repo",
    )
    args = parser.parse_args(argv)

    try:
        revision = args.revision or git_revision(args.repo)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise RuntimeError("--revision must be a full 40-character Git revision")
        text = args.facts_file.read_text(encoding="utf-8-sig")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: freshness check could not run: {exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_text(text, revision)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"OK: checked all freshness records against {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

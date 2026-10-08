"""Matchers for policy rules.

An argument matcher is one of:

- a string: a glob. If it looks like a path (`~`, `/`, `\\`, `C:`, or `**` at the start), it is a
  path glob, and the argument is normalized first (`~`, `..`, symlinks), so `~/code/../.ssh/key`
  cannot slip past a rule for `~/.ssh/**`.
- a list: any of the matchers.
- `{glob: ...}`, `{path: ...}`, `{regex: ...}` (re.search), or `{equals: ...}`.

Path globs: `**` matches across folders, `*` and `?` stay inside one folder. `dir/**` also
matches `dir` itself.
Matching is case-insensitive on Windows.
"""

import fnmatch
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

IGNORE_CASE = sys.platform == "win32"
_PATH_START = re.compile(r"^(~|/|\\|\*\*|[A-Za-z]:)")


def looks_like_path(pattern: str) -> bool:
    return bool(_PATH_START.match(pattern))


def normalize_path(value: str) -> str:
    try:
        p = Path(value).expanduser().resolve().as_posix()
    except (OSError, ValueError):  # not a usable path (NUL byte, too long): match the raw text
        p = value.replace("\\", "/")
    return p.lower() if IGNORE_CASE else p


@lru_cache(maxsize=1024)
def _path_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/")
    head, sep, rest = pattern.partition("/")
    if head.startswith("~"):
        pattern = Path(head).expanduser().as_posix() + sep + rest
    out, i = [], 0
    while i < len(pattern):
        if pattern.endswith("/**") and i == len(pattern) - 3:
            out.append("(?:/.*)?")  # "dir/**" also matches "dir" itself
            i += 3
        elif pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    # A relative pattern such as "**/.env" may match at any depth; an absolute one only from the start.
    prefix = "" if pattern.startswith(("**", "/")) or re.match(r"^[A-Za-z]:", pattern) else "(?:.*/)?"
    return re.compile(prefix + "".join(out) + r"\Z", re.IGNORECASE if IGNORE_CASE else 0)


def path_matches(pattern: str, value: str) -> bool:
    return bool(_path_regex(pattern).match(normalize_path(value)))


def arg_matches(matcher: Any, value: Any) -> bool:
    if isinstance(matcher, list):
        return any(arg_matches(m, value) for m in matcher)
    if isinstance(matcher, dict):
        if len(matcher) != 1:
            raise ValueError(f"Matcher needs exactly one key: {matcher}")
        [(kind, pattern)] = matcher.items()
        if kind == "equals":
            return value == pattern
        if not isinstance(value, str):
            return False
        if kind == "regex":
            return re.search(pattern, value) is not None
        if kind == "path":
            return path_matches(pattern, value)
        if kind == "glob":
            return fnmatch.fnmatchcase(value, pattern)
        raise ValueError(f"Unknown matcher {kind!r}")
    if isinstance(matcher, str):
        if not isinstance(value, str):
            return False
        return path_matches(matcher, value) if looks_like_path(matcher) else fnmatch.fnmatchcase(value, matcher)
    return value == matcher


def name_matches(patterns: str | list[str], name: str) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)

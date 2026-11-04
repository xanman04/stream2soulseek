"""Strict parsing and precedence comparison for application release versions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
import re


_VERSION_RE = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?",
    re.ASCII,
)


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    """A parsed SemVer version; comparisons follow SemVer precedence."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def _precedence_key(self) -> tuple[object, ...]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch) and _prerelease_equal(self.prerelease, other.prerelease)

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return left_core < right_core
        return _prerelease_lt(self.prerelease, other.prerelease)


def _prerelease_equal(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return not _prerelease_lt(left, right) and not _prerelease_lt(right, left)


def _prerelease_lt(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    # A stable release outranks a prerelease with the same core version.
    if not left:
        return False
    if not right:
        return True
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric, b_numeric = a.isascii() and a.isdigit(), b.isascii() and b.isdigit()
        if a_numeric and b_numeric:
            return int(a) < int(b)
        if a_numeric != b_numeric:
            return a_numeric
        return a < b
    return len(left) < len(right)


def parse_version(text: str) -> Version:
    """Parse strict SemVer with an optional ``v`` prefix, or raise ValueError."""
    if not isinstance(text, str):
        raise ValueError("version must be a string")
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid semantic version: {text!r}")
    major, minor, patch, prerelease, build = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease else ()
    if any(identifier.isascii() and identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0") for identifier in identifiers):
        raise ValueError(f"invalid numeric prerelease identifier in {text!r}")
    return Version(
        int(major), int(minor), int(patch), identifiers,
        tuple(build.split(".")) if build else (),
    )


def is_newer(candidate: str, current: str) -> bool:
    """Return whether candidate has higher SemVer precedence; invalid input is false."""
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False

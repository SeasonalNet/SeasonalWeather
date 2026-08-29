#!/usr/bin/env python3
"""SeasonalWeather release-tag and PEP 440 version guardrails.

This script derives the development version from Git instead of importing the
package or requiring runtime dependencies. Release tags use SemVer; Python
package and runtime versions use the corresponding PEP 440 spelling.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NoReturn

SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
PEP440_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<pre>a|b|rc)(?P<pre_number>0|[1-9]\d*))?"
    r"(?:\.dev(?P<development>0|[1-9]\d*))?"
    r"(?:\+(?P<local>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$",
    re.IGNORECASE,
)


class SemVerError(ValueError):
    """Raised when a version string violates the local SemVer policy."""


class Pep440Error(ValueError):
    """Raised when a generated software version is not valid PEP 440."""


@dataclass(frozen=True)
class SemVer:
    original: str
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = SEMVER_RE.match(value)
        if not match:
            raise SemVerError(f"invalid SemVer version: {value!r}")

        prerelease_text = match.group("prerelease") or ""
        prerelease = tuple(prerelease_text.split(".")) if prerelease_text else ()
        for ident in prerelease:
            if ident.isdigit() and len(ident) > 1 and ident.startswith("0"):
                raise SemVerError(f"invalid SemVer prerelease identifier with leading zero: {ident!r}")

        return cls(
            original=value,
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
        )

    def precedence_key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def compare(self, other: SemVer) -> int:
        if self.precedence_key() != other.precedence_key():
            return (self.precedence_key() > other.precedence_key()) - (self.precedence_key() < other.precedence_key())

        # Build metadata is ignored by design because it has no precedence.
        if self.prerelease != other.prerelease:
            if not self.prerelease:
                return 1
            if not other.prerelease:
                return -1
            for left, right in zip(self.prerelease, other.prerelease, strict=False):
                if left == right:
                    continue
                left_numeric = left.isdigit()
                right_numeric = right.isdigit()
                if left_numeric and right_numeric:
                    return (int(left) > int(right)) - (int(left) < int(right))
                if left_numeric:
                    return -1
                if right_numeric:
                    return 1
                return (left > right) - (left < right)

            prerelease_result = (len(self.prerelease) > len(other.prerelease)) - (
                len(self.prerelease) < len(other.prerelease)
            )
            if prerelease_result:
                return prerelease_result
        return 0

    def __lt__(self, other: SemVer) -> bool:
        return self.compare(other) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self.compare(other) == 0


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        die(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def _pep440(value: str) -> None:
    if PEP440_RE.fullmatch(value) is None:
        raise Pep440Error(f"invalid PEP 440 software version: {value!r}")


def _semver_prerelease_to_pep440(prerelease: tuple[str, ...]) -> str:
    if not prerelease:
        return ""
    if len(prerelease) == 1:
        match = re.fullmatch(r"(?P<label>alpha|beta|rc|dev)(?:[-.](?P<number>0|[1-9]\d*))?", prerelease[0])
        if match is None:
            raise Pep440Error(f"unsupported SemVer prerelease for PEP 440 conversion: {'.'.join(prerelease)!r}")
        label = match.group("label")
        number = match.group("number") or "0"
    elif len(prerelease) == 2 and prerelease[0] in {"alpha", "beta", "rc", "dev"}:
        label, number = prerelease
        if not number.isdigit() or (len(number) > 1 and number.startswith("0")):
            raise Pep440Error(f"unsupported SemVer prerelease number: {'.'.join(prerelease)!r}")
    else:
        raise Pep440Error(f"unsupported SemVer prerelease for PEP 440 conversion: {'.'.join(prerelease)!r}")

    pep_label = {"alpha": "a", "beta": "b", "rc": "rc", "dev": ".dev"}[label]
    return f"{pep_label}{number}"


def pep440_from_git_describe(describe: str) -> str:
    """Convert a Git-describe SemVer tag into a PEP 440 software version."""

    match = re.fullmatch(
        r"v(?P<version>.+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?",
        describe,
    )
    if match is None:
        raise Pep440Error(f"unsupported Git describe output: {describe}")

    parsed_base = SemVer.parse(match.group("version"))
    distance = int(match.group("distance"))
    prerelease = _semver_prerelease_to_pep440(parsed_base.prerelease)
    if distance == 0 and match.group("dirty") is None:
        software_version = f"{parsed_base.major}.{parsed_base.minor}.{parsed_base.patch}{prerelease}"
    elif parsed_base.prerelease and prerelease.startswith(".dev"):
        raise Pep440Error("cannot represent commits after a SemVer development tag in PEP 440")
    else:
        if parsed_base.prerelease:
            release = f"{parsed_base.major}.{parsed_base.minor}.{parsed_base.patch}{prerelease}.dev{distance}"
        else:
            release = f"{parsed_base.major}.{parsed_base.minor}.{parsed_base.patch + 1}.dev{distance}"
        local = f"g{match.group('sha')}"
        if match.group("dirty"):
            local += ".dirty"
        software_version = f"{release}+{local}"
    _pep440(software_version)
    return software_version


def vcs_version() -> str:
    describe = git("describe", "--tags", "--long", "--dirty", "--match", "v[0-9]*")
    try:
        return pep440_from_git_describe(describe)
    except (Pep440Error, SemVerError) as exc:
        die(str(exc))


def tag_to_version(tag_name: str) -> str:
    if not tag_name.startswith("v"):
        die(f"release tag must start with 'v': {tag_name}")
    return tag_name[1:]


def semver_tags(exclude: Iterable[str] = ()) -> list[tuple[str, SemVer]]:
    excluded = set(exclude)
    tags: list[tuple[str, SemVer]] = []
    for tag_name in git("tag", "--list", "v*").splitlines():
        tag_name = tag_name.strip()
        if not tag_name or tag_name in excluded:
            continue
        try:
            tags.append((tag_name, SemVer.parse(tag_to_version(tag_name))))
        except SemVerError:
            # Non-release v* tags should not influence release ordering.
            continue
    return tags


def latest_release_before(tag_name: str | None = None) -> tuple[str, SemVer] | None:
    tags = semver_tags(exclude=[tag_name] if tag_name else [])
    if not tags:
        return None
    return max(tags, key=lambda item: item[1])


def command_version(_: argparse.Namespace) -> None:
    print(vcs_version())


def command_check_working(_: argparse.Namespace) -> None:
    version = vcs_version()
    try:
        _pep440(version)
    except Pep440Error as exc:
        die(str(exc))
    print(f"PEP 440 version ok: {version}")


def command_check_version(args: argparse.Namespace) -> None:
    try:
        SemVer.parse(args.version)
    except SemVerError as exc:
        die(str(exc))
    print(f"SemVer tag version ok: {args.version}")


def command_check_tag(args: argparse.Namespace) -> None:
    tag_name = args.tag
    tag_version = tag_to_version(tag_name)
    try:
        SemVer.parse(tag_version)
    except SemVerError as exc:
        die(str(exc))

    tag_type = git("cat-file", "-t", tag_name)
    if tag_type != "tag":
        die(f"release tag must be annotated: {tag_name} is a {tag_type} object")

    tagged_commit = git("rev-parse", f"{tag_name}^{{}}")
    head_commit = git("rev-parse", "HEAD")
    if tagged_commit != head_commit:
        die(f"checked-out commit does not match release tag: tag={tag_name} head={head_commit[:12]}")

    print(f"tag ok: {tag_name} is annotated and names the checked-out release commit")


def command_check_newer(args: argparse.Namespace) -> None:
    version = SemVer.parse(args.version)
    latest = latest_release_before(args.exclude_tag)
    if latest is None:
        print(f"version order ok: {args.version} is the first release")
        return

    latest_tag, latest_version = latest
    if version.compare(latest_version) <= 0:
        die(f"new version {args.version} must be greater than latest release {latest_tag} ({latest_version.original})")
    print(f"version order ok: {args.version} > {latest_tag}")


def command_check_tag_order(args: argparse.Namespace) -> None:
    tag_name = args.tag
    version = SemVer.parse(tag_to_version(tag_name))
    latest = latest_release_before(tag_name)
    if latest is None:
        print(f"tag order ok: {tag_name} is the first release")
        return

    latest_tag, latest_version = latest
    if version.compare(latest_version) <= 0:
        die(f"tag {tag_name} must be greater than previous release {latest_tag}")
    print(f"tag order ok: {tag_name} > {latest_tag}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SeasonalWeather SemVer and PEP 440 guardrails")
    subparsers = parser.add_subparsers(required=True)

    version = subparsers.add_parser("version", help="print the code version")
    version.set_defaults(func=command_version)

    check_working = subparsers.add_parser("check-working", help="validate the Git-derived PEP 440 package version")
    check_working.set_defaults(func=command_check_working)

    check_version = subparsers.add_parser("check-version", help="validate a SemVer release-tag version")
    check_version.add_argument("version")
    check_version.set_defaults(func=command_check_version)

    check_tag = subparsers.add_parser(
        "check-tag", help="validate that an annotated vX.Y.Z tag names the checked-out commit"
    )
    check_tag.add_argument("tag")
    check_tag.set_defaults(func=command_check_tag)

    check_newer = subparsers.add_parser("check-newer", help="validate that a version is greater than existing releases")
    check_newer.add_argument("version")
    check_newer.add_argument(
        "--exclude-tag",
        default=None,
        help="tag to exclude from latest-release comparison",
    )
    check_newer.set_defaults(func=command_check_newer)

    check_tag_order = subparsers.add_parser(
        "check-tag-order", help="validate that a tag is newer than previous releases"
    )
    check_tag_order.add_argument("tag")
    check_tag_order.set_defaults(func=command_check_tag_order)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

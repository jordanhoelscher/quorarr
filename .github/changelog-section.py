#!/usr/bin/env python3
"""Print one version's section from a Keep a Changelog file.

Used by the Release workflow to turn `## [1.1.0] — 2026-08-15` and everything
under it into the body of the GitHub Release, so the release page and the
changelog can never disagree -- there is only one copy of the words.

Usage:
    changelog-section.py CHANGELOG.md v1.1.0

Exits non-zero, with a message on stderr, if the version has no section or its
section is empty. A release published with silently empty notes is worse than
a failed release job: the tag and image are already out, and nobody looks at a
release page twice.
"""
import re
import sys
from pathlib import Path

#: `## [1.2.3] — 2026-08-15`, `## [1.2.3]`, or `## [1.2.3-rc1] - 2026-08-15`.
#: The version is matched exactly, so `1.1` cannot match the `1.1.0` heading.
_HEADING = "^## +\\[{}\\]"
_ANY_HEADING = re.compile(r"^## +\[", re.MULTILINE)


def section(text: str, version: str) -> str | None:
    """Return the body under ``version``'s heading, or None if absent.

    Args:
        text: The whole changelog.
        version: A version with or without its leading ``v``.

    Returns:
        Everything between this heading and the next ``## [`` heading (or the
        end of the file), stripped. None if there is no such heading.
    """
    version = version.lstrip("vV")
    start = re.search(_HEADING.format(re.escape(version)), text, re.MULTILINE)
    if start is None:
        return None

    rest = text[start.end():]
    # Skip to the end of the heading line, then read up to the next heading.
    _, _, body = rest.partition("\n")
    following = _ANY_HEADING.search(body)
    return (body[: following.start()] if following else body).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} CHANGELOG.md VERSION", file=sys.stderr)
        return 2

    _, path, version = argv
    body = section(Path(path).read_text(encoding="utf-8"), version)

    if not body:
        print(
            f"no changelog section for {version} in {path} -- add one before tagging",
            file=sys.stderr,
        )
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

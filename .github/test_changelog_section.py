"""Tests for the release-notes extractor the Release workflow runs.

Run from the backend job: ``pytest ../.github -q``. This lives with the
workflow rather than in ``backend/tests`` because it tests CI tooling, not the
application -- but it is still run by CI, so the extractor cannot rot silently
and first be noticed as an empty release page.
"""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "changelog-section.py"

SAMPLE = """# Changelog

Preamble that is not part of any release.

## [1.1.0] — 2026-08-15

### Added

- A thing.

## [1.0.0] — 2026-08-15

Initial public release.

## [0.9.0] — 2026-08-14

- The oldest one.
"""


def extract(tmp_path, version, text=SAMPLE):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(text)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(changelog), version],
        capture_output=True,
        text=True,
    )


def test_extracts_the_section_for_a_version(tmp_path):
    result = extract(tmp_path, "1.1.0")

    assert result.returncode == 0
    assert result.stdout.strip() == "### Added\n\n- A thing."


def test_stops_at_the_next_version_heading(tmp_path):
    """The 1.0.0 body must not swallow 0.9.0."""
    result = extract(tmp_path, "1.0.0")

    assert result.stdout.strip() == "Initial public release."


def test_extracts_the_last_section_with_no_heading_after_it(tmp_path):
    result = extract(tmp_path, "0.9.0")

    assert result.stdout.strip() == "- The oldest one."


def test_never_returns_the_preamble(tmp_path):
    """A version that isn't there must fail, not hand back the file's top."""
    result = extract(tmp_path, "9.9.9")

    assert result.returncode != 0
    assert "9.9.9" in result.stderr
    assert "Preamble" not in result.stdout


def test_a_v_prefixed_tag_name_is_accepted(tmp_path):
    """The workflow passes the git tag; requiring it to strip the v is a trap."""
    result = extract(tmp_path, "v1.1.0")

    assert result.returncode == 0
    assert result.stdout.strip() == "### Added\n\n- A thing."


def test_an_empty_section_fails_rather_than_publishing_nothing(tmp_path):
    text = "# Changelog\n\n## [2.0.0] — 2026-09-01\n\n## [1.0.0] — 2026-08-15\n\nBody.\n"

    result = extract(tmp_path, "2.0.0", text)

    assert result.returncode != 0
    assert "2.0.0" in result.stderr


def test_a_version_that_prefixes_another_is_not_confused(tmp_path):
    """`1.1` must not match the `1.1.0` heading."""
    result = extract(tmp_path, "1.1")

    assert result.returncode != 0

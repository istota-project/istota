"""The guard that refuses a release cut with no announcement in front of it.

`scripts/release.sh` requires the `[Unreleased]` section to open with prose —
everything before the first `### ` heading — because that is what both
extractors emit ahead of the changeset, in the tag annotation and in the
GitHub Release body. See `.claude/rules/releases.md`.

The guard is a python heredoc inside the shell script, and it is extracted
from the shipped file rather than restated here: a copy of it in this module
would be the thing that drifts, and the guard's whole job is to be running on
the day somebody forgets. `scripts/release.sh` itself is not run — it pushes
tags — so what is exercised is the check, against a CHANGELOG in a tmp dir.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO / "scripts" / "release.sh"

CANONICAL_SECTIONS = "### Added\n\n- Something a person can see.\n"


def _guard_source() -> str:
    text = RELEASE_SH.read_text()
    m = re.search(r"<<'ANNOUNCEMENT_CHECK'\n(.*?)\nANNOUNCEMENT_CHECK\n", text, re.S)
    assert m, "the announcement guard is no longer a heredoc in scripts/release.sh"
    return m.group(1)


def _run(tmp_path: Path, changelog: str) -> subprocess.CompletedProcess:
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return subprocess.run(
        [sys.executable, "-c", _guard_source()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def _changelog(unreleased_body: str) -> str:
    return (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        f"{unreleased_body}"
        "## [0.40.1] - 2026-08-12\n\n"
        "### Fixed\n\n- An older release, which the guard must not read.\n"
    )


ANNOUNCEMENT = (
    "This release is mostly one thing: the boundary the deployment reported as "
    "working was not working, on the shape that ships. It also carries the "
    "offline web chat work, and a bell that collects everything waiting on you.\n\n"
    "**Before you upgrade.** Reissue your forge tokens; the CLIs need wider scopes "
    "than the wrappers they replace.\n\n"
)


class TestTheGuardAcceptsAnAnnouncement:
    def test_prose_before_the_first_subsection_passes(self, tmp_path):
        r = _run(tmp_path, _changelog(ANNOUNCEMENT + CANONICAL_SECTIONS))
        assert r.returncode == 0, r.stderr


class TestTheGuardRefuses:
    def test_a_section_that_goes_straight_into_the_changeset(self, tmp_path):
        r = _run(tmp_path, _changelog(CANONICAL_SECTIONS))
        assert r.returncode != 0
        assert "no release announcement" in r.stderr

    def test_a_placeholder_too_short_to_be_an_announcement(self, tmp_path):
        r = _run(tmp_path, _changelog("Bug fixes.\n\n" + CANONICAL_SECTIONS))
        assert r.returncode != 0
        assert "no release announcement" in r.stderr

    def test_a_third_level_heading_inside_the_announcement(self, tmp_path):
        # Reordered to the bottom of the release notes by both extractors,
        # which is the announcement arriving after what it introduces.
        body = ANNOUNCEMENT + "### Highlights\n\nMore prose.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "Highlights" in r.stderr

    def test_a_top_level_heading_inside_the_announcement(self, tmp_path):
        body = ANNOUNCEMENT + "## Security\n\nMore prose.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "## Security" in r.stderr

    def test_a_misspelled_changeset_subsection(self, tmp_path):
        # Same mechanism as a stray heading: not a canonical bucket, so it is
        # emitted after `### Security` rather than where it was written.
        body = ANNOUNCEMENT + "### Fixes\n\n- Something.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "Fixes" in r.stderr


class TestTheGuardReadsTheRightSection:
    def test_an_announcement_on_the_previous_release_does_not_count(self, tmp_path):
        changelog = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            f"{CANONICAL_SECTIONS}\n"
            "## [0.40.1] - 2026-08-12\n\n"
            f"{ANNOUNCEMENT}"
            "### Fixed\n\n- Something.\n"
        )
        r = _run(tmp_path, changelog)
        assert r.returncode != 0
        assert "no release announcement" in r.stderr


class TestTheEscapeHatch:
    def test_the_script_takes_no_announcement_and_a_version_in_either_order(self):
        text = RELEASE_SH.read_text()
        assert "--no-announcement" in text
        assert 'REQUIRE_ANNOUNCEMENT=0' in text

    @pytest.mark.parametrize(
        "argv",
        [
            ["0.9.0"],
            ["--no-announcement", "0.9.0"],
            ["0.9.0", "--codename", "Dumbo octopus"],
            ["--codename", "Dumbo octopus", "--no-announcement", "0.9.0"],
        ],
    )
    def test_the_argument_loop_finds_the_version(self, tmp_path, argv):
        r = _run_argument_loop(argv, '\necho "$NEW|$REQUIRE_ANNOUNCEMENT|$CODENAME"')
        assert r.returncode == 0, r.stderr
        version, required, codename = r.stdout.strip().split("|")
        assert version == "0.9.0"
        assert required == ("0" if "--no-announcement" in argv else "1")
        assert codename == ("Dumbo octopus" if "--codename" in argv else "")


def _run_argument_loop(argv, tail):
    loop = re.search(
        r"REQUIRE_ANNOUNCEMENT=1\n(.*?)\n: \"\$\{NEW:\?[^\n]*\n(.*?)\nfi\n",
        RELEASE_SH.read_text(),
        re.S,
    )
    assert loop, "the argument loop in scripts/release.sh has moved"
    script = (
        "REQUIRE_ANNOUNCEMENT=1\n"
        + loop.group(1)
        + '\n: "${NEW:?}"\n'
        + loop.group(2)
        + "\nfi"
        + tail
    )
    return subprocess.run(
        ["bash", "-c", script, "release.sh", *argv],
        capture_output=True,
        text=True,
    )


class TestTheCodename:
    """A headline release's codename rides on its version heading.

    `scripts/release.sh` writes it there and `release.yml` reads it back for the
    GitHub Release title; both halves are extracted from the shipped files.
    """

    @pytest.mark.parametrize("name", ["Dumbo)", "a\nb", "", "x" * 61, "Octopus [x]"])
    def test_a_name_that_could_break_the_heading_is_refused(self, name):
        r = _run_argument_loop(["0.9.0", "--codename", name], "")
        if name == "":
            assert r.returncode == 0  # an empty name is no codename
        else:
            assert r.returncode != 0
            assert "codename must be" in r.stderr

    def _write_heading(self, tmp_path, codename):
        text = RELEASE_SH.read_text()
        m = re.search(r'"\$CODENAME" <<\'PY\'\n(.*?)\nPY\n', text, re.S)
        assert m, "the heading writer in scripts/release.sh has moved"
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\nNotes.\n\n"
            "[Unreleased]: https://example.invalid/compare/v0.8.0...main\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", m.group(1), "0.9.0", "2026-10-13", "https://example.invalid", codename],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr

    def _title(self, tmp_path):
        import yaml

        workflow = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text())
        step = next(
            s for s in workflow["jobs"]["release"]["steps"] if s.get("name") == "Create or update GitHub Release"
        )
        m = re.search(r"<<'PY'\n(.*?)\nPY\n", step["run"], re.S)
        assert m, "the title extractor in release.yml has moved"
        r = subprocess.run(
            [sys.executable, "-c", m.group(1)],
            cwd=tmp_path,
            env={"VERSION": "0.9.0", "TAG": "v0.9.0"},
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_the_codename_reaches_the_release_title(self, tmp_path):
        self._write_heading(tmp_path, "Dumbo octopus")
        assert "## [0.9.0] - 2026-10-13 (Dumbo octopus)\n" in (tmp_path / "CHANGELOG.md").read_text()
        assert self._title(tmp_path) == "v0.9.0 (Dumbo octopus)"

    def test_a_release_without_one_is_titled_by_its_tag(self, tmp_path):
        self._write_heading(tmp_path, "")
        assert "## [0.9.0] - 2026-10-13\n" in (tmp_path / "CHANGELOG.md").read_text()
        assert self._title(tmp_path) == "v0.9.0"


class TestTheLengthLimits:
    """The section is release notes, not a merge log; 0.44.0 is the model."""

    def test_the_0_44_0_section_passes(self, tmp_path):
        text = (REPO / "CHANGELOG.md").read_text()
        m = re.search(r"^## \[0\.44\.0\][^\n]*\n(.*?)(?=^## \[)", text, re.M | re.S)
        assert m, "the 0.44.0 section is gone from CHANGELOG.md"
        r = _run(tmp_path, _changelog(m.group(1).lstrip("\n")))
        assert r.returncode == 0, r.stderr

    def test_a_long_changeset_entry_is_named(self, tmp_path):
        long_entry = "- **A feature** that " + "goes on " * 30 + "and on.\n"
        r = _run(tmp_path, _changelog(ANNOUNCEMENT + "### Added\n\n" + long_entry))
        assert r.returncode != 0
        assert "too long to read as release notes" in r.stderr
        assert "**A feature** that goes on" in r.stderr

    def test_an_indented_upgrade_note_counts_toward_its_entry(self, tmp_path):
        entry = "- Short.\n\n  **Upgrade note:** " + "more words here " * 20 + "\n"
        r = _run(tmp_path, _changelog(ANNOUNCEMENT + "### Fixed\n\n" + entry))
        assert r.returncode != 0
        assert "- Short." in r.stderr

    def test_more_than_ten_upgrade_entries(self, tmp_path):
        prose = ANNOUNCEMENT.split("**Before you upgrade.**")[0]
        items = "".join(f"- Do thing {i}.\n" for i in range(11))
        r = _run(tmp_path, _changelog(prose + "**Before you upgrade.**\n\n" + items + "\n" + CANONICAL_SECTIONS))
        assert r.returncode != 0
        assert "11 entries" in r.stderr

    def test_a_section_over_the_size_limit(self, tmp_path):
        bullets = "".join(f"- Entry {i} with a few words in it.\n" for i in range(700))
        r = _run(tmp_path, _changelog(ANNOUNCEMENT + "### Fixed\n\n" + bullets))
        assert r.returncode != 0
        assert "characters; keep it under 20000" in r.stderr

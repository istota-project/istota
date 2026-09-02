"""The Linux tier's shell probes, run under /bin/sh on whatever host this is.

The tier those probes belong to needs a real kernel and a working bubblewrap,
so nobody developing on macOS can run it — which means a probe that is simply
*wrong* ships green and turns the tier red on the first host that executes it.
Stage 1 of this spec shipped exactly that: a marker built as

    cat <missing> | tr -d "\\n" | sed -e "s/^/LABEL_BODY=/" -e "s/$/;/"

emits nothing at all when the file is absent, because sed is line-oriented and
zero input lines produce zero output lines. The absent case was the whole
assertion, so the marker never printed for any label.

That failure mode is silence, and silence is what an absence test looks for.
So every probe the tier renders is exercised here, in the default suite,
against **both** arms — a present target and an absent one — with only two
requirements: the marker is printed either way, and the two arms differ. That
is not a test of the namespace and does not pretend to be; it is a test that
the sentence the namespace will be asked to answer is a sentence at all.

This file runs the probes with `/bin/sh`, which is what the tools inside the
sandbox reduce to for these constructs (the real invocation is `bash -o
pipefail -c`, a superset). Anything that needs bash would be a finding.
"""

import subprocess
from pathlib import Path

import pytest

from tests.linux import test_tool_server_lifecycle as lifecycle
from tests.linux import test_tool_server_network as network
from tests.linux import test_tool_server_real as real


def _sh(script: str) -> str:
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=30,
    )
    return done.stdout


@pytest.fixture
def present(tmp_path):
    d = tmp_path / "present"
    d.mkdir()
    (d / "file.txt").write_text("hello\n")
    return d


@pytest.fixture
def absent(tmp_path):
    return tmp_path / "absent"


class TestPresenceProbe:
    def test_it_labels_both_arms(self, present, absent):
        out = _sh(real.presence_probe({
            "HERE": present / "file.txt",
            "GONE": absent / "file.txt",
        }))
        assert "HERE=PRESENT" in out, out
        assert "GONE=ABSENT" in out, out

    def test_the_two_arms_differ(self, present, absent):
        """The half that catches a probe printing the same thing regardless —
        which is what an absence assertion cannot detect on its own."""
        here = _sh(real.presence_probe({"X": present / "file.txt"}))
        gone = _sh(real.presence_probe({"X": absent / "file.txt"}))
        assert here != gone

    def test_a_path_with_a_space_is_quoted(self, tmp_path):
        odd = tmp_path / "a dir"
        odd.mkdir()
        (odd / "f").write_text("x")
        assert "X=PRESENT" in _sh(real.presence_probe({"X": odd / "f"}))


class TestListingProbe:
    def test_a_populated_directory_lists_its_entries(self, present):
        out = _sh(real.listing_probe(present))
        assert "ENTRIES=[" in out and "file.txt" in out, out

    def test_an_empty_directory_is_distinguishable_from_a_missing_one(
        self, tmp_path, absent,
    ):
        """The distinction the database-mask assertion rests on: a tmpfs mask
        is present-and-empty and an unbound path is absent, and a probe
        reporting both as an empty list would let one pass for the other."""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert "ENTRIES=[]" in _sh(real.listing_probe(empty))
        assert "ENTRIES=[MISSING]" in _sh(real.listing_probe(absent))

    def test_it_prints_a_marker_on_every_arm(self, present, tmp_path, absent):
        for target in (present, tmp_path / "empty2", absent):
            assert "ENTRIES=[" in _sh(real.listing_probe(target))


class TestWriteProbe:
    def test_both_arms_print(self, present, absent):
        assert "WRITE=OK" in _sh(real.write_probe(present / "new.txt"))
        assert "WRITE=FAIL" in _sh(real.write_probe(absent / "nope" / "new.txt"))


class TestLifecycleProbes:
    def test_the_background_probe_reports_a_pid_and_writes(self, tmp_path):
        marker = tmp_path / "bg.txt"
        out = _sh(lifecycle.background_probe(marker) + "; sleep 0.6")
        assert "BGPID=" in out, out
        assert int(out.split("BGPID=")[1].split()[0]) > 0
        assert marker.exists(), "the background writer never wrote"
        # Nothing here reaps it; the tier's own kill paths do. Clean up so a
        # developer machine is not left with a loop per run.
        subprocess.run(
            ["pkill", "-f", str(marker)], capture_output=True, check=False,
        )

    def test_the_read_back_probe_labels_both_arms(self, tmp_path):
        marker = tmp_path / "m.txt"
        assert "MARKER=MISSING" in _sh(lifecycle.read_back_probe(marker))
        marker.write_text("kept")
        assert "MARKER=kept" in _sh(lifecycle.read_back_probe(marker))


class TestNetworkProbes:
    def test_the_env_probe_separates_unset_from_set_and_empty(self):
        """`NO_PROXY` is legitimately the empty string, so `NAME=` alone is
        ambiguous. This is the one probe whose *whole point* is a distinction
        the obvious spelling cannot make."""
        script = network.env_probe(["A", "B", "C"])
        out = _sh('A=value; B=""; export A B; ' + script)
        assert "A=[value]" in out, out
        assert "B=[]" in out, out
        assert "C=UNSET" in out, out

    def test_the_fetch_probe_labels_a_refusal(self):
        """No network is used: 192.0.2.1 is TEST-NET-1 (RFC 5737), which
        routes nowhere, and `--max-time` is what turns that into a labelled
        refusal rather than a hang."""
        out = _sh(network.fetch_probe("https://192.0.2.1/", "X", max_time=2))
        assert "X=REFUSED" in out, out

    def test_the_fetch_probe_labels_a_success(self, tmp_path):
        """The other arm, against a `file://` URL so this needs no network
        either. Without it a probe that printed REFUSED unconditionally would
        satisfy the tier's refusal assertion and quietly fail its control."""
        target = tmp_path / "page.html"
        target.write_text("<html></html>")
        out = _sh(network.fetch_probe(target.as_uri(), "X"))
        assert "X=OK" in out, out


def test_every_probe_renderer_is_covered_here():
    """A probe added to the tier and not exercised here is the defect this
    file exists for, so the list is checked rather than assumed."""
    renderers = set()
    for module in (real, lifecycle, network):
        for name, value in vars(module).items():
            if name.endswith("_probe") and callable(value):
                renderers.add(f"{module.__name__.rsplit('.', 1)[1]}.{name}")
    assert renderers == {
        "test_tool_server_real.presence_probe",
        "test_tool_server_real.listing_probe",
        "test_tool_server_real.write_probe",
        "test_tool_server_real.parent_env_probe",
        "test_tool_server_lifecycle.background_probe",
        "test_tool_server_lifecycle.read_back_probe",
        "test_tool_server_network.env_probe",
        "test_tool_server_network.fetch_probe",
    }


def test_curl_is_available_or_the_network_probe_tests_would_be_vacuous():
    """`fetch_probe` reduces to `curl … || echo REFUSED`, so on a host with no
    curl both arms answer REFUSED and the success control above passes for the
    wrong reason. Named rather than skipped: this file is the guard, and a
    guard that quietly stops guarding is the thing it guards against."""
    from shutil import which

    if which("curl") is None:
        pytest.skip("no curl on this host; the fetch probe cannot be exercised")
    assert Path(which("curl")).exists()


class TestTheParentEnvProbe:
    """ISSUE-390's `/proc/<ppid>/environ` probe, both arms.

    The real path cannot be exercised on this host — macOS has no `/proc` — so
    the rendered script is run with that path rewritten to a file holding a
    NUL-separated environment block, which is the byte shape the kernel serves.
    What that proves is the sentence: the marker prints whether or not the name
    is there, and the two arms differ. Whether the kernel puts the token in
    that file is the tier's question, and the tier has a positive control for
    it.
    """

    def _run(self, tmp_path, body: bytes) -> str:
        environ = tmp_path / "environ"
        environ.write_bytes(body)
        cmdline = tmp_path / "cmdline"
        cmdline.write_bytes(b"python3\x00-m\x00istota.tool_server\x00")
        script = real.parent_env_probe("CLAUDE_CODE_OAUTH_TOKEN")
        script = script.replace("/proc/$PPID/environ", str(environ))
        script = script.replace("/proc/$PPID/cmdline", str(cmdline))
        return _sh(script)

    def test_the_marker_prints_when_the_name_is_absent(self, tmp_path):
        out = self._run(tmp_path, b"PATH=/usr/bin\x00HOME=/home/x\x00")
        assert "PARENT_ENV=ABSENT" in out

    def test_the_marker_prints_when_the_name_is_present(self, tmp_path):
        out = self._run(
            tmp_path, b"PATH=/usr/bin\x00CLAUDE_CODE_OAUTH_TOKEN=sk-fake\x00"
        )
        assert "PARENT_ENV=PRESENT" in out

    def test_a_name_that_merely_ends_with_it_is_not_a_match(self, tmp_path):
        """The grep is anchored, so `X_CLAUDE_CODE_OAUTH_TOKEN` is not the token."""
        out = self._run(tmp_path, b"X_CLAUDE_CODE_OAUTH_TOKEN=sk-fake\x00")
        assert "PARENT_ENV=ABSENT" in out

    def test_it_reports_the_parent_command(self, tmp_path):
        """Without this the absence arm is a fact about an unknown process."""
        out = self._run(tmp_path, b"PATH=/usr/bin\x00")
        assert "istota.tool_server" in out

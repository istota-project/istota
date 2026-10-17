"""The Mac app's contract with the stack it drives.

`desktop/src-tauri/src/stack.rs` holds, between its `// --- contract ---`
markers, every Mac CLI verb and option the app passes, the `istota` commands it
runs through `exec`, and the exit codes it maps to fixed messages. This file
reads that table and holds it to the usage text of `host/mac/istota-stack` and
`host/istota-stack`, to `istota`'s own parser, and to the scripts' documented
exit codes, so a renamed verb or option fails here rather than in the app on a
user's Mac. The Rust side holds the other half: every argv `stack.rs` builds
stays inside the table (`cargo test` in `desktop/src-tauri`).

These read files off disk, which testmon cannot see (`AGENTS.md`); name this
file in the closing run of any change to the two scripts' usage or to stack.rs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from istota import cli

REPO = Path(__file__).resolve().parent.parent
STACK_RS = REPO / "desktop" / "src-tauri" / "src" / "stack.rs"
MAC_CLI = REPO / "host" / "mac" / "istota-stack"
VM_CLI = REPO / "host" / "istota-stack"
TAURI_CONF = REPO / "desktop" / "src-tauri" / "tauri.conf.json"
LIMA_VERSION = REPO / "desktop" / "lima" / "VERSION"


def _contract() -> str:
    text = STACK_RS.read_text()
    start = text.index("// --- contract ---")
    end = text.index("// --- end contract ---")
    return text[start:end]


def _verbs() -> list[tuple[str, str, list[str]]]:
    verbs = []
    for script, name, options in re.findall(r'Verb::(mac|vm)\("([a-z-]+)", &\[([^\]]*)\]\)', _contract()):
        verbs.append((script, name, re.findall(r'"([^"]+)"', options)))
    return verbs


def _istota_commands() -> list[list[str]]:
    block = _contract().split("ISTOTA_COMMANDS", 1)[1].split("];", 1)[0]
    return [re.findall(r'"([^"]+)"', row) for row in re.findall(r"&\[([^\]]+)\]", block)]


def _exit_codes() -> set[int]:
    block = _contract().split("EXIT_MESSAGES", 1)[1]
    return {int(code) for code in re.findall(r"\((\d+), \"", block)}


def _usage_entries(script: Path) -> dict[str, str]:
    """Each verb's usage entry: its `istota-stack <verb>` line and the
    indented lines under it, joined; a verb with several entries gets all."""
    lines = []
    for line in script.read_text().splitlines()[1:]:
        if line == "# --- end usage ---":
            break
        lines.append(re.sub(r"^# ?", "", line))
    entries: dict[str, str] = {}
    current: list[str] = []
    for line in lines:
        match = re.match(r"^\s+istota-stack ([a-z][a-z-]*)", line)
        if match:
            current = [match.group(1)]
            # `up | down`, `allow-host-port N | deny-host-port N`
            current += re.findall(r"\|\s*(?:istota-stack )?([a-z][a-z-]*)", line)
            for verb in current:
                entries[verb] = entries.get(verb, "") + "\n" + line
        elif current and line.startswith("    ") and line.strip():
            for verb in current:
                entries[verb] += "\n" + line
        else:
            current = []
    return entries


def _mac_dispatched() -> set[str]:
    """The verbs the Mac script's `main` handles itself; the rest it forwards."""
    text = MAC_CLI.read_text()
    body = text[text.index("\nmain() {"):]
    body = body[: body.index("\n}\n")]
    verbs = set()
    for pattern in re.findall(r"^\s+([a-z|-]+)\)", body, flags=re.M):
        verbs.update(pattern.split("|"))
    return verbs


def _mentions(text: str, option: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text) is not None


class TestTheTableIsReadable:
    def test_the_table_names_verbs_commands_and_codes(self):
        assert len(_verbs()) >= 4
        assert ["web", "login-code"] in _istota_commands()
        assert {64, 78} <= _exit_codes()


class TestMacVerbs:
    @pytest.mark.parametrize(
        "name,options", [(n, o) for s, n, o in _verbs() if s == "mac"], ids=lambda v: str(v)
    )
    def test_the_mac_cli_documents_the_verb_and_its_options(self, name, options):
        entries = _usage_entries(MAC_CLI)
        assert name in entries, f"host/mac/istota-stack's usage has no `istota-stack {name}`"
        documented = entries[name]
        # `restore` and `import-standalone` take "[create's options]".
        if "[create's options]" in documented:
            documented += entries["create"]
        for option in options:
            assert _mentions(documented, option), f"`istota-stack {name}` documents no {option}"

    def test_create_s_options_are_what_restore_and_import_parse(self):
        # The usage's "[create's options]" is held to what each verb's parser
        # passes on, for the options the app gives them.
        text = MAC_CLI.read_text()
        for function in ("cmd_restore", "cmd_import_standalone"):
            body = text[text.index(f"\n{function}() {{"):]
            body = body[: body.index("\n}\n")]
            for option in ("--appliance", "--workspace"):
                assert _mentions(body, option), f"{function} parses no {option}"

    @pytest.mark.parametrize("name", [n for s, n, _ in _verbs() if s == "mac"])
    def test_the_mac_cli_handles_the_verb_itself(self, name):
        assert name in _mac_dispatched()


class TestForwardedVerbs:
    @pytest.mark.parametrize(
        "name,options", [(n, o) for s, n, o in _verbs() if s == "vm"], ids=lambda v: str(v)
    )
    def test_the_vm_cli_documents_the_verb_and_the_mac_forwards_it(self, name, options):
        assert name not in _mac_dispatched(), f"the Mac script handles {name} itself"
        entries = _usage_entries(VM_CLI)
        assert name in entries, f"host/istota-stack's usage has no `istota-stack {name}`"
        for option in options:
            assert _mentions(entries[name], option), f"`istota-stack {name}` documents no {option}"


class TestIstotaCommands:
    @pytest.fixture
    def help_of(self, monkeypatch, capsys):
        def run(words: list[str]) -> tuple[int, str]:
            monkeypatch.setattr("sys.argv", ["istota", *words, "--help"])
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            return code, capsys.readouterr().out

        return run

    @pytest.mark.parametrize("command", _istota_commands(), ids=" ".join)
    def test_istota_has_the_command_and_its_options(self, command, help_of):
        words = [w for w in command if not w.startswith("-")]
        options = [w for w in command if w.startswith("-")]
        code, out = help_of(words)
        assert code == 0, f"`istota {' '.join(words)}` is not a command"
        for option in options:
            assert _mentions(out, option), f"`istota {' '.join(words)}` takes no {option}"


class TestExitCodes:
    def test_every_documented_exit_code_has_a_message(self):
        mac = MAC_CLI.read_text()
        documented = {int(code) for code in re.findall(r"^EX_[A-Z]+=(\d+)$", mac, flags=re.M)}
        refused_tag = re.search(r"^EXIT_REFUSED_TAG=(\d+)$", VM_CLI.read_text(), flags=re.M)
        assert documented, "no EX_* codes in host/mac/istota-stack"
        assert refused_tag
        documented.add(int(refused_tag.group(1)))
        assert documented <= _exit_codes(), sorted(documented - _exit_codes())


class TestTheBundle:
    def test_every_bundled_resource_exists(self):
        resources = json.loads(TAURI_CONF.read_text())["bundle"]["resources"]
        source_dir = TAURI_CONF.parent
        for source, target in resources.items():
            assert (source_dir / source).is_file(), source
        assert resources["../../host/mac/istota-stack"] == "bin/istota-stack"
        # The Mac CLI finds its template at ../lima/istota.yaml beside itself.
        assert resources["../../host/lima/istota.yaml"] == "lima/istota.yaml"

    def test_lima_and_zstd_are_pinned_by_digest(self):
        pins = dict(
            line.split("=", 1)
            for line in LIMA_VERSION.read_text().splitlines()
            if line and not line.startswith("#")
        )
        for name in ("LIMA", "ZSTD"):
            assert re.fullmatch(r"\d+\.\d+\.\d+", pins[f"{name}_VERSION"])
            assert re.fullmatch(r"[0-9a-f]{64}", pins[f"{name}_SOURCE_SHA256"])
            assert pins[f"{name}_VERSION"] in pins[f"{name}_SOURCE_URL"]
        assert pins["MACOSX_DEPLOYMENT_TARGET"] == "14.0"

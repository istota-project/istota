"""`istota-stack push-key` on a real VM (#948).

The daemon mounts `/data/config` read-only, so the key can only be written by
a one-shot container with the config directory open. This runs the verb the
operator's way and reads the result off the host: the files, their owner and
modes, the refusal without `--force`, and a key imported on stdin.
"""

from __future__ import annotations

import pytest

from . import lima

pytestmark = pytest.mark.vm

CONFIG = f"{lima.STACK}/config/config.toml"
PUSH_DIR = f"{lima.STACK}/config/push"
KEY = f"{PUSH_DIR}/signing.pem"
SAVED = f"{lima.SCRATCH}/push-key-saved"


@pytest.fixture(scope="module")
def stack(local_stack):
    local_stack.run(f"""
rm -rf {SAVED}
install -d -m 0700 {SAVED}
cp -p {CONFIG} {SAVED}/config.toml
if [ -e {PUSH_DIR} ]; then cp -a {PUSH_DIR} {SAVED}/push; fi
rm -rf {PUSH_DIR}
python3 - {CONFIG} <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(r"^signing_key_path\\s*=.*\\n", "", text, flags=re.M)
line = 'signing_key_path = "/data/config/push/signing.pem"'
if re.search(r"^\\[push\\]$", text, flags=re.M):
    text = re.sub(r"^\\[push\\]$", "[push]\\n" + line, text, count=1, flags=re.M)
else:
    text += "\\n[push]\\n" + line + "\\n"
open(path, "w").write(text)
PY
""")
    try:
        yield local_stack
    finally:
        local_stack.run(f"""
rm -rf {PUSH_DIR}
if [ -e {SAVED}/push ]; then cp -a {SAVED}/push {PUSH_DIR}; fi
cp -p {SAVED}/config.toml {CONFIG}
rm -rf {SAVED}
""")


def _public(vm: lima.Vm, path: str) -> str:
    return vm.out(f"openssl pkey -in {path} -pubout")


class TestPushKey:
    def test_create_writes_the_pair_as_the_daemons_uid(self, stack):
        result = stack.run("istota-stack push-key create < /dev/null")
        output = result.stdout + result.stderr
        assert "Key ID" in output, output
        assert "PRIVATE KEY" not in output
        assert stack.out(f"stat -c '%u %a' {KEY}") == "10001 600"
        assert stack.out(f"stat -c '%u %a' {KEY}.pub") == "10001 644"
        assert stack.out(f"stat -c '%u %a' {PUSH_DIR}") == "10001 700"

    def test_create_refuses_an_existing_key_without_force(self, stack):
        before = stack.out(f"sha256sum {KEY}")
        result = stack.run("istota-stack push-key create < /dev/null", check=False)
        assert result.returncode != 0
        assert "--force" in result.stderr + result.stdout
        assert stack.out(f"sha256sum {KEY}") == before

    def test_import_takes_the_key_on_stdin(self, stack):
        source = f"{lima.SCRATCH}/push-key-import.pem"
        stack.run(f"openssl genpkey -algorithm ed25519 -out {source} && chmod 0600 {source}")
        try:
            refused = stack.run(f"istota-stack push-key import < {source}", check=False)
            assert refused.returncode != 0
            assert _public(stack, KEY) != _public(stack, source)

            result = stack.run(f"istota-stack push-key import --force < {source}")
            output = result.stdout + result.stderr
            assert "Key ID" in output and "PRIVATE KEY" not in output, output
            assert _public(stack, KEY) == _public(stack, source)
            assert stack.out(f"stat -c '%u %a' {KEY}") == "10001 600"
        finally:
            stack.run(f"rm -f {source}")

    def test_import_refuses_what_is_not_an_ed25519_key(self, stack):
        before = stack.out(f"sha256sum {KEY}")
        result = stack.run("echo not-a-key | istota-stack push-key import --force", check=False)
        assert result.returncode != 0
        assert "not-a-key" not in result.stdout + result.stderr
        assert stack.out(f"sha256sum {KEY}") == before

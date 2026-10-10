#!/usr/bin/env bash
# The mutations are literal script text, single-quoted on purpose.
# shellcheck disable=SC2016,SC1003
# Negative controls for host/istota-stack: each breaks one property in a copy of
# the script and requires the tests that witness it to turn red.
#
#   scripts/test-stack-negative-control.sh            # both row 13 controls
#   scripts/test-stack-negative-control.sh skip-verify
#   scripts/test-stack-negative-control.sh user-keyring
#   scripts/test-stack-negative-control.sh no-drop    # row 5's wrapper half, image tier
#   scripts/test-stack-negative-control.sh rollback   # the upgrade tier's rollback case
#
# Row 13 (release integrity) runs in the default suite against a fixture
# repository: tests/test_istota_stack.py. The broken copy is handed over in
# ISTOTA_STACK_SCRIPT. A control passes only when every named node id appears on
# a FAILED line of pytest's summary, so an unrelated failure cannot stand in.
#
# no-drop runs the image tier's wrapper witness (tests/image/test_stack_wrapper.py)
# against a copy whose exec path skips `istota-drop`, so it needs Docker and a
# built image, and takes minutes.
set -euo pipefail

cd "$(dirname "$0")/.."
SCRIPT=host/istota-stack
mode="${1:-row13}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

ROW13=tests/test_istota_stack.py::TestARefusedTagChangesNothing

mutate() {
    local expression="$2" copy="$work/istota-stack.$1"
    python3 - "$SCRIPT" "$copy" "$expression" <<'PY'
import re, sys
source, dest, expression = sys.argv[1], sys.argv[2], sys.argv[3]
old, new = expression.split("\n=>\n")
text = open(source).read()
if old not in text:
    sys.exit(f"control mutation does not apply: {old!r}")
open(dest, "w").write(text.replace(old, new, 1))
PY
    echo "$copy"
}

require_red() {
    local label="$1" output="$2"
    shift 2
    local missing=0 node
    for node in "$@"; do
        if ! grep -Fq "FAILED ${node}" "$output"; then
            echo "[control] ${label} did not turn ${node} red" >&2
            missing=1
        fi
    done
    if [ "$missing" -ne 0 ]; then
        tail -40 "$output" >&2
        exit 1
    fi
    echo "[control] OK: ${label} turned every named witness red"
}

run_row13() {
    local label="$1" copy="$2"
    local output="$work/${label}.out"
    shift 2
    ISTOTA_STACK_SCRIPT="$copy" uv run pytest "$ROW13" -q --no-header -rf -p no:randomly -n0 \
        > "$output" 2>&1 || true
    require_red "$label" "$output" "$@"
}

skip_verify() {
    # Verification gone: whatever the ref names is deployed.
    local copy
    copy="$(mutate skip-verify 'verify_release_tag() {
    local tag="$1" object kind named
=>
verify_release_tag() {
    verified_git rev-parse "refs/tags/$1^{}" 2>/dev/null || verified_git rev-parse "$1"; return
    local tag="$1" object kind named')"
    run_row13 skip-verify "$copy" \
        "${ROW13}::test_an_unsigned_tag_is_refused" \
        "${ROW13}::test_a_lightweight_tag_is_refused" \
        "${ROW13}::test_a_tag_signed_by_another_key_is_refused" \
        "${ROW13}::test_a_branch_name_is_refused" \
        "${ROW13}::test_another_key_in_the_users_own_git_config_is_not_trusted" \
        "${ROW13}::test_a_tag_ref_naming_another_releases_object_is_refused"
}

user_keyring() {
    # Verify against whatever the operator's own git configuration trusts.
    local copy
    copy="$(mutate user-keyring 'GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -C "$SRC" \
        -c gpg.format=ssh \
        -c "gpg.ssh.allowedSignersFile=${ALLOWED_SIGNERS}" \
=>
git -C "$SRC" \
        -c gpg.format=ssh \')"
    run_row13 user-keyring "$copy" \
        "${ROW13}::test_another_key_in_the_users_own_git_config_is_not_trusted"
}

no_drop() {
    local copy output="$work/no-drop.out"
    copy="$(mutate no-drop '${ISTOTA_STACK_EXEC} istota-drop "$@"
=>
${ISTOTA_STACK_EXEC} "$@"')"
    ISTOTA_STACK_SCRIPT="$copy" uv run pytest -m image -n0 tests/image/test_stack_wrapper.py \
        -q --no-header -rf -p no:randomly > "$output" 2>&1 || true
    require_red no-drop "$output" \
        "tests/image/test_stack_wrapper.py::TestTheVmWrapperExecsAsTheDaemon::test_the_wrapper_runs_as_10001_with_no_capabilities" \
        "tests/image/test_stack_wrapper.py::TestTheVmWrapperExecsAsTheDaemon::test_nothing_under_data_is_root_owned_after_wrapper_calls"
}

rollback() {
    # The upgrade tier's rollback case against an image that never refuses.
    local base output="$work/rollback.out" node=tests/image/test_upgrade.py::TestAnOlderImageRefusesANewerSchema
    base="$(docker images --format '{{.Repository}}:{{.Tag}}' istota-test/istota | head -1)"
    [ -n "$base" ] || { echo "no istota-test/istota image yet: run the image tier first" >&2; exit 2; }
    docker build -q -f docker/test/Dockerfile.no-schema-refusal --build-arg "BASE=$base" \
        -t istota-test/no-schema-refusal:control docker/test >/dev/null
    ISTOTA_IMAGE_TAG=istota-test/no-schema-refusal:control uv run pytest -m image -n0 \
        "$node" -q --no-header -rf -p no:randomly > "$output" 2>&1 || true
    require_red rollback "$output" \
        "${node}::test_the_boot_exits_with_the_schema_refusal" \
        "${node}::test_the_database_keeps_the_newer_stamp" \
        "${node}::test_the_rollback_preflight_says_so_without_writing"
}

case "$mode" in
    row13) skip_verify; user_keyring ;;
    rollback) rollback ;;
    skip-verify) skip_verify ;;
    user-keyring) user_keyring ;;
    no-drop) no_drop ;;
    *) echo "unknown control: $mode" >&2; exit 64 ;;
esac

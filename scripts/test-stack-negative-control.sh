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
#   scripts/test-stack-negative-control.sh setup-web  # the three setup --web controls
#   scripts/test-stack-negative-control.sh setup-unmarked
#   scripts/test-stack-negative-control.sh setup-mint-in-staging
#   scripts/test-stack-negative-control.sh setup-no-lock
#
# Row 13 (release integrity) runs in the default suite against a fixture
# repository: tests/test_istota_stack.py. The broken copy is handed over in
# ISTOTA_STACK_SCRIPT. A control passes only when every named node id appears on
# a FAILED line of pytest's summary, so an unrelated failure cannot stand in.
#
# The setup --web controls run the same file's setup classes, and pass only when
# the FAILED and ERROR lines are exactly the named node ids: each breaks one
# property, and a neighbour turning red for the wrong reason fails the control.
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
STACK_TESTS=tests/test_istota_stack.py
SETUP_WEB=(
    "${STACK_TESTS}::TestSetupWebRefuses"
    "${STACK_TESTS}::TestSetupWebMints"
    "${STACK_TESTS}::TestSetupWebLocal"
    "${STACK_TESTS}::TestSetupWebUnfinished"
    "${STACK_TESTS}::TestSetupWebFinished"
    "${STACK_TESTS}::TestSetupCode"
)

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
    chmod +x "$copy"
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

require_exactly_red() {
    local label="$1" output="$2"
    shift 2
    local expected actual
    expected="$(printf '%s\n' "$@" | sort -u)"
    actual="$(sed -n -E 's/^(FAILED|ERROR) ([^ ]+).*/\2/p' "$output" | sort -u)"
    if [ "$expected" != "$actual" ]; then
        echo "[control] ${label} did not turn exactly its witnesses red" >&2
        diff <(echo "$expected") <(echo "$actual") | sed -n 's/^</  still green: /p; s/^>/  also red:    /p' >&2 || true
        tail -40 "$output" >&2
        exit 1
    fi
    echo "[control] OK: ${label} turned exactly its named witnesses red ($#)"
}

run_setup_web() {
    local label="$1" copy="$2"
    local output="$work/${label}.out"
    shift 2
    ISTOTA_STACK_SCRIPT="$copy" uv run pytest "${SETUP_WEB[@]}" -q --no-header -rfE -p no:randomly -n0 --color=no \
        > "$output" 2>&1 || true
    require_exactly_red "$label" "$output" "$@"
}

run_row13() {
    local label="$1" copy="$2"
    local output="$work/${label}.out"
    shift 2
    ISTOTA_STACK_SCRIPT="$copy" uv run pytest "$ROW13" -q --no-header -rf -p no:randomly -n0 --color=no \
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

setup_unmarked() {
    # Whatever staging holds is installed, setup-done or not.
    local copy
    copy="$(mutate setup-unmarked '    if [ ! -f "${staging}/setup-done" ] || [ -L "${staging}/setup-done" ]; then
        setup_did_not_finish "$rc"
    fi
=>
')"
    run_setup_web setup-unmarked "$copy" \
        "${STACK_TESTS}::TestSetupWebUnfinished::test_a_failed_run_changes_neither_env_file_and_cleans_up" \
        "${STACK_TESTS}::TestSetupWebUnfinished::test_a_clean_exit_without_the_marker_is_still_a_failure" \
        "${STACK_TESTS}::TestSetupWebUnfinished::test_a_timeout_says_so_and_keeps_its_code" \
        "${STACK_TESTS}::TestSetupWebUnfinished::test_a_lockout_says_to_run_again_and_keeps_its_code" \
        "${STACK_TESTS}::TestSetupWebUnfinished::test_a_secret_written_before_the_failure_is_named" \
        "${STACK_TESTS}::TestSetupWebFinished::test_a_linked_marker_is_not_a_finished_setup"
}

setup_mint_in_staging() {
    # The code, certificate and key minted straight into staging, which is
    # handed to uid 10001, rather than into root's .setup/. Root gets copies
    # back, so printing and setup-code still work and the witness is the
    # staged files' mode, not a file missing from .setup/.
    local copy
    copy="$(mutate setup-mint-in-staging '    mint_setup_files "$publish" "$local_mode"
    new_staging
    install -m 0400 "${SETUP_DIR}/code" "${staging}/setup-code"
    if [ -z "$local_mode" ]; then
        install -m 0400 "${SETUP_DIR}/cert.pem" "${staging}/cert.pem"
        install -m 0400 "${SETUP_DIR}/key.pem" "${staging}/key.pem"
    fi
=>
    new_staging
    SETUP_DIR="$staging" mint_setup_files "$publish" "$local_mode"
    mv "${staging}/code" "${staging}/setup-code"
    cp "${staging}/setup-code" "${SETUP_DIR}/code"
    for f in instructions cert.pem key.pem; do
        if [ -e "${staging}/${f}" ]; then cp "${staging}/${f}" "${SETUP_DIR}/${f}"; fi
    done
')"
    run_setup_web setup-mint-in-staging "$copy" \
        "${STACK_TESTS}::TestSetupWebMints::test_the_code_and_certificate_are_root_s_and_the_copies_read_only"
}

setup_no_lock() {
    # No lock taken: a second run goes ahead beside the first, and setup-code,
    # which answers only while the lock is held, says no setup is running.
    local copy
    copy="$(mutate setup-no-lock '    setup_lock take || die "a setup is already running: \`istota-stack setup-code\` prints its address."
=>
')"
    run_setup_web setup-no-lock "$copy" \
        "${STACK_TESTS}::TestSetupWebRefuses::test_a_second_run_is_refused_while_the_first_serves" \
        "${STACK_TESTS}::TestSetupCode::test_during_a_setup_it_prints_the_instructions"
}

no_drop() {
    local copy output="$work/no-drop.out"
    copy="$(mutate no-drop '${ISTOTA_STACK_EXEC} istota-drop "$@"
=>
${ISTOTA_STACK_EXEC} "$@"')"
    ISTOTA_STACK_SCRIPT="$copy" uv run pytest -m image -n0 tests/image/test_stack_wrapper.py \
        -q --no-header -rf -p no:randomly > "$output" 2>&1 || true
    # Only the uid witness: a root CLI call on this volume reuses the daemon's
    # existing database sidecars, so the root-owned sweep has nothing new to
    # find and stays green under this control. It fails closed (it can only
    # report files that exist).
    require_red no-drop "$output" \
        "tests/image/test_stack_wrapper.py::TestTheVmWrapperExecsAsTheDaemon::test_the_wrapper_runs_as_10001_with_no_capabilities"
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
    setup-web) setup_unmarked; setup_mint_in_staging; setup_no_lock ;;
    setup-unmarked) setup_unmarked ;;
    setup-mint-in-staging) setup_mint_in_staging ;;
    setup-no-lock) setup_no_lock ;;
    *) echo "unknown control: $mode" >&2; exit 64 ;;
esac

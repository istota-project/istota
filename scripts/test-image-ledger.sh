# Sourced by a driver that builds images across several steps and pytest sessions.
# Every tag it or a session builds goes in one ledger, and the driver calls
# remove_ledger_images when it ends. testbed/images.py is the session half.
#
# A function named docker, so that no build site can be missed. It reaches a
# command substitution and a subshell, and a child process has the ledger variable.

ISTOTA_TEST_IMAGE_LEDGER="$(mktemp "${TMPDIR:-/tmp}/istota-image-ledger.XXXXXX")"
export ISTOTA_TEST_IMAGE_LEDGER

docker() {
    if [ "${1:-}" = "build" ]; then
        local previous="" argument
        for argument in "$@"; do
            if [ "$previous" = "-t" ]; then
                printf '%s\n' "$argument" >> "$ISTOTA_TEST_IMAGE_LEDGER"
            fi
            previous="$argument"
        done
    fi
    command docker "$@"
}

remove_ledger_images() {
    local tag
    case "$(printf '%s' "${ISTOTA_KEEP_TEST_IMAGES:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            echo "Kept the images this run built (ISTOTA_KEEP_TEST_IMAGES):" >&2
            sort -u "$ISTOTA_TEST_IMAGE_LEDGER" >&2
            ;;
        *)
            while IFS= read -r tag; do
                command docker image rm "$tag" >/dev/null 2>&1 \
                    || echo "Could not remove the image $tag" >&2
            done < <(sort -u "$ISTOTA_TEST_IMAGE_LEDGER")
            # testbed/images.py says why by age, and holds the same value.
            if [ -s "$ISTOTA_TEST_IMAGE_LEDGER" ]; then
                command docker builder prune --all --force --filter until=48h >/dev/null 2>&1 \
                    || echo "Could not trim the build cache" >&2
            fi
            ;;
    esac
    rm -f "$ISTOTA_TEST_IMAGE_LEDGER"
}

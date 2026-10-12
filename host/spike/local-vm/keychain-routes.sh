#!/bin/bash
# local-vm-install Stage 1 (f), third half: two routes that keep the value off
# argv when the caller has a controlling TTY, each run under script(1) so the
# TTY is there. Throwaway values; every item is deleted. Run on the Mac.
#   6. setsid (through perl's POSIX) before `security add-generic-password -w`:
#      no controlling TTY, so security falls back to stdin.
#   7. `security -i`: the command, value included, read from stdin.
#   8. route 6 with the value once only: does a short read store anything?
set -uo pipefail
SERVICE="istota.local.spike-probe"
ACCOUNT="secret-key-spike-routes-$$"
VALUE="spike-throwaway-$(date +%s)-not-a-secret"
export SERVICE ACCOUNT VALUE

cleanup() { security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
readback() {
    local got present
    got="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
    present="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo yes || echo no)"
    echo "read back: $([ "$got" = "$VALUE" ] && echo matches || echo "differs (len ${#got})") present=$present"
}

echo "== 6. setsid, value twice on the pipe, under a controlling pty"
script -q /dev/null perl -e 'alarm 15; exec @ARGV' bash -c \
    'printf "%s\n%s\n" "$VALUE" "$VALUE" | perl -MPOSIX -e "POSIX::setsid(); exec @ARGV" security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w >/dev/null 2>&1; echo "inner exit=$?"' </dev/null
readback
cleanup

echo "== 7. security -i, under a controlling pty"
script -q /dev/null perl -e 'alarm 15; exec @ARGV' bash -c \
    'printf "add-generic-password -s %s -a %s -w %s\n" "$SERVICE" "$ACCOUNT" "$VALUE" | security -i; echo "inner exit=$?"' </dev/null
readback
cleanup

echo "== 8. setsid, value once"
script -q /dev/null perl -e 'alarm 15; exec @ARGV' bash -c \
    'printf "%s\n" "$VALUE" | perl -MPOSIX -e "POSIX::setsid(); exec @ARGV" security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w >/dev/null 2>&1; echo "inner exit=$?"' </dev/null
readback
cleanup
echo "== after cleanup: $(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo "item still there" || echo "no item")"

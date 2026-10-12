#!/bin/bash
# local-vm-install Stage 1 (f), second half: the Mac CLI runs from a terminal,
# so `security` has a controlling TTY while its stdin is a pipe. Does it still
# read the pipe, or open /dev/tty? And a JXA fallback without the
# synchronizable attribute. Throwaway values; every item is deleted. Run on
# the Mac.
set -uo pipefail
SERVICE="istota.local.spike-probe"
ACCOUNT="secret-key-spike-tty-$$"
VALUE="spike-throwaway-$(date +%s)-not-a-secret"
export SERVICE ACCOUNT VALUE

cleanup() { security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
readback() {
    local got
    got="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
    echo "read back: $([ "$got" = "$VALUE" ] && echo matches || echo "differs (len ${#got}, present=$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo yes || echo no))")"
}

echo "== 4. under script(1): a controlling pty, stdin of security a pipe"
# The pty gets no input; if security reads /dev/tty it waits until perl's alarm.
script -q /dev/null perl -e 'alarm 15; exec @ARGV' bash -c \
    'printf "%s\n%s\n" "$VALUE" "$VALUE" | security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w; echo "inner exit=$?"' </dev/null
echo "script exit=$?"
readback
cleanup

echo "== 5. JXA, no synchronizable attribute"
printf '%s' "$VALUE" | perl -e 'alarm 20; exec @ARGV' osascript -l JavaScript - "$SERVICE" "$ACCOUNT" 3<&0 <<'JXA'
ObjC.import('Foundation');
ObjC.import('Security');
function run(argv) {
    const data = $.NSFileHandle.alloc.initWithFileDescriptor(3).readDataToEndOfFile;
    const query = $.NSMutableDictionary.alloc.init;
    query.setObjectForKey($.kSecClassGenericPassword, $.kSecClass);
    query.setObjectForKey($(argv[0]), $.kSecAttrService);
    query.setObjectForKey($(argv[1]), $.kSecAttrAccount);
    query.setObjectForKey(data, $.kSecValueData);
    return 'bytes=' + data.length + ' SecItemAdd=' + $.SecItemAdd(query, null);
}
JXA
echo "exit=$?"
readback
cleanup
echo "== after cleanup: $(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo "item still there" || echo "no item")"

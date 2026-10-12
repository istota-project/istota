#!/bin/bash
# local-vm-install Stage 1 (f): does `security add-generic-password` take the
# value from a pipe when `-w` is the last argument and has no value? Uses a
# throwaway service name and value, and deletes the item afterwards. Run on
# the Mac, with no TTY on stdin.
set -uo pipefail
SERVICE="istota.local.spike-probe"
ACCOUNT="secret-key-spike-$$"
VALUE="spike-throwaway-$(date +%s)-not-a-secret"

# macOS has no timeout(1); perl's alarm bounds a call that would wait on a TTY.
bounded() { perl -e 'alarm shift; exec @ARGV' 20 "$@"; }

cleanup() { security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "stdin is a tty: $([ -t 0 ] && echo yes || echo no)"
echo "== 1. -w last, value on a pipe"
printf '%s\n' "$VALUE" | bounded security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w
echo "exit=$?"
got="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
echo "read back: $([ "$got" = "$VALUE" ] && echo "matches" || echo "differs (len ${#got})")"
cleanup

echo "== 2. -w last, value on a pipe twice (a prompt that asks to retype)"
printf '%s\n%s\n' "$VALUE" "$VALUE" | bounded security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w
echo "exit=$?"
got="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
echo "read back: $([ "$got" = "$VALUE" ] && echo "matches" || echo "differs (len ${#got})")"
cleanup

echo "== 3. osascript JavaScript through the Security framework, value on stdin"
printf '%s' "$VALUE" | bounded osascript -l JavaScript - "$SERVICE" "$ACCOUNT" 3<&0 <<'JXA'
ObjC.import('Foundation');
ObjC.import('Security');
function run(argv) {
    const data = $.NSFileHandle.alloc.initWithFileDescriptor(3).readDataToEndOfFile;
    const query = $.NSMutableDictionary.alloc.init;
    query.setObjectForKey($.kSecClassGenericPassword, $.kSecClass);
    query.setObjectForKey($(argv[0]), $.kSecAttrService);
    query.setObjectForKey($(argv[1]), $.kSecAttrAccount);
    query.setObjectForKey(data, $.kSecValueData);
    query.setObjectForKey($.kCFBooleanFalse, $.kSecAttrSynchronizable);
    return 'SecItemAdd=' + $.SecItemAdd(query, null);
}
JXA
echo "exit=$?"
got="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
echo "read back: $([ "$got" = "$VALUE" ] && echo "matches" || echo "differs (len ${#got})")"
security find-generic-password -s "$SERVICE" -a "$ACCOUNT" 2>&1 | grep -E 'keychain:|"svce"|"acct"' || true
cleanup
echo "== after cleanup: $(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo "item still there" || echo "no item")"

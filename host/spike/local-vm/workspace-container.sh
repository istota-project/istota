#!/bin/bash
# local-vm-install Stage 1 (a), (b): what uid 10001 sees of the Mac folder at
# /mnt/shared inside the istota container. Run by run-workspace.sh as the
# container's entrypoint. Each case prints one CASE line.
set -uo pipefail

W=/mnt/shared
TAG="${PROBE_TAG:-p}"

run_case() {
    local name="$1"; shift
    local out rc
    out="$("$@" 2>&1)"
    rc=$?
    echo "CASE ${name} exit=${rc} out=$(printf '%s' "$out" | head -c 400 | tr '\n' '|')"
}

echo "id: $(id)"
echo "caps: $(grep -E '^Cap(Eff|Prm|Bnd)' /proc/self/status | tr '\n' ' ')"
echo "mountinfo: $(grep " $W " /proc/self/mountinfo)"
echo "fstype: $(stat -f -c %T "$W") / $(awk -v m="$W" '$5==m {for(i=7;i<=NF;i++) if($i=="-"){print $(i+1); exit}}' /proc/self/mountinfo)"

echo "== ownership as reported to the guest"
run_case stat_root stat -c '%n uid=%u gid=%g mode=%a' "$W" "$W/Users" "$W/Users/alice" "$W/Users/alice/inbox" "$W/Users/alice/inbox/mac.txt"
run_case read_mac_file cat "$W/Users/alice/inbox/mac.txt"

echo "== writes as 10001 into a Mac-created tree (dirs 0755, owner the Mac user)"
run_case create_at_root sh -c "echo from-10001 > $W/c-$TAG-root.txt && stat -c 'uid=%u gid=%g mode=%a' $W/c-$TAG-root.txt"
run_case create_in_inbox sh -c "echo from-10001 > $W/Users/alice/inbox/c-$TAG.txt && stat -c 'uid=%u gid=%g mode=%a' $W/Users/alice/inbox/c-$TAG.txt"
run_case mkdir_nested mkdir -p "$W/Users/alice/memories-$TAG/deep"
run_case rename_own mv "$W/Users/alice/inbox/c-$TAG.txt" "$W/Users/alice/inbox/c-$TAG-renamed.txt"
run_case rename_mac_file sh -c "cp $W/Users/alice/inbox/mac.txt $W/Users/alice/inbox/macfile-$TAG.txt 2>&1; ls -ln $W/Users/alice/inbox/"
run_case append_to_mac_file sh -c "echo appended >> $W/Users/alice/inbox/mac.txt && tail -1 $W/Users/alice/inbox/mac.txt"
run_case chmod_own chmod 0600 "$W/Users/alice/inbox/c-$TAG-renamed.txt"
run_case chown_own_to_self chown 10001:10001 "$W/Users/alice/inbox/c-$TAG-renamed.txt"
run_case chown_own_to_other chown 0:0 "$W/Users/alice/inbox/c-$TAG-renamed.txt"
run_case chown_mac_file chown 10001:10001 "$W/Users/alice/inbox/mac.txt"
run_case utime_mac_file touch -d '2020-01-01' "$W/Users/alice/inbox/mac.txt"
run_case sqlite_on_workspace python3 -c "
import sqlite3; c = sqlite3.connect('$W/probe-$TAG.db'); c.execute('pragma journal_mode=wal'); c.execute('create table t(x)'); c.execute('insert into t values (1)'); c.commit(); print(c.execute('select count(*) from t').fetchone())"
run_case xattr_set python3 -c "
import os; p='$W/Users/alice/inbox/c-$TAG-renamed.txt'; os.setxattr(p, 'user.istota', b'1'); print(os.getxattr(p, 'user.istota'))"
run_case delete_own rm -rf "$W/Users/alice/memories-$TAG"
run_case listing ls -lan "$W/Users/alice/inbox"

echo "== case sensitivity"
run_case case_create_upper sh -c "echo upper > $W/Case-$TAG.txt"
run_case case_open_lower cat "$W/case-$TAG.txt"
run_case case_create_lower_distinct sh -c "echo lower > $W/case-$TAG.txt; ls $W | grep -i '^case-$TAG' ; cat $W/Case-$TAG.txt"
run_case case_dir_users_lower ls -d "$W/users" "$W/USERS"

echo "== (b) symlinks the Mac made in the folder"
run_case readlink_abs readlink "$W/abs-link"
run_case read_abs_link cat "$W/abs-link"
run_case read_rel_link cat "$W/rel-link"
run_case read_etc_link cat "$W/etc-link"
run_case dotdot_escape ls "$W/.."

echo "== bwrap with the workspace (the sandbox's per-user subtree bind)"
command -v bwrap >/dev/null && echo "bwrap: $(bwrap --version)"
run_case bwrap_user_subtree_rw bwrap --unshare-user --disable-userns --unshare-pid --unshare-net --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --tmpfs "$W" --bind "$W/Users/alice" "$W/Users/alice" \
    -- /bin/sh -c "ls -A $W; ls -A $W/Users; echo in-sandbox > $W/Users/alice/sb-$TAG.txt && cat $W/Users/alice/sb-$TAG.txt; ls -ln $W/Users/alice/sb-$TAG.txt; cat /proc/self/uid_map"
run_case bwrap_sibling_hidden bwrap --unshare-user --disable-userns --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --tmpfs "$W" --bind "$W/Users/alice" "$W/Users/alice" \
    -- /bin/sh -c "cat $W/Users/alice/inbox/mac.txt; ls $W/abs-link 2>&1; ls $W/Users/bob 2>&1"
run_case bwrap_ro_bind_write_refused bwrap --unshare-user --disable-userns --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --ro-bind "$W/Users/alice" "$W/Users/alice" \
    -- /bin/sh -c "touch $W/Users/alice/ro-$TAG.txt"
run_case bwrap_mask_over_workspace_dir bwrap --unshare-user --disable-userns --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --bind "$W" "$W" --tmpfs "$W/Users/alice/inbox" --remount-ro "$W/Users/alice/inbox" \
    -- /bin/sh -c "ls -A $W/Users/alice/inbox | wc -l; touch $W/Users/alice/inbox/x 2>&1"
run_case sandbox_file_on_mac stat -c 'uid=%u gid=%g' "$W/Users/alice/sb-$TAG.txt"

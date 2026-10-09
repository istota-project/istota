#!/usr/bin/env python3
"""Call bpf, keyctl, add_key, userfaultfd and perf_event_open; print errno.

Each call is made with arguments a kernel that let it through would reject
with something other than EPERM (EFAULT, EINVAL) or accept outright, so the
answer separates "seccomp refused" from "the kernel refused on its own
terms" as far as one process can. The relevant sysctls are printed beside the
results, since kernel.unprivileged_bpf_disabled and perf_event_paranoid make
the kernel itself answer EPERM / EACCES for an unprivileged caller.
"""

import ctypes
import errno
import os
import platform

NR = {
    "x86_64": {"bpf": 321, "keyctl": 250, "add_key": 248, "userfaultfd": 323,
               "perf_event_open": 298},
    "aarch64": {"bpf": 280, "keyctl": 219, "add_key": 217, "userfaultfd": 282,
                "perf_event_open": 241},
}[platform.machine()]

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

KEYCTL_GET_KEYRING_ID = 0
KEY_SPEC_SESSION_KEYRING = -3
UFFD_USER_MODE_ONLY = 1


def call(name: str, *args) -> str:
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(NR[name]), *args)
    if rc >= 0:
        if name == "userfaultfd":
            os.close(rc)
        return f"ok rc={rc}"
    err = ctypes.get_errno()
    return f"{errno.errorcode.get(err, err)}"


def sysctl(path: str) -> str:
    try:
        with open(f"/proc/sys/{path}") as f:
            return f.read().strip()
    except OSError as exc:
        return f"unreadable ({exc.strerror})"


def main() -> None:
    attr = ctypes.create_string_buffer(128)
    print(f"uid={os.getuid()}")
    print(f"  bpf(BPF_MAP_CREATE, zeroed attr): {call('bpf', ctypes.c_int(0), attr, ctypes.c_uint(128))}")
    print(f"  keyctl(GET_KEYRING_ID, session): {call('keyctl', ctypes.c_int(KEYCTL_GET_KEYRING_ID), ctypes.c_int(KEY_SPEC_SESSION_KEYRING), ctypes.c_int(0))}")
    print(f"  add_key(NULL type): {call('add_key', ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_size_t(0), ctypes.c_int(KEY_SPEC_SESSION_KEYRING))}")
    print(f"  userfaultfd(O_CLOEXEC|USER_MODE_ONLY): {call('userfaultfd', ctypes.c_int(os.O_CLOEXEC | UFFD_USER_MODE_ONLY))}")
    print(f"  userfaultfd(O_CLOEXEC): {call('userfaultfd', ctypes.c_int(os.O_CLOEXEC))}")
    print(f"  perf_event_open(NULL attr): {call('perf_event_open', ctypes.c_void_p(0), ctypes.c_int(0), ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_ulong(0))}")
    print(f"  sysctl kernel.unprivileged_bpf_disabled={sysctl('kernel/unprivileged_bpf_disabled')}"
          f" kernel.perf_event_paranoid={sysctl('kernel/perf_event_paranoid')}"
          f" vm.unprivileged_userfaultfd={sysctl('vm/unprivileged_userfaultfd')}")


if __name__ == "__main__":
    main()

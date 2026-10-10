"""Derive the draft istota seccomp profile from Docker's default.

Stage 1 spike. Two changes to Docker's default, nothing else:

1. One unconditional allow rule naming the calls bwrap was measured to need.
   bwrap runs with no capability, and the default allows those calls only to
   a container that holds CAP_SYS_ADMIN (an `includes.caps` rule), with
   `clone` filtered by flag and `clone3` answered ENOSYS otherwise.

2. The `includes.caps: [CAP_SYS_ADMIN]` rule is removed. Docker evaluates
   `includes.caps` against the container's configured capability set when it
   builds the filter, not against the calling process, so with SYS_ADMIN in
   `cap_add` for the root phase that rule would allow `bpf`,
   `perf_event_open`, `fsopen`, `open_tree`, `syslog`, `quotactl` and the rest
   to every process in the container, the dropped daemon and every sandbox
   included. Measured in this spike; see RESULTS.md.

Usage:
  derive_profile.py <docker-default.json> -              print the relevant rules
  derive_profile.py <docker-default.json> <out> [--keep-sysadmin-rule] [syscall ...]
With no syscalls given, the measured set is used.
"""

import json
import sys

# Measured in the spike by removing one name at a time (RESULTS.md). `setns`,
# which the spec's first list named, is not needed by bwrap 0.12.
MEASURED_SET = ["clone", "clone3", "mount", "pivot_root", "umount2", "unshare"]


def is_sysadmin_include_rule(rule: dict) -> bool:
    return (rule.get("includes") or {}).get("caps") == ["CAP_SYS_ADMIN"]


def derive(default: dict, extra: list[str], drop_sysadmin_rule: bool = True) -> dict:
    profile = json.loads(json.dumps(default))
    if drop_sysadmin_rule:
        profile["syscalls"] = [r for r in profile["syscalls"] if not is_sysadmin_include_rule(r)]
    profile["syscalls"].append(
        {
            "names": sorted(extra),
            "action": "SCMP_ACT_ALLOW",
            "comment": "istota: bwrap namespace setup and the root phase's cgroup remount; no capability condition",
        }
    )
    return profile


def bwrap_relevant(default: dict) -> list[dict]:
    wanted = set(MEASURED_SET) | {"setns", "bpf", "keyctl", "add_key", "request_key",
                                  "perf_event_open", "userfaultfd"}
    rules = []
    for rule in default["syscalls"]:
        hit = [n for n in rule["names"] if n in wanted]
        if hit:
            rules.append({
                "names": hit,
                "all_names_in_rule": len(rule["names"]),
                "action": rule["action"],
                "args": rule.get("args"),
                "includes": rule.get("includes"),
                "excludes": rule.get("excludes"),
            })
    return rules


def main() -> int:
    src, out = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    drop = "--keep-sysadmin-rule" not in rest
    extra = [a for a in rest if not a.startswith("--")] or MEASURED_SET
    with open(src) as f:
        default = json.load(f)
    if out == "-":
        for rule in bwrap_relevant(default):
            print(json.dumps(rule))
        return 0
    with open(out, "w") as f:
        json.dump(derive(default, extra, drop_sysadmin_rule=drop), f, indent=2)
        f.write("\n")
    print(f"wrote {out}: extra allow {sorted(extra)}, sysadmin rule {'dropped' if drop else 'kept'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

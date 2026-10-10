# Stage 1 spike results: one deployment shape

Measured 2026-10-09 in a Lima VM (`vz`, arm64, 4 CPUs, 6 GiB) running Debian 13.7, kernel 6.12.111+deb13-cloud-arm64, cgroup v2 with the systemd driver, AppArmor enabled. Docker Engine 29.9.0, Docker Compose v5.6.0, runc 1.5.2, Podman 5.4.2 (rootful), all from Docker's and Debian's apt repositories. The image is the current `docker/istota/Dockerfile` at 5273ca06, patched by `Dockerfile.spike` (uid and gid 10001, `setpriv`, the spike entrypoint). bwrap 0.12.0.

The raw output of every run is in `out/<variant>.log`. `run-probes.sh` and `podman-probes.sh` reproduce them.

## Verdict

The design works on this VM with three changes the spec does not yet carry. One of them is a decision for the spec, so Stage 2 should not start until it is made.

1. **AppArmor blocks bwrap and the cgroup remount (decision needed).** Debian 13 has AppArmor enabled, and Docker confines every container with `docker-default` unless told otherwise. That profile has `deny mount,`. Under it bwrap fails at its first mount (`bwrap: Failed to make / slave: Permission denied`) and the root phase's remount fails with `EACCES` (`out/apparmor-default-skip-cgroup.log`, `out/mountprobe--docker-default.log`). The testbed never saw this because Docker Desktop has no AppArmor. Two routes were measured:
   - `apparmor=unconfined`: everything works, and a uid-0 `docker compose exec` can write VM sysctls (`kernel/core_pattern` written, `out/main-setpcap-aa-unconfined.log`), as the spec already assumes.
   - A shipped AppArmor profile (`apparmor-istota.draft`): `docker-default` with `mount,`, `pivot_root,` and `userns,` allowed and `/proc/sys/user/max_user_namespaces` writable (bwrap `--disable-userns` writes it). Everything works, and `docker-default`'s `/proc/sys` write denial still holds: the same uid-0 exec is refused on `vm/swappiness` and `kernel/core_pattern` (`out/main-setpcap-aa-istota.log`). This closes the sysctl exposure the spec currently accepts, at the cost of one more file `provision.sh` loads with `apparmor_parser -r -W`. `userns,` was needed because a profile compiled against the VM's own feature ABI mediates user namespace creation (`Creating new namespace failed: Permission denied` without it).
2. **`CAP_SETPCAP` must be in `cap_add`.** With the spec's five capabilities, `setpriv --bounding-set=-all` fails with `apply bounding set: Operation not permitted` and the container exits (`out/main-aa-unconfined.log`). Emptying the bounding set needs `CAP_SETPCAP`. With it added, the daemon ends with every capability set empty.
3. **The seccomp profile must drop Docker's `CAP_SYS_ADMIN` rule.** Docker evaluates `includes.caps` against the container's configured capabilities when it builds the filter, not against the calling process. With `SYS_ADMIN` in `cap_add` for the root phase, the default's `CAP_SYS_ADMIN` rule lets `bpf`, `perf_event_open`, the new mount API, `syslog`, `quotactl` and the rest through for every process in the container, the dropped daemon and every sandbox included. Measured: with that rule kept, uid 10001 gets `EINVAL` from `bpf` and `EFAULT` from `perf_event_open` (the kernel saw the call); with it removed, both are `EPERM` (`out/keep-sysadmin-rule.log`, `out/main-setpcap-aa-istota.log`). The draft profile removes the rule.

Two smaller findings change wording in the spec rather than the design:

4. **Row 5's exec-path witness cannot pass as written.** `docker compose exec --user 10001:10001` gives uid 10001 with `CapPrm`, `CapEff`, `CapInh`, `CapAmb` empty and `NoNewPrivs: 1`, but `CapBnd` is the whole `cap_add` set (`CHOWN,FOWNER,SETGID,SETUID,SETPCAP,SYS_ADMIN`). An exec'd process starts from the container's configuration, and a uid-10001 process cannot drop its own bounding set. Either the wrapper does a plain exec through `istota-drop` (the healthcheck's route, which can empty it), or the witness asserts the four other sets plus `NoNewPrivs` and accepts the bounding set. With `no-new-privileges` the bounding set cannot be used: nothing can gain a capability through exec.
5. **Row 16's witness needs specific arguments.** On this kernel `kernel.unprivileged_bpf_disabled=2`, `kernel.perf_event_paranoid=3` and `vm.unprivileged_userfaultfd=0`, so a careless call fails for kernel reasons under any profile. The arguments in `probes/syscalls.py` separate the two: under the draft profile all of them are `EPERM`; under `seccomp=unconfined` they become `bpf(BPF_MAP_CREATE, zeroed attr)` EINVAL, `keyctl(GET_KEYRING_ID)` ok, `add_key(NULL)` EFAULT, `userfaultfd(O_CLOEXEC|UFFD_USER_MODE_ONLY)` ok, `perf_event_open(NULL)` EFAULT (`out/unconfined.log`). Plain `userfaultfd(O_CLOEXEC)` is `EPERM` either way and must not be used.

## The run contract under Docker

Every probe below is from `out/main-setpcap-aa-istota.log`: the draft seccomp profile, `systempaths=unconfined`, `no-new-privileges:true`, `apparmor=istota-spike-default`, `cap_drop: [ALL]`, `cap_add: [CHOWN, FOWNER, SETUID, SETGID, SYS_ADMIN, SETPCAP]`, `read_only: true`, `tmpfs: /tmp`, `cgroup: private`, no bind of `/sys/fs/cgroup`. `out/main-setpcap-aa-unconfined.log` is the same run under `apparmor=unconfined` with the first draft profile (which still allowed `setns`); its results match except where noted.

### bwrap

| Case (as uid 10001) | Result |
|---|---|
| `--unshare-user --ro-bind / /` | exit 0 |
| plus `--proc /proc --dev /dev` | exit 0 |
| the plan's set: `--unshare-user --disable-userns --unshare-pid --die-with-parent`, procfs, dev, tmpfs, a `--tmpfs /data/db --remount-ro` mask | exit 0; mask empty, `touch` refused "Read-only file system", uid_map `10001 0 1` |
| `--unshare-net` | exit 0; interfaces `['lo']`; connect to 1.1.1.1:443 `ENETUNREACH` (101), while the same connect outside bwrap succeeds |
| `executor._bwrap_available()` in-process | `True`, `needs_unshare_user False` |

### The syscall set bwrap needs

Measured by removing one name at a time from the added allow rule (`out/without-*.log`, root phase's remount skipped so only bwrap is measured):

| Removed | Result |
|---|---|
| `clone` | container cannot start (exit 254). With `SYS_ADMIN` in `cap_add` the default's flag-filtered `clone` rule is excluded, so nothing can fork |
| `clone3` | the daemon hangs importing numpy (thread creation fails with `EPERM`; glibc falls back to `clone` only on `ENOSYS`). Same cause: the default's `clone3` → `ENOSYS` rule is excluded under `SYS_ADMIN` |
| `mount` | `Failed to make / slave: Operation not permitted` |
| `pivot_root` | `pivot_root: Operation not permitted` |
| `umount2` | `unmount old root: Operation not permitted` |
| `unshare` | `unshare user ns: Operation not permitted` |
| `setns` | everything passes: **not needed** |

So the profile is Docker 29.9.0's default with the `CAP_SYS_ADMIN` `includes` rule removed and one unconditional allow for `clone`, `clone3`, `mount`, `pivot_root`, `umount2`, `unshare`. That is `seccomp-istota.draft.json`, derived by `derive_profile.py` from `docker-default-29.9.0.json` (fetched by `fetch-default-profile.sh` from the moby `docker-v29.9.0` tag, where it is vendored from `moby/profiles`). `bpf`, `keyctl`, `add_key`, `request_key`, `perf_event_open`, `userfaultfd`, `kexec_*`, `*_module`, `open_by_handle_at` stay denied.

Comparison runs, under `apparmor=unconfined` so only seccomp differs:

- Docker's builtin profile with `SYS_ADMIN` in `cap_add`: bwrap fails at `pivot_root: Operation not permitted` (`out/docker-default-with-sysadmin.log`). Row 1's control ("replace the shipped profile with Docker's default") does go red, but at `pivot_root`, not at namespace creation.
- Docker's builtin profile without `SYS_ADMIN`: `No permissions to create a new namespace` (`out/docker-default-without-sysadmin.log`).
- `seccomp=unconfined`: bwrap works and the row 16 calls stop failing (`out/unconfined.log`).

The root phase's `mount -o remount,rw` needs `LIBMOUNT_FORCE_MOUNT2=always`: util-linux 2.41 uses the new mount API (`fspick`, `fsconfig`, `mount_setattr`), which the profile leaves denied, and `EPERM` from it does not make libmount fall back. With the variable set, classic `mount(2)` is used and only `mount` is needed.

### Per-task cgroups

- Mount before the root phase: `root=/ type=cgroup2 opts=ro,nosuid,nodev,noexec,relatime`, superblock `rw,nsdelegate,memory_recursiveprot`. `/proc/self/cgroup` is `0::/`.
- `mount -o remount,rw /sys/fs/cgroup`: ok; after, `root=/ ... rw`. A raw `MS_REMOUNT` and `MS_REMOUNT|MS_BIND` both work; mounting a fresh cgroup2 on top answers `EBUSY` (`out/mountprobe--unconfined.log`), so the spec's fallback is "unmount, then mount", not "mount over".
- Controllers available: `cpuset cpu io memory hugetlb pids rdma misc`. After moving PID 1 into `supervisor/` and writing `+memory +pids +cpu`: `subtree_control` is `cpu memory pids`. Root dir, `cgroup.procs`, `cgroup.subtree_control`, `cgroup.threads` and `supervisor/` chowned to 10001.
- As uid 10001, through the real `istota.sandbox.cgroup` with `root=/sys/fs/cgroup`: `probe()` OK; `create()` made `task-990001-1` with `memory.max = 67108864`, `pids.max = 64`, `cpu.max = 50000 100000`; a child placed with `placement()` that touches 256 MiB was killed (returncode -9, `memory.events` `oom: 1, oom_kill: 1, max: 38`); a 16 MiB child survived; `destroy()` True.
- Unpatched `resolve_root()` returns `None` here, as the spec says; the `ISTOTA_TASK_CGROUP_ROOT` arm is Stage 2.
- **Without `CAP_SYS_ADMIN`** the remount is refused (`mount: permission denied`, then `umount: must be superuser to unmount`) and the container exits (`out/no-sysadmin-remount.log`). Under Docker there is no route without it.
- **Negative control, host bind** (`overlay-cgroup-bind.yml`): the mount reads `root=/../.. type=cgroup2 opts=rw`, and the entrypoint refuses before writing anything (exit 70, `out/cgroup-bind.log`). This reproduces the spec's Docker 29.8 measurement on 29.9.

### Privileges

| Process | uid | CapPrm / CapEff / CapInh / CapAmb | CapBnd | NoNewPrivs | Seccomp | AppArmor |
|---|---|---|---|---|---|---|
| daemon (PID 1, `istota-scheduler --daemon` from the venv, after `setpriv`) | 10001 | all 0 | 0 | 1 | 2 (1 filter) | enforced |
| `docker compose exec --user 10001:10001` | 10001 | all 0 | `CHOWN,FOWNER,SETGID,SETUID,SETPCAP,SYS_ADMIN` | 1 | 2 | enforced |
| plain `docker compose exec` | 0 | Prm and Eff = `CHOWN,FOWNER,SETGID,SETUID,SETPCAP,SYS_ADMIN` | same | 1 | 2 | enforced |

A plain exec is uid 0 with the `cap_add` set, which is why every exec path has to drop. Under `apparmor=unconfined` it wrote `vm/swappiness` and `kernel/core_pattern` (same value written back); under the draft AppArmor profile both writes are refused. uid 10001 is refused either way.

Other observations from the same run: `/app`, `/usr` and `/etc` are not writable by uid 0 or 10001 (`read_only`); no `docker.sock` anywhere and `DOCKER_HOST` unset.

### Compose file secrets

The secret was declared with `uid: "10001"`, `gid: "10001"`, `mode: 0400`, from a file that is `0644 root:root` on the VM. Inside the container `/run/secrets/probe_secret` is `uid=0 gid=0 mode=644` (Compose v5.6.0, Docker 29.9.0). Compose outside Swarm does not apply `uid`, `gid` or `mode` to a file secret, as the spec expected; the VM side has to set them. There is no pinned Compose version in the repository: the role and this VM install whatever `docker-compose-plugin` Docker's apt repository resolves to.

## Rootful Podman

Same VM, same patched image (copied with `docker save | podman load`), same seccomp profile, `--security-opt unmask=ALL` (Podman's form of `systempaths=unconfined`), `no-new-privileges`, `--cap-drop ALL`, `--read-only`, `--cgroupns private`, and `cap_add` of `CHOWN, FOWNER, SETUID, SETGID, SETPCAP`, with **no `SYS_ADMIN`**.

- **Podman hands the container a writable cgroup2 mount**: `root=/ ... rw` before the root phase does anything. The root phase delegates with `CHOWN` alone; no remount and no `CAP_SYS_ADMIN`. `probe()` OK, the 256 MiB child OOM-killed, the 16 MiB child survived (`out/podman-no-sysadmin-*.log`).
- **It accepts the same seccomp profile.**
- Its default AppArmor profile (`containers-default-0.62.2`) refuses user namespace creation: `bwrap: Creating new namespace failed: Permission denied` (`out/podman-no-sysadmin-default-apparmor.log`). With the draft AppArmor profile every bwrap case passes (`out/podman-no-sysadmin-istota-apparmor.log`).
- Daemon: uid 10001, every capability set 0, `NoNewPrivs: 1`. `podman exec --user 10001:10001` keeps the configured bounding set, as Docker does.
- Outbound connections from a Podman container timed out in this VM (default network, not investigated); `--unshare-net` inside bwrap still gave `ENETUNREACH`.

For spec 3: rootful Podman removes the need for `CAP_SYS_ADMIN` in the root phase entirely, and the remount step with it. It needs the same AppArmor decision as Docker.

## Files

- `install-engines.sh`: Docker Engine, the compose plugin and Podman on a fresh Debian 13 VM.
- `fetch-default-profile.sh`, `docker-default-29.9.0.json`, `derive_profile.py`, `seccomp-istota.draft.json`: the seccomp derivation and its result.
- `apparmor-istota.draft`: the AppArmor profile measured above.
- `Dockerfile.spike`, `entrypoint.spike.sh`, `unprivileged.spike.sh`: the hand-patched image and its two phases.
- `compose.spike.yml` and `overlay-*.yml`: the run contract and its variants.
- `probes/`: what runs inside the container.
- `run-probes.sh`, `podman-probes.sh`: the drivers.

Run order inside the VM, as root, with this directory copied to `/srv/istota-spike` and the current image loaded as `istota-spike/istota:<tag>`: `install-engines.sh`, `run-probes.sh setup istota-spike/istota:<tag>`, then `run-probes.sh <variant>` for each variant in its `case` list, and `podman-probes.sh load` followed by its variants. The `SPIKE_APPARMOR` environment variable picks the AppArmor profile for a run.

Two logs predate fixes and are kept as the evidence for them: `main-spec-caps.log` (the spec's exact contract: remount `EACCES` under `docker-default`) and `main-aa-unconfined.log` (`setpriv` failing without `CAP_SETPCAP`; the remount failure in its first run was the new mount API, fixed by `LIBMOUNT_FORCE_MOUNT2`).

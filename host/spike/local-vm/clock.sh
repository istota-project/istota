#!/bin/bash
# local-vm-install Stage 1 (d), the half that needs no sleeping Mac: what keeps
# the guest clock, and who steps a 30-minute offset back? The offset is made by
# setting the clock back, which is not what a Mac sleep does (see RESULTS.md);
# it tests only the correction. Run as root in the guest.
#   clock.sh [watch-seconds] [stop-timesyncd: 0|1]
set -uo pipefail
WATCH="${1:-180}"
STOP_TIMESYNCD="${2:-0}"

echo "== time services"
timedatectl show-timesync --all 2>/dev/null | grep -E '^(ServerName|PollIntervalUSec|PollIntervalMaxUSec)='
echo "clocksource: $(cat /sys/devices/system/clocksource/clocksource0/current_clocksource)"
ps -eo pid,args | grep -E '[l]ima-guestagent' | head -1

if [ "$STOP_TIMESYNCD" = 1 ]; then
    systemctl stop systemd-timesyncd
    echo "timesyncd stopped for this run"
fi
since="$(date '+%Y-%m-%d %H:%M:%S')"

# Times on CLOCK_MONOTONIC throughout: bash's SECONDS follows the wall clock.
python3 - "$WATCH" <<'PY'
import subprocess, sys, time
watch = float(sys.argv[1])
def dev():
    return time.clock_gettime(time.CLOCK_REALTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
base = dev()
subprocess.run(["date", "-s", f"@{int(time.time()) - 1800}"], check=True, stdout=subprocess.DEVNULL)
t0 = time.monotonic()
print(f"set back 1800 s; deviation {dev() - base:.1f} s")
while time.monotonic() - t0 < watch:
    d = dev() - base
    if abs(d) < 5:
        print(f"corrected after {time.monotonic() - t0:.1f} s (deviation {d:.2f} s)")
        break
    time.sleep(0.5)
else:
    print(f"not corrected within {watch:.0f} s (deviation {dev() - base:.1f} s)")
PY

echo "== journal since the change (timesyncd, guest agent, kernel time messages)"
journalctl --since "$since" --no-pager -o short-monotonic 2>/dev/null \
    | grep -iE 'timesync|guestagent|clock|time' | head -10
[ "$STOP_TIMESYNCD" = 1 ] && systemctl start systemd-timesyncd
echo "timesyncd: $(systemctl is-active systemd-timesyncd)"

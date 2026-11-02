# Ubuntu acceptance checklist

Run on the dedicated Ubuntu VM after installing this update. This is the final
verification workflow, not a claim that reboot and systemd behavior were tested
on the Windows development host. Use the actual command results as evidence.

## 1. Upgrade the installed copy

From the source repository after fetching/copying this update:

```bash
git log -1 --oneline
sudo systemctl disable --now traffic-schedule.timer
sudo systemctl stop traffic-schedule.service
sudo systemctl stop 'traffic-manual-*.service'
sudo systemctl stop traffic-browser.service traffic-browser-lab.service traffic-pktgen.service traffic-http.service
sudo systemctl stop traffic-lab.service
sudo bash install.sh
cmp testbed.py /opt/traffic-testbed/testbed.py
sudo systemd-analyze verify /etc/systemd/system/traffic-*.service /etc/systemd/system/traffic-*.timer
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py validate
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py network-mode browser
```

Expected: installation and validation succeed, unit verification has no errors,
and mode is `external` if your installed configuration is already migrated.
Installation still preserves configuration and leaves scheduling disabled.
Stop capture processes before stopping lab namespaces. The supplied defaults
remain disabled on a fresh install.

## 2. Run offline tests, including rapid schedule boundaries

```bash
python3 -m unittest discover -s tests -v
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/traffic-testbed/browsers \
  /opt/traffic-testbed/venv/bin/python tests/browser_shutdown_live.py -v
sudo python3 tests/integration.py
```

Expected:

* Unit tests pass. Tests simulate before 08:00, inside the window, exactly 17:00,
  weekend, DST offsets, reboot reconciliation, repeat cooldowns and persistent
  budgets; no host clock changes or nine-hour wait is needed.
* Two live Chromium tests pass against a loopback-only slow server. They stop
  during an active page load using a simulated schedule boundary or the actual
  SIGTERM handler, require prompt shutdown and reject unhandled async errors.
  These inject a localhost fixture directly into the workload; production URL
  validation is unchanged and no schedule override is installed.
* Linux namespace integration waits for a verified HTTP response and a nonzero
  pktgen packet count before deliberately stopping/killing either workload.
  It allows cold Chromium startup up to a bounded readiness timeout, rather
  than assuming five seconds is sufficient. It prints PASS for real HTTP, UDP generation,
  independent stop, clean browser shutdown, SIGKILL pktgen cleanup and isolation.
  It refuses active scheduled/manual workloads and an existing emergency flag.

The loopback test checks the handler, not OS signal delivery to an entire
systemd cgroup; test actual delivery in step 4. Tests use no public websites.
The live Chromium test uses the explicitly selected installed browser path;
Ubuntu's default root/user browser cache may differ from that install path.

## 3. Verify external browsing again

Clear a previous emergency block only when you intend to resume testing:

```bash
sudo rm -f /etc/traffic-testbed/DISABLED
sudo /opt/traffic-testbed/manual.sh browser 60
sudo journalctl -u traffic-manual-browser --since '-3 min' --no-pager
```

Expected: configured pages report verified HTTP success, HTTP/blocked counts,
and `browser stopped`. The end must not contain `Future exception was never
retrieved` or `TargetClosedError`. Blocked requests now log a specific reason:
origin, method, schedule/shutdown, per-page budget, cycle budget or daily budget.
HTTP 403/429, challenges and connection errors remain failures rather than
being bypassed. Capture with the README's `tcpdump -i any` command if needed.
The manual test ignores repetition and never replenishes its request budget.

## 4. Verify real systemd stop and emergency stop

Start a manual run in terminal A:

```bash
sudo /opt/traffic-testbed/manual.sh browser 60
```

While it is running, in terminal B:

```bash
sudo systemctl stop traffic-manual-browser.service
sudo journalctl -u traffic-manual-browser --since '-3 min' --no-pager
```

Expected: the workload handles SIGTERM, closes the browser, and its transient
unit becomes inactive/removed. Browser services use `KillMode=mixed`: SIGTERM
goes to Python first so it can close the Playwright driver and Chromium. Any
remaining cgroup processes are forcibly killed after stop timeout/main exit.
Pktgen retains its separate control-group stop policy. Check the installed
browser policy with `systemctl show traffic-browser -p KillMode` (expected
`mixed`); while a manual browser run is active, check the transient unit too.
Repeat the run and this time stop it with:

```bash
sudo /opt/traffic-testbed/emergency-stop.sh
sudo /opt/traffic-testbed/manual.sh browser 10
```

Expected: the running workload stops; the attempted new run logs that the
emergency flag is present and sends no page requests. The timer is disabled.
The flag persists on disk across reboot. Clear it only after this check:

```bash
sudo rm /etc/traffic-testbed/DISABLED
```

## 5. Opt into repeated scheduled cycles

This is optional. Existing installations keep single-run budgets until enabled.
Back up your configuration and add/update these three browser fields:

```bash
sudo cp -a /etc/traffic-testbed/config.json /etc/traffic-testbed/config.json.before-repeat
sudo python3 - <<'PY'
import json
path = '/etc/traffic-testbed/config.json'
with open(path) as f:
    c = json.load(f)
c['browser'].update(repeat_during_window=True, cycle_pause_seconds=900,
                    daily_request_limit=1000)
with open(path, 'w') as f:
    json.dump(c, f, indent=2)
PY
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py validate
```

Each cycle retains existing navigation, per-page HTTP and cycle HTTP limits.
After it finishes, the service closes Chromium, waits at least 60 seconds
(default 900), and starts another bounded cycle if the weekday window is open.
The persistent daily HTTP allowance counts approved requests before sending,
including redirects/assets. It survives service restarts and reboots. The file
is `/var/lib/traffic-testbed-browser/daily-budget.json`, writable only by the
service account. Malformed state or a write failure stops generation.

The daily limit applies to scheduled repetition, not explicit bounded manual
tests. It can stop traffic before 17:00. No output rate or duration is promised
when websites reject requests or a budget is exhausted. Do not delete the state
file to work around a daily cap; choose suitable budgets before the test.

To observe cycling without waiting long, temporarily use `request_limit: 1` and
`cycle_pause_seconds: 60` and start scheduled services during real permitted
hours. Look for `scheduled browser cycle=1`, `cooldown_seconds=60`, then cycle 2.
Restore your intended configuration afterward. Unit tests exercise the same
loop with a fake clock at any time of day.

## 6. Enable and validate the real schedule

```bash
timedatectl
systemd-analyze calendar 'Mon..Fri *-*-* 08:00:00'
systemd-analyze calendar 'Mon..Fri *-*-* 17:00:00'
sudo systemctl enable --now traffic-schedule.timer
sudo systemctl start traffic-schedule.service
systemctl list-timers --all traffic-schedule.timer
systemctl status traffic-browser traffic-browser-lab traffic-pktgen --no-pager
```

Expected: the next boundary uses the VM's intended local timezone. Inside the
window, enabled services start (unless already at a daily cap); outside it,
all scheduled generators remain stopped. The unused browser-mode unit stays
stopped. Manually starting `traffic-browser.service` outside hours must log
`scheduled activation rejected` and produce no page requests.

At an actual weekday boundary, inspect journals and captures to confirm 08:00
activation and 17:00 shutdown. systemd and userspace are not hard real-time:
suspension, resource starvation, queued frames and in-flight requests affect
the exact completion time. Portable simulations do not certify dispatch timing.

## 7. Reboot acceptance

When ready, reboot the dedicated VM with the timer enabled:

```bash
sudo reboot
```

After reconnecting:

```bash
systemctl list-timers --all traffic-schedule.timer
systemctl status traffic-browser traffic-browser-lab traffic-pktgen --no-pager
sudo journalctl -b -u traffic-schedule -u traffic-browser -u traffic-pktgen --no-pager
```

Inside weekday hours, enabled generators should recover after the boot timer;
outside hours or on a weekend they must remain inactive. A previously exhausted
daily HTTP cap must remain exhausted. Repeat at an outside-window time and
with the persistent emergency flag present. Do not change the clock of a VM
running unrelated services merely to accelerate these checks.

Once actual stop, emergency block, boot recovery and schedule boundaries pass,
the device is suitable for an unattended pilot within its configured budgets.

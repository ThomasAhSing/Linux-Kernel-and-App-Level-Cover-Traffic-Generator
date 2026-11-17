# Linux kernel and application traffic generator

The application workload randomly selects website visits, ICMP ping, DNS lookups,
HTTPS GET/POST/API requests, and TCP/TLS connections. It uses the Ubuntu VM's
internet connection, keeps the existing ten website URLs, and uses public
Postman Echo endpoints for POST/API requests without an API key. Kernel pktgen
is a separate component confined to the isolated lab network; see
[LAB_GUIDE.md](LAB_GUIDE.md) to configure it.

## 1. Initial setup or update

Use an Ubuntu 24.04 VM with systemd, internet access, and a kernel with pktgen
support. Copy the updated project to the VM and open a terminal in its source
directory. The installer installs Python dependencies, Chromium and `ping`,
checks pktgen support, and leaves scheduling disabled.

**For an update only**, stop existing workloads before replacing their files:

```bash
sudo systemctl disable --now traffic-schedule.timer
sudo systemctl stop traffic-schedule.service
sudo systemctl stop 'traffic-manual-*.service'
sudo systemctl stop traffic-browser.service traffic-browser-lab.service traffic-pktgen.service traffic-http.service
sudo systemctl stop traffic-lab.service
```

**For both initial setup and updates**, install from the source directory:

```bash
sudo bash install.sh
```

Installation preserves `/etc/traffic-testbed/config.json`. Run the following to
back it up, replace its browser section with the latest example, and enable the
application workload with repeated scheduled cycles. This applies the current
website list and POST/API endpoints, replaces any custom browser settings, and
preserves existing pktgen settings. On a fresh install, pktgen stays disabled.

```bash
sudo python3 - <<'PY'
import datetime
import json
from pathlib import Path
import shutil

path = Path('/etc/traffic-testbed/config.json')
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup = path.with_name('config.json.backup-' + stamp)
shutil.copy2(path, backup)
current = json.loads(path.read_text())
template = json.loads(Path('/opt/traffic-testbed/config.example.json').read_text())
current['browser'] = template['browser']
current['browser']['enabled'] = True
current['browser']['repeat_during_window'] = True
path.write_text(json.dumps(current, indent=2) + '\n')
print('Configuration updated; backup:', backup)
PY
```

The source example stays disabled; the command enables the **installed** copy.
For later changes, edit that installed copy:

```bash
sudo nano /etc/traffic-testbed/config.json
```

If a previous emergency stop created `/etc/traffic-testbed/DISABLED`, remove it
only when you intend to resume traffic:

```bash
sudo rm -f /etc/traffic-testbed/DISABLED
```

## 2. Validate

Check the installed configuration and systemd units before running:

```bash
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py validate
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py network-mode browser
sudo systemd-analyze verify /etc/systemd/system/traffic-*.service /etc/systemd/system/traffic-*.timer
```

Expected: `configuration valid; no network or kernel changes`, mode `external`,
and no unit verification errors. Validation does not generate traffic.

Run the automated tests from the source directory:

```bash
python3 -m unittest discover -s tests -v
```

These tests do not visit public websites. Linux/systemd and live network
behavior still need the test run below. If installation reports missing pktgen,
use a pktgen-enabled kernel or install its matching Ubuntu modules package.

## 3. Run a 60-second test

Keep scheduling disabled and scheduled workloads stopped while testing. If they
are already running:

```bash
sudo systemctl disable --now traffic-schedule.timer
sudo systemctl stop traffic-schedule.service
sudo systemctl stop traffic-browser.service traffic-browser-lab.service traffic-pktgen.service
```

Run the application workload manually, then inspect its logs:

```bash
sudo /opt/traffic-testbed/manual.sh browser 60
sudo journalctl -u traffic-manual-browser.service --since '-5 minutes' --no-pager
```

Manual tests can run any day for 1–60 seconds. Look for
`browser request success` or `network operation=... success`, attempt/error
counts, and `browser stopped` at completion. The run can finish earlier if a
budget is exhausted. Ordinary operation failures are logged and subsequent
attempts continue. ICMP requires permission for the service account to use
ping sockets; some destinations may also block ping.

Each step randomly selects an operation and then a suitable target. Repeats are
possible, so a short test does **not** guarantee every website or operation is
selected. DNS may be cached. Direct HTTPS operations check response headers
and status; they do not process the response body. Public endpoints can reject
or rate-limit requests. Use logs to distinguish successful requests from attempts.

## 4. Run on the weekday schedule

Check the VM's time and timezone; the schedule uses the **VM's local timezone**.
Then enable the timer and reconcile the schedule immediately:

```bash
timedatectl
sudo systemctl enable --now traffic-schedule.timer
sudo systemctl start traffic-schedule.service
systemctl list-timers --all traffic-schedule.timer
sudo journalctl -u traffic-browser.service -u traffic-pktgen.service -u traffic-schedule.service --since today --no-pager
```

The workloads run Monday–Friday, **08:00–17:00**. Starting the scheduler outside
that window leaves them stopped. The enabled timer resumes scheduling after a
reboot. It starts pktgen as well if that component is enabled and configured.

With the installed settings above, application operations start at least
15 seconds apart, each cycle allows up to 20 attempts, and cycles pause for
900 seconds before repeating. Each cycle also has a shared 200-unit budget;
the persistent daily budget is 1000 units. Each explicit network attempt costs
one unit, and each intercepted browser HTTP request, including assets and
redirects, costs one. Limits can stop traffic before 17:00; this is intermittent
traffic rather than continuous output.

Configuration is read at activation. After editing it, validate again and
restart scheduled workloads through the reconciler:

```bash
sudo systemctl stop traffic-schedule.service
sudo systemctl stop traffic-browser.service traffic-browser-lab.service traffic-pktgen.service
sudo systemctl start traffic-schedule.service
```

To stop traffic and block future runs, including after reboot:

```bash
sudo /opt/traffic-testbed/emergency-stop.sh
```

To resume, explicitly remove the `DISABLED` file as described in setup, then
enable the timer and start the schedule service again.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for failures and
[ACCEPTANCE.md](ACCEPTANCE.md) for detailed Linux deployment checks.

# Browser and kernel traffic testbed

Playwright can now visit an explicit list of external HTTPS websites using the Ubuntu VM's normal internet connection. Pktgen remains isolated in the original namespace pair and can only send synthetic UDP to `10.203.0.2:9000`. No host routes or network settings are changed. Both components retain independent lifecycle management, weekday scheduling, locks and emergency shutdown.

## Install or upgrade on Ubuntu 24.04

Run from this source directory. Fetch/copy the updated source into the VM first. Installation preserves existing configuration and never enables scheduling.

```bash
# Stop scheduling, reconciliation, and workloads before replacing their code.
sudo systemctl disable --now traffic-schedule.timer
sudo systemctl stop traffic-schedule.service
sudo systemctl stop 'traffic-manual-*.service'
sudo systemctl stop traffic-browser.service traffic-pktgen.service traffic-http.service
# On upgrades after this version, also stop traffic-browser-lab.service if present.
sudo systemctl stop traffic-lab.service
sudo bash install.sh
sudo systemd-analyze verify /etc/systemd/system/traffic-*.service /etc/systemd/system/traffic-*.timer
```

On a fresh VM, skip the stop commands for units not installed yet. Installation requires Python 3, systemd, internet access to download Playwright/Chromium and OS packages, and a kernel with pktgen support. `preflight.sh` loads/verifies pktgen without generating traffic. If necessary, install the matching `linux-modules-extra-$(uname -r)` package or use a pktgen-enabled kernel. Chromium runs as the unprivileged `traffic-testbed` account.

## Configure an external website list

The new `config.example.json` is an external-mode template, with both generators disabled. Existing installs retain their old lab configuration until explicitly changed. This command backs up the installed config, replaces only the browser section from the new template, and enables the browser. It preserves your existing pktgen settings:

```bash
sudo cp -a /etc/traffic-testbed/config.json /etc/traffic-testbed/config.json.before-external
sudo python3 - <<'PY'
import json
path = '/etc/traffic-testbed/config.json'
with open(path) as f:
    current = json.load(f)
with open('/opt/traffic-testbed/config.example.json') as f:
    template = json.load(f)
current['browser'] = template['browser']
current['browser']['enabled'] = True
with open(path, 'w') as f:
    json.dump(current, f, indent=2)
PY
sudo nano /etc/traffic-testbed/config.json
```

The browser section uses these fields:

```json
{
  "enabled": true,
  "network_mode": "external",
  "urls": ["https://www.google.com/", "https://www.wikipedia.org/"],
  "authorized_origins": ["https://www.google.com", "https://www.wikipedia.org"],
  "asset_origins": ["https://www.gstatic.com", "https://upload.wikimedia.org"],
  "interval_seconds": 15,
  "request_limit": 20,
  "timeout_seconds": 15,
  "max_requests": 200,
  "max_requests_per_page": 50
}
```

* `urls`: 1–50 full page URLs, visited in list order repeatedly until a limit or shutdown. Select websites appropriate for your test.
* `authorized_origins`: exact scheme/hostname/port origins permitted for pages, redirects and assets. Paths belong in `urls`, not here. `https://google.com` and `https://www.google.com` are different origins. No wildcards or implicit subdomains.
* `asset_origins`: additional exact origins permitted for images, stylesheets, scripts and other non-navigation requests. These cannot become page/redirect destinations unless also in `authorized_origins`.
* `interval_seconds`: minimum interval between navigation starts, 5–3600 in external mode. Default 15 seconds; pages are not opened concurrently.
* `request_limit`: maximum navigation attempts, including failures, per activation (1–10,000).
* `timeout_seconds`: page-load timeout, 1–30 seconds.
* `max_requests`: total permitted intercepted HTTP requests, including redirects and assets, per activation (1–1000).
* `max_requests_per_page`: permitted intercepted HTTP requests during each navigation interval (1–100).

External mode requires HTTPS on port 443 with certificate verification enabled. Credentials in URLs, private IP literals, malformed URLs, HTTP downgrades, unapproved origins and non-GET/HEAD methods are rejected. Service workers, WebSockets and downloads are disabled. The browser uses a temporary profile; it does not sign in or bypass site challenges. No other application's traffic or configuration is changed.

Some sites redirect to regional/consent domains or fetch assets from additional origins. These remain blocked until explicitly configured. The journal records blocked URLs; review the required origins and add only those you intend to permit. A 403/429 response, challenge or blocked redirect is logged as a failed load, not bypassed. Stop or reduce requests when the chosen site rejects them. The example list is not a guarantee of availability or a complete asset-domain inventory.

These are page-request controls, not a network firewall. DNS and browser-internal traffic are outside the intercepted HTTP budget; hostname validation does not pin DNS answers or enforce an IP-level egress policy. External mode intentionally uses normal VM networking. Use network-level controls if your experiment needs stricter egress containment.

Validate before starting:

```bash
sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py validate
```

Expected: `configuration valid; no network or kernel changes`. Configuration is loaded once per activation; restart the relevant workload after edits. `DISABLED`, if present from an earlier emergency stop, must be explicitly removed before runs will start.

## Run and observe a 60-second test

External browsing needs VM internet/DNS access but does not need the HTTP server, lab service or pktgen. The manual wrapper validates the browser config and chooses normal networking automatically:

```bash
sudo /opt/traffic-testbed/manual.sh browser 60
sudo journalctl -u traffic-manual-browser.service --since '-2 min' --no-pager
```

Expected: HTTP status successes (usually 200), visited URLs, navigation/error counts, HTTP request counts and blocked-request counts, followed by browser shutdown. It may finish sooner if a request budget is exhausted. Manual tests can run any day for 1–60 seconds. Unit tests never visit public websites automatically.

To capture external browsing, start this in a second terminal **before** the test:

```bash
sudo tcpdump -ni any -w /tmp/external-browser.pcap \
  'tcp port 443 or udp port 443 or port 53'
```

Stop capture with Ctrl+C, then:

```bash
sudo chmod 644 /tmp/external-browser.pcap
```

Open the file in Wireshark. Useful display filters:

| Filter | Expected activity |
| --- | --- |
| `dns` | Website/domain lookups when not cached and using ordinary DNS |
| `tls.handshake.type == 1` | TLS ClientHello messages, often showing server names |
| `tcp.port == 443 || udp.port == 443` | HTTPS connections, including QUIC where used |
| `tcp.analysis.retransmission` | TCP retransmissions, useful for diagnosing connection problems |

Unlike the old HTTP lab capture, HTTPS is encrypted: normally you will not see readable `GET` paths or HTTP `200` responses in Wireshark. Use the journal to verify response status and requested URL. Use Statistics → Conversations and I/O Graphs to inspect destinations and traffic volume. CDN addresses vary, and several websites may share an IP. Capturing on `any` may also include unrelated VM traffic. **External browser traffic will not appear on the isolated `sink0` interface.**

For pktgen, use the original lab configuration/commands in [LAB_GUIDE.md](LAB_GUIDE.md). Its receiver, port, rate/bandwidth limits and namespace isolation are unchanged. Both components can still run concurrently, each using its appropriate network.

## Weekday scheduling and stopping

Production runs only Monday–Friday 08:00–17:00 in the device's local timezone. Services check the schedule at startup and while running. The timer reconciles current time on both boundaries, boot, and clock/timezone changes; missed events cannot blindly replay an out-of-window start. It selects `traffic-browser.service` for external config or `traffic-browser-lab.service` for local lab config, and stops the unused browser unit. Both modes share one lock; scheduled/manual browser overlap is rejected. Pktgen remains independently managed.

```bash
timedatectl
sudo systemctl enable --now traffic-schedule.timer
sudo systemctl start traffic-schedule.service
systemctl list-timers --all traffic-schedule.timer
sudo journalctl -u traffic-browser -u traffic-browser-lab -u traffic-pktgen -u traffic-schedule --since today
```

Budgets apply per activation, not per day. The browser can finish before 17:00 when its request limit is reached. systemd/Linux are not hard real-time: in-flight requests/packets, suspension and CPU starvation can delay completion. The local lab guide details finite pktgen batches and timing limitations.

Emergency stop persists across reboot and stops both browser modes and manual runs:

```bash
sudo /opt/traffic-testbed/emergency-stop.sh
```

To explicitly clear the block later, remove `/etc/traffic-testbed/DISABLED`. Scheduling stays disabled until you enable it again. To uninstall, stop any tcpdump/lab inspection processes, then run `sudo bash uninstall.sh`. Configuration and the system account are retained; program files, units and owned lab namespaces are removed.

## Tests and troubleshooting

```bash
python3 -m unittest discover -s tests -v
# Optional Linux-only live lab tests; stop all generators and disable scheduling first:
sudo python3 tests/integration.py
```

Portable tests cover legacy lab compatibility, exact external origins, asset versus navigation permissions, redirect policy, URL rejection, HTTP budgets, browser cleanup, schedule selection, pktgen validation and failures. The live integration test deliberately uses `config.lab.example.json` and stays offline in the lab pair. External smoke testing is the explicit manual command above and requires live Ubuntu verification.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Request interception and service-worker handling follow the [Playwright network documentation](https://playwright.dev/python/docs/network); WebSocket routing uses [BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext). The browser allowlist is evaluated on each redirected request as well as the initial URL.

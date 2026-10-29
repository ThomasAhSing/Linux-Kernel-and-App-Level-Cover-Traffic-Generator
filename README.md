# Linux traffic generation testbed

Ubuntu 24.04 LTS, Python 3.12, systemd and a kernel with `CONFIG_NET_PKTGEN` are required. This directory is independent of the surrounding Dropbox project. There is no integration with other applications.

Playwright navigates real Chromium pages served by the included HTTP server. Kernel pktgen emits IPv4 UDP frames to port 9000; these are synthetic packets, not simulated application protocols. Separate services, locks, process groups and journals keep both generators independent. The browser service wants the HTTP server; a pktgen failure does not stop the browser and vice versa.

The lab uses **only** `tt-source:lab0 (10.203.0.1/30)` connected to `tt-sink:sink0 (10.203.0.2/30)`. Neither namespace has an external interface, bridge, gateway or default route. No physical interfaces, host routes, host forwarding settings or host firewall rules are changed. Root administrators must keep this topology isolated. Do not attach either endpoint to another network. Exact destination/origin allowlists add validation; namespace topology provides containment because pktgen bypasses ordinary IP routing/firewall processing.

## Timing and limits

The production `run` command checks local time at entry and throughout execution: Monday–Friday, **08:00 inclusive to 17:00 exclusive**. Manual service starts and automatic failure restarts cannot bypass it. The timer fires at both boundaries, at boot, and on clock/timezone changes. Persistent missed events reconcile **current** time rather than blindly replaying an old start. DST uses device local time; there is no hardcoded UTC offset. The services check time every 50 ms. The distinct `manual` command permits an explicit test lasting 1–60 seconds on any day; systemd's scheduled units never use it.

This is not a hard real-time system. Timer accuracy requests do not guarantee exact dispatch under CPU starvation, suspension, clock jumps or kernel faults. In-flight frames/requests can complete after the boundary. A finite pktgen batch contains at most one second's configured packet count; controller death cannot leave an infinite stream. systemd's `ExecStopPost` stops and resets namespace-local pktgen even after SIGKILL. An emergency flag also persists across reboot. No software implementation can promise zero packets after an arbitrary instantaneous clock jump or frozen controller.

Packet size is the Ethernet frame length excluding FCS, 60–1514 bytes. Packet rate is capped at 1,000 pps; bandwidth at 1,000,000 bps. The effective rate is the smaller of configured pps and `floor(bandwidth / ((size + 24) * 8))`, accounting for FCS, preamble and inter-frame gap. A rounded-up nanosecond delay, burst=1, clone_skb=0 and finite counts enforce conservative pacing, rather than line-rate injection. These are pacing budgets, not guaranteed precision measurements on veth. Rate, configuration, transmit errors and per-batch kernel statistics go to the journal. Browser navigation intervals are at least one second and limits at most 10,000 attempts per activation (failures count). Browser HTTP success requires a 2xx response. Timeouts and failures are counted and logged; the next configured page is then attempted. systemd reports OOM/CPU resource events in its own journal. Kernel threads are outside service CPUQuota; pktgen's rate/count bounds are the relevant controls.

## Ubuntu commands and expected results

Run these from this directory on a dedicated Ubuntu VM. Installation downloads packages and Chromium, loads pktgen to verify support, and installs files under `/opt/traffic-testbed` and `/etc/traffic-testbed`. It **does not enable or start scheduled execution**. Existing configuration is preserved. Stop the services and disable the timer before upgrading an existing installation.

1. Install dependencies and the project:

   ```bash
   sudo bash install.sh
   sudo systemd-analyze verify /etc/systemd/system/traffic-*.service /etc/systemd/system/traffic-*.timer
   systemctl is-enabled traffic-schedule.timer
   ```

   Expected: `pktgen supported; no packets generated`, installation confirmation, and `disabled` on a fresh installation. Unit verification has no errors. If module loading fails, install `linux-modules-extra-$(uname -r)` if available for your kernel, or boot a kernel built with `CONFIG_NET_PKTGEN=m`. Preflight intentionally fails instead of substituting a userspace packet sender.

2. Configure and set up the isolated laboratory:

   ```bash
   sudo python3 - <<'PY'
   import json
   path = '/etc/traffic-testbed/config.json'
   with open(path) as f:
       c = json.load(f)
   c['browser']['enabled'] = True
   c['pktgen'].update(enabled=True, interface='lab0', destination='10.203.0.2',
                      authorized_destinations=['10.203.0.2'])
   with open(path, 'w') as f:
       json.dump(c, f, indent=2)
   PY
   sudo /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py validate
   sudo systemctl start traffic-lab.service
   sudo ip -n tt-source address show
   sudo ip -n tt-sink address show
   sudo ip -n tt-source route show
   sudo ip -n tt-sink route show
   ```

   Expected: validation succeeds; each namespace has loopback and one veth, addresses `.1/30` and `.2/30`, and only the directly connected `10.203.0.0/30` route. **No default route.** Configuration is strict JSON: do not add comments or unknown keys. Disable either component with its own `enabled: false`. All numeric bounds are validated before any generation. `validate` changes no kernel or networking state and does not require Playwright to be installed. Editing configuration requires restarting that generator; it is loaded once per activation.

3. Start the local HTTP server:

   ```bash
   sudo systemctl start traffic-http.service
   systemctl status traffic-http.service --no-pager
   ```

   Expected: `active (running)` on `10.203.0.2:8000`, with `/` and `/page-two`. HTTP is the supplied default; HTTPS is supported by Chromium against an operator-provisioned server at `https://10.203.0.2:8443` with a trusted certificate containing that IP address. Update both `base_url` and its exact authorized origin; TLS verification is never disabled. The bundled HTTP service does not provide TLS termination.

4. Short real-browser test, including outside the weekday window:

   ```bash
   sudo /opt/traffic-testbed/manual.sh browser 10
   sudo journalctl -u traffic-manual-browser.service --since '-2 min' --no-pager
   ```

   Expected: HTTP 200 request successes, alternating pages, counts, elapsed times, and browser shutdown within the bounded test (plus browser cleanup time). A disabled component logs `disabled` without launching. The wrapper uses a nonroot browser user and a transient systemd unit for process cleanup.

5. Short kernel test:

   ```bash
   sudo /opt/traffic-testbed/manual.sh pktgen 10
   sudo journalctl -u traffic-manual-pktgen.service --since '-2 min' --no-pager
   sudo ip netns exec tt-source ls /proc/net/pktgen
   ```

   Expected: effective rate 100 pps for the example settings, per-batch `pkts-sofar` statistics and `errors: 0`, then stop/reset. After cleanup, no `lab0@tt` entry remains. The pktgen module stays loaded so unrelated users of it are unaffected.

6. Run both simultaneously and exercise independent stops:

   ```bash
   sudo -v
   sudo /opt/traffic-testbed/manual.sh browser 30 &
   sudo /opt/traffic-testbed/manual.sh pktgen 30 &
   wait
   ```

   While running, use another terminal:

   ```bash
   systemctl status traffic-manual-browser traffic-manual-pktgen --no-pager
   sudo systemctl stop traffic-manual-browser.service
   systemctl is-active traffic-manual-pktgen.service
   ```

   Expected: both initially active; pktgen remains active after stopping the browser. Duplicate controller starts are rejected by per-component `flock` locks, including scheduled/manual overlap. Manual wrappers do not alter installed config, enable scheduling, or automatically start HTTP.

7. Observe browser traffic in another terminal during a test:

   ```bash
   sudo ip netns exec tt-sink tcpdump -ni sink0 'tcp port 8000'
   ```

   Expected: TCP handshakes and HTTP exchanges for the local server. Press Ctrl-C to end capture.

8. Observe kernel packets and interface counters:

   ```bash
   sudo ip netns exec tt-sink tcpdump -ni sink0 'udp dst port 9000'
   sudo ip netns exec tt-source cat /proc/net/pktgen/lab0@tt
   sudo ip -s -n tt-source link show lab0
   sudo ip -s -n tt-sink link show sink0
   ```

   Expected during generation: UDP packets solely from `.1` to `.2:9000`, rising counters and kernel counts. The proc device exists only during the run. For Wireshark, capture with `sudo ip netns exec tt-sink tcpdump -ni sink0 -w /tmp/testbed.pcap`, stop it, then open that pcap as your normal desktop user. Capture inside the namespace: the host has no veth endpoint for this lab.

9. Inspect logs and service/timer state:

   ```bash
   sudo journalctl -u traffic-browser -u traffic-pktgen -u traffic-schedule --since today --no-pager
   systemctl status traffic-browser traffic-pktgen traffic-http --no-pager
   systemctl list-timers --all traffic-schedule.timer
   systemd-analyze calendar 'Mon..Fri *-*-* 08:00:00'
   ```

   Expected: separate scheduled service journals and timer's next weekday boundary once enabled. Before enabling the timer, the scheduled units may be inactive and have no logs. Bounded manual tests use their separate `traffic-manual-*` journals.

10. Stop both workloads immediately, including manual ones:

    ```bash
    sudo /opt/traffic-testbed/emergency-stop.sh
    ```

    Expected: scheduling disabled, both generators stopped, and `/etc/traffic-testbed/DISABLED` present. Every loop and new start checks this flag. It persists after reboot. HTTP and lab services can remain up for inspection. Clear it explicitly with `sudo rm /etc/traffic-testbed/DISABLED` before testing or resuming scheduling.

11. Enable weekday scheduling explicitly:

    ```bash
    sudo rm -f /etc/traffic-testbed/DISABLED
    sudo systemctl enable --now traffic-schedule.timer
    sudo systemctl start traffic-schedule.service
    systemctl list-timers --all traffic-schedule.timer
    ```

    Expected: current local time is reconciled immediately; enabled generators run inside the weekday window and remain stopped outside it. Browser activation also starts the HTTP server. Boot recovery starts the lab automatically when needed. No service should be enabled directly at boot: only the timer is enabled. To pause scheduling and stop workloads without the persistent flag:

    ```bash
    sudo systemctl disable --now traffic-schedule.timer
    sudo systemctl stop traffic-schedule.service
    sudo systemctl stop traffic-browser.service traffic-pktgen.service
    ```

12. Disable and uninstall:

    ```bash
    sudo bash uninstall.sh
    ```

    Expected: timer and services stopped, transient manual units stopped, owned namespaces deleted, code/venv and unit files removed. Configuration and the system account are retained deliberately. If not needed, remove them explicitly:

    ```bash
    sudo rm -rf /etc/traffic-testbed
    sudo userdel traffic-testbed
    ```

    System packages and the host pktgen module are not removed. Run `lab.sh down` only after stopping all namespace processes; it refuses to delete occupied namespaces. Pre-existing namespaces with these names are never adopted.

## Tests

Portable tests (no third-party test dependency, root, traffic or Playwright installation needed):

```bash
python3 -m unittest discover -s tests -v
```

Tests cover validation, injection/URL rejection, numeric budgets, boundary and weekend guards, DST local offsets, boot/missed-event reconciliation, independent failures, request successes/timeouts and browser cleanup using an injected fake browser, and pktgen error/cleanup paths. Linux additionally tests actual `flock` exclusion. These are deterministic tests; they do not change host time or wait nine hours.

Live tests on an installed Ubuntu VM, with the schedule disabled and production generators stopped:

```bash
sudo systemctl disable --now traffic-schedule.timer
sudo systemctl stop traffic-browser.service traffic-pktgen.service
sudo python3 tests/integration.py
```

Expected: `PASS: real HTTP, kernel UDP, concurrent operation, independent stop, SIGKILL cleanup, isolated topology`. This test uses a temporary authorized config, real Chromium and pktgen, captures traffic, checks isolation topology, stops the browser while pktgen runs, kills the pktgen controller and checks kernel cleanup. It restores the initial lab/HTTP active state and does not change installed config. Use a disposable VM; no external network is added to either namespace. For actual reboot validation, enable the timer and reboot once inside and once outside the operating window, then inspect journals, counters and timer status. Reboot and hard real-time behavior cannot be established by the portable mocks.

## Implementation references

The [Linux pktgen documentation](https://docs.kernel.org/networking/pktgen.html) describes proc controls and statistics. [Linux 6.8 pktgen source](https://github.com/torvalds/linux/blob/v6.8/net/core/pktgen.c) implements namespace-local control. [systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html) documents calendar accuracy, boot triggers and persistence. [Playwright browser installation](https://playwright.dev/python/docs/browsers) documents browser binaries and OS dependencies; [BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext) documents request routing and service-worker blocking.

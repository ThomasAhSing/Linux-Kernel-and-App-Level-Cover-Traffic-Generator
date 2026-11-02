#!/usr/bin/env python3
"""Opt-in live Ubuntu integration test; installed testbed required, up to ~60 seconds.

Uses separate transient manual units and temporary config. Never changes the
installed config, clocks, or scheduling state. Refuses an active scheduler.
"""
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def wait_active(unit):
    for _ in range(100):
        if active(unit):
            return
        time.sleep(0.1)
    raise AssertionError(f"{unit} never became active")


def journal(invocation):
    return subprocess.check_output(
        ["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager"], text=True)


def wait_for_log(invocation, process, ready, description, timeout=40, clock=time.monotonic,
                 pause=time.sleep, read_logs=journal):
    """Active systemd state does not mean Chromium has finished starting."""
    until = clock() + timeout
    logs = ""
    while clock() < until:
        logs = read_logs(invocation)
        result = process.poll()
        if result is not None:
            raise RuntimeError(f"Generator exited before {description} (status={result}).\n{logs}")
        if ready(logs):
            return logs
        pause(0.25)
    raise RuntimeError(f"Generator did not reach {description} within {timeout}s.\n{logs}")


def wait_browser_ready(invocation, process, **kwargs):
    return wait_for_log(invocation, process, lambda logs: "browser request success" in logs,
                        "verified browser HTTP success", **kwargs)


def wait_pktgen_ready(invocation, process, **kwargs):
    return wait_for_log(invocation, process,
                        lambda logs: "pktgen batch statistics" in logs and
                        re.search(r"pkts-sofar:\s*[1-9][0-9]*", logs) is not None,
                        "nonzero kernel packet count", **kwargs)


def stop_if_loaded(unit):
    result = subprocess.run(["systemctl", "show", "--value", "-p", "LoadState", unit],
                            check=False, capture_output=True, text=True)
    if result.stdout.strip() not in ("", "not-found"):
        subprocess.run(["systemctl", "stop", unit], check=False)


def main():
    import grp
    assert os.geteuid() == 0, "sudo required"
    assert not active("traffic-schedule.timer"), "disable schedule before integration testing"
    for component in ("browser", "pktgen"):
        assert not active(f"traffic-{component}.service"), "stop scheduled generators first"
        assert not active(f"traffic-manual-{component}.service"), "manual workload already active"
    assert not active("traffic-browser-lab.service"), "stop lab browser before integration testing"
    assert not Path("/etc/traffic-testbed/DISABLED").exists(), "emergency disable flag present"
    lab_was_active = active("traffic-lab.service")
    http_was_active = active("traffic-http.service")
    children = []
    try:
        run("systemctl", "start", "traffic-lab.service", "traffic-http.service")
        run("modprobe", "pktgen")
        # Both endpoints have only the veth pair and loopback; no external edge exists.
        for ns, interface in (("tt-source", "lab0"), ("tt-sink", "sink0")):
            links = json.loads(subprocess.check_output(["ip", "-j", "-n", ns, "link"]))
            assert {x["ifname"] for x in links} == {"lo", interface}
            routes = json.loads(subprocess.check_output(["ip", "-j", "-n", ns, "route"]))
            assert not any(x.get("dst") == "default" or "gateway" in x for x in routes)
        with tempfile.TemporaryDirectory(prefix="traffic-testbed-") as directory:
            os.chmod(directory, 0o755)
            root = Path(directory)
            c = json.loads((Path(__file__).parents[1] / "config.lab.example.json").read_text())
            c["browser"].update(enabled=True, request_limit=100)
            c["pktgen"].update(enabled=True, interface="lab0", destination="10.203.0.2",
                                authorized_destinations=["10.203.0.2"])
            config = root / "config.json"
            config.write_text(json.dumps(c))
            os.chown(config, 0, grp.getgrnam("traffic-testbed").gr_gid)
            os.chmod(config, 0o640)
            capture = root / "capture.pcap"
            tcpdump = subprocess.Popen(["ip", "netns", "exec", "tt-sink", "tcpdump", "-Z", "root", "-U", "-n",
                                        "-i", "sink0", "-w", str(capture),
                                        "tcp port 8000 or udp dst port 9000"])
            children.append(tcpdump)
            time.sleep(1)
            assert tcpdump.poll() is None, "packet capture exited before workloads started"
            processes = {}
            invocations = {}
            for component in ("browser", "pktgen"):
                proc = subprocess.Popen(["/opt/traffic-testbed/manual.sh", component, "60", str(config)])
                processes[component] = proc
                children.append(proc)
                wait_active(f"traffic-manual-{component}.service")
                invocation = subprocess.check_output([
                    "systemctl", "show", "--value", "-p", "InvocationID", f"traffic-manual-{component}.service"
                ], text=True).strip()
                assert len(invocation) == 32, f"missing {component} invocation id"
                invocations[component] = invocation
            browser_invocation = invocations["browser"]
            # Cold Chromium startup may take longer than five seconds on a VM.
            # Verify genuine HTTP success before deliberately stopping it.
            wait_browser_ready(browser_invocation, processes["browser"])
            wait_pktgen_ready(invocations["pktgen"], processes["pktgen"], timeout=10)
            assert active("traffic-manual-browser.service"), "browser stopped before the lifecycle check"
            assert active("traffic-manual-pktgen.service"), "pktgen stopped before the lifecycle check"
            run("systemctl", "stop", "traffic-manual-browser.service")
            browser_result = processes["browser"].wait(timeout=20)
            if browser_result != 0:
                raise RuntimeError(f"Browser failed on graceful systemd stop (status={browser_result}).\n"
                                   + journal(browser_invocation))
            assert active("traffic-manual-pktgen.service"), "browser stop affected pktgen"
            # SIGKILL tests systemd ExecStopPost cleanup rather than Python finally.
            run("systemctl", "kill", "--kill-whom=main", "--signal=SIGKILL", "traffic-manual-pktgen.service")
            for _ in range(100):
                result = subprocess.check_output(["ip", "netns", "exec", "tt-source", "ls", "/proc/net/pktgen"], text=True)
                if "lab0@tt" not in result:
                    break
                time.sleep(0.1)
            assert "lab0@tt" not in result, "pktgen device leaked after unexpected termination"
            tcpdump.terminate()
            tcpdump.wait(timeout=5)
            packets = subprocess.check_output(["tcpdump", "-nn", "-r", str(capture)], text=True)
            assert ".8000" in packets, "no genuine browser HTTP traffic captured"
            assert ".9000:" in packets, "no kernel UDP traffic captured"
            logs = journal(browser_invocation)
            assert "browser request success" in logs, "browser did not verify HTTP success"
            assert "browser stopped" in logs, "browser did not report graceful shutdown"
            assert "Future exception was never retrieved" not in logs, "unhandled asynchronous shutdown error"
            assert "TargetClosedError" not in logs, "unhandled browser-close error"
            print("PASS: real HTTP, kernel UDP, concurrent operation, independent stop, clean browser shutdown, SIGKILL cleanup, isolated topology")
    finally:
        for unit in ("traffic-manual-browser.service", "traffic-manual-pktgen.service"):
            stop_if_loaded(unit)
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=15)
        if not http_was_active:
            run("systemctl", "stop", "traffic-http.service")
        if not lab_was_active:
            run("systemctl", "stop", "traffic-lab.service")


if __name__ == "__main__":
    main()

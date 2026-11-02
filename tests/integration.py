#!/usr/bin/env python3
"""Opt-in live Ubuntu integration test; installed testbed required, ~25 seconds.

Uses separate transient manual units and temporary config. Never changes the
installed config, clocks, or scheduling state. Refuses an active scheduler.
"""
import grp
import json
import os
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


def main():
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
            for component in ("browser", "pktgen"):
                proc = subprocess.Popen(["/opt/traffic-testbed/manual.sh", component, "20", str(config)])
                children.append(proc)
                wait_active(f"traffic-manual-{component}.service")
            time.sleep(5)
            run("systemctl", "stop", "traffic-manual-browser.service")
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
            packets = subprocess.check_output(["tcpdump", "-nr", str(capture)], text=True)
            assert ".8000" in packets, "no genuine browser HTTP traffic captured"
            assert ".9000:" in packets, "no kernel UDP traffic captured"
            logs = subprocess.check_output(["journalctl", "-u", "traffic-manual-browser", "--since", "-1min", "--no-pager"], text=True)
            assert "browser request success" in logs, "browser did not verify HTTP success"
            print("PASS: real HTTP, kernel UDP, concurrent operation, independent stop, SIGKILL cleanup, isolated topology")
    finally:
        subprocess.run(["systemctl", "stop", "traffic-manual-browser.service", "traffic-manual-pktgen.service"], check=False)
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

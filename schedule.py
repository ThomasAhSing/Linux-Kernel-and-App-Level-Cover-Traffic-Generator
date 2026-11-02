#!/usr/bin/env python3
"""Reconcile, rather than replay, missed start/stop events."""
import logging
import subprocess
from testbed import in_window, load, browser_mode, CONFIG


def reconcile(window=in_window, execute=subprocess.run, read_config=load):
    action = "start" if window() else "stop"
    logging.warning("scheduled %s (local weekday 08:00-17:00)", action)
    # Distinct calls: failure of one generator does not block the other.
    failed = False
    browser_units = ("traffic-browser.service", "traffic-browser-lab.service")
    if action == "start":
        try:
            mode = browser_mode(read_config(CONFIG, "browser")["browser"])
        except Exception:
            logging.exception("browser configuration invalid; stopping browser services")
            failed = True
            units = [("stop", x) for x in browser_units]
        else:
            chosen = browser_units[mode == "lab"]
            units = [("stop", x) for x in browser_units if x != chosen] + [("start", chosen)]
    else:
        units = [("stop", x) for x in browser_units]
    units.append((action, "traffic-pktgen.service"))
    for operation, unit in units:
        result = execute(["systemctl", operation, unit], check=False)
        failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(reconcile())

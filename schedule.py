#!/usr/bin/env python3
"""Reconcile, rather than replay, missed start/stop events."""
import logging
import subprocess
from testbed import in_window


def reconcile(window=in_window, execute=subprocess.run):
    action = "start" if window() else "stop"
    logging.warning("scheduled %s (local weekday 08:00-17:00)", action)
    # Distinct calls: failure of one generator does not block the other.
    failed = False
    for unit in ("traffic-browser.service", "traffic-pktgen.service"):
        result = execute(["systemctl", action, unit], check=False)
        failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(reconcile())

#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'run with sudo' >&2; exit 1; }
systemctl disable --now traffic-schedule.timer
systemctl stop traffic-schedule.service
systemctl stop 'traffic-manual-*.service'
systemctl stop traffic-browser.service traffic-browser-lab.service traffic-pktgen.service traffic-http.service
systemctl stop traffic-lab.service
# Fail before removing tooling if namespace cleanup could not complete.
/opt/traffic-testbed/lab.sh down
for unit in traffic-lab.service traffic-http.service traffic-browser.service traffic-browser-lab.service traffic-pktgen.service traffic-schedule.service traffic-schedule.timer; do
    rm -f -- "/etc/systemd/system/$unit"
done
systemctl daemon-reload
rm -rf -- /opt/traffic-testbed
echo 'Removed program and units. Configuration, browser budget state and traffic-testbed system account retained.'

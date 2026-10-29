#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'run with sudo' >&2; exit 1; }
touch /etc/traffic-testbed/DISABLED
systemctl disable --now traffic-schedule.timer
# Stop reconciliation before masking to avoid a concurrent start race.
systemctl stop traffic-schedule.service
systemctl stop traffic-browser.service traffic-pktgen.service
systemctl stop 'traffic-manual-*.service'
echo 'Generators stopped; persistent DISABLED flag blocks runs, including after reboot.'

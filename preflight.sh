#!/bin/bash
set -euo pipefail
[[ $(uname -s) == Linux ]] || { echo 'Linux required' >&2; exit 1; }
source /etc/os-release
echo "Host: $PRETTY_NAME, kernel $(uname -r)"
command -v ip
command -v systemctl
[[ -d /run/systemd/system ]] || { echo 'systemd must be PID 1' >&2; exit 1; }
modprobe pktgen
[[ -w /proc/net/pktgen/pgctrl ]] || { echo 'pktgen proc interface missing/not writable' >&2; exit 1; }
echo 'pktgen supported; no packets generated'

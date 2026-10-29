#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'run with sudo' >&2; exit 1; }
component=${1:-}
seconds=${2:-10}
config=${3:-/etc/traffic-testbed/config.json}
[[ $component == browser || $component == pktgen ]] || { echo 'usage: manual.sh browser|pktgen [1-60 seconds] [absolute config path]' >&2; exit 2; }
[[ $seconds =~ ^[0-9]+$ && $seconds -ge 1 && $seconds -le 60 ]] || exit 2
[[ $config == /* ]] || { echo 'absolute config path required' >&2; exit 2; }
systemctl start traffic-lab.service
opts=(--property=NetworkNamespacePath=/run/netns/tt-source
      --property=KillMode=control-group --property=TimeoutStopSec=15
      --property=MemoryMax=1G --property=CPUQuota=50% --property=TasksMax=256
      --setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/traffic-testbed/browsers
      --setenv=PYTHONUNBUFFERED=1)
if [[ $component == browser ]]; then
    opts+=(--uid=traffic-testbed --gid=traffic-testbed)
else
    modprobe pktgen
    opts+=(--property='ExecStopPost=/opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py cleanup')
fi
systemd-run --unit="traffic-manual-$component" --wait --collect "${opts[@]}" \
    /opt/traffic-testbed/venv/bin/python /opt/traffic-testbed/testbed.py manual "$component" \
    --seconds "$seconds" --config "$config"

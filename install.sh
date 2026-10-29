#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'run with sudo' >&2; exit 1; }
cd -- "$(dirname -- "$0")"
# Installation must not replace a running controller.
for unit in traffic-browser.service traffic-pktgen.service traffic-http.service traffic-lab.service traffic-schedule.timer; do
    if systemctl is-active --quiet "$unit"; then
        echo "stop $unit before installing" >&2; exit 1
    fi
done
apt-get update
apt-get install -y python3 python3-venv iproute2 kmod util-linux tcpdump
bash ./preflight.sh
id traffic-testbed &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin traffic-testbed
install -d -m 0755 /opt/traffic-testbed /etc/traffic-testbed
install -m 0644 testbed.py schedule.py http_server.py requirements.txt /opt/traffic-testbed/
install -m 0755 lab.sh preflight.sh emergency-stop.sh manual.sh /opt/traffic-testbed/
if [[ ! -e /etc/traffic-testbed/config.json ]]; then
    install -o root -g traffic-testbed -m 0640 config.example.json /etc/traffic-testbed/config.json
fi
python3 -m venv /opt/traffic-testbed/venv
/opt/traffic-testbed/venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/traffic-testbed/browsers /opt/traffic-testbed/venv/bin/playwright install --with-deps chromium
install -m 0644 systemd/* /etc/systemd/system/
systemctl daemon-reload
echo 'Installed. Scheduling remains disabled. Configure /etc/traffic-testbed/config.json first.'

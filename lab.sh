#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'root required' >&2; exit 1; }
mkdir -p /run/traffic-testbed
exec 9>/run/traffic-testbed/lab.lock
flock -n 9 || { echo 'lab setup/cleanup already running' >&2; exit 1; }
marker=/run/traffic-testbed/owned
cleanup() {
    [[ -f $marker ]] || return 0
    # Only these dedicated namespaces are owned by this installation.
    for ns in tt-source tt-sink; do
        if ip netns list | awk '{print $1}' | grep -qx "$ns"; then
            [[ -z $(ip netns pids "$ns") ]] || { echo "$ns still has processes; stop services first" >&2; return 1; }
            ip netns del "$ns"
        fi
    done
    rm -f "$marker"
}
case ${1:-} in
up)
    [[ ! -e $marker ]] || { echo 'owned lab exists; stop and clean it first' >&2; exit 1; }
    for ns in tt-source tt-sink; do
        [[ ! -e /run/netns/$ns ]] || { echo "namespace collision: $ns" >&2; exit 1; }
    done
    touch "$marker"
    trap cleanup ERR
    ip netns add tt-source
    ip netns add tt-sink
    ip -n tt-source link add lab0 type veth peer name sink0 netns tt-sink
    ip -n tt-source link set lab0 address 02:00:00:00:20:01
    ip -n tt-sink link set sink0 address 02:00:00:00:20:02
    ip -n tt-source addr add 10.203.0.1/30 dev lab0
    ip -n tt-sink addr add 10.203.0.2/30 dev sink0
    for ns in tt-source tt-sink; do
        ip -n "$ns" link set lo up
        ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=0
        ip netns exec "$ns" sysctl -qw net.ipv6.conf.all.disable_ipv6=1
    done
    ip -n tt-source link set lab0 up
    ip -n tt-sink link set sink0 up
    touch /run/traffic-testbed/browser.lock
    chown root:traffic-testbed /run/traffic-testbed/browser.lock
    chmod 0660 /run/traffic-testbed/browser.lock
    echo 'isolated lab ready (no host interfaces, bridges or gateways)'
    ;;
down) cleanup ;;
*) echo 'usage: lab.sh up|down' >&2; exit 2 ;;
esac

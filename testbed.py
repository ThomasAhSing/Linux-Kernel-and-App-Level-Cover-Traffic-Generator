#!/usr/bin/env python3
"""Isolated lab workloads. No routing, DNS, or external service integration."""
import argparse
import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

LOG = logging.getLogger("testbed")
PROC = Path("/proc/net/pktgen")
CONFIG = "/etc/traffic-testbed/config.json"
DISABLED = Path("/etc/traffic-testbed/DISABLED")
STOP = False


def in_window(now=None):
    # Resolve local time on every check, including timezone/clock changes.
    now = now if now is not None else dt.datetime.now().astimezone()
    return now.weekday() < 5 and 8 <= now.hour < 17


def number(value, low, high, name, integer=False):
    if type(value) not in ((int,) if integer else (int, float)):
        raise ValueError(f"{name}: invalid numeric type")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: must be between {low} and {high}")


def validate(c, component=None):
    if set(c) != {"log_level", "browser", "pktgen"}:
        raise ValueError("unknown or missing top-level keys")
    if c["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("invalid log level")
    for name in ([component] if component else ["browser", "pktgen"]):
        p = c[name]
        expected = ({"enabled", "base_url", "authorized_origins", "pages", "interval_seconds",
                     "request_limit", "timeout_seconds"} if name == "browser" else
                    {"enabled", "destination", "authorized_destinations", "interface",
                     "packet_size", "packet_rate", "max_bandwidth_bps"})
        if set(p) != expected or type(p["enabled"]) is not bool:
            raise ValueError(f"{name}: invalid keys or enabled flag")
        if name == "browser":
            number(p["interval_seconds"], 1, 3600, "interval")
            number(p["request_limit"], 1, 10000, "request limit", True)
            number(p["timeout_seconds"], 1, 30, "timeout")
            if not isinstance(p["pages"], list) or not p["pages"] or any(
                x not in ("/", "/page-two") for x in p["pages"]
            ):
                raise ValueError("only the local test pages are permitted")
            u = urlsplit(p["base_url"])
            if (u.scheme not in ("http", "https") or u.hostname != "10.203.0.2"
                    or u.username or u.password or u.path or u.query or u.fragment
                    or u.port not in (8000, 8443)):
                raise ValueError("website must be the isolated receiver on port 8000 or 8443")
            if (not isinstance(p["authorized_origins"], list)
                    or p["base_url"] not in p["authorized_origins"]):
                raise ValueError("website is not explicitly authorized")
        else:
            number(p["packet_size"], 60, 1514, "packet size", True)
            number(p["packet_rate"], 1, 1000, "packet rate", True)
            number(p["max_bandwidth_bps"], 1000, 1000000, "bandwidth", True)
            if p["enabled"] or p["destination"] or p["interface"]:
                # Fixed isolated endpoint; do not accept arbitrary RFC1918 networks.
                if str(ipaddress.IPv4Address(p["destination"])) != "10.203.0.2":
                    raise ValueError("destination must be the isolated lab receiver")
                if p["interface"] != "lab0":
                    raise ValueError("interface must be the dedicated lab0 veth")
                if (not isinstance(p["authorized_destinations"], list)
                        or p["destination"] not in p["authorized_destinations"]):
                    raise ValueError("destination not allowlisted")
            if effective_rate(p) < 1:
                raise ValueError("bandwidth too low for one packet per second")
    return c


def effective_rate(p):
    # Include FCS, preamble and inter-frame gap, not just pktgen's frame bytes.
    return min(p["packet_rate"], p["max_bandwidth_bps"] // ((p["packet_size"] + 24) * 8))


def load(path, component=None):
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f), component)


@contextlib.contextmanager
def exclusive(component):
    import fcntl
    with open(f"/run/traffic-testbed/{component}.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError("duplicate instance rejected") from e
        yield


def ip_json(*args):
    return json.loads(subprocess.check_output(["ip", "-j", *args], text=True, timeout=5))


def verify_isolation():
    if os.stat("/proc/self/ns/net").st_ino != os.stat("/run/netns/tt-source").st_ino:
        raise RuntimeError("must run inside tt-source network namespace")
    links = ip_json("-d", "link", "show")
    if {x["ifname"] for x in links} != {"lo", "lab0"}:
        raise RuntimeError("unexpected interfaces in isolated namespace")
    link = next(x for x in links if x["ifname"] == "lab0")
    if link.get("linkinfo", {}).get("info_kind") != "veth" or "UP" not in link["flags"]:
        raise RuntimeError("lab0 must be an active veth")
    if any("gateway" in x or x.get("dst") == "default" for x in ip_json("route", "show", "table", "all")):
        raise RuntimeError("gateway/default route prohibited")


def pg(file, command, check=True):
    path = PROC / file
    path.write_text(command + "\n")
    if check and "Result: OK:" not in path.read_text():
        raise RuntimeError(f"pktgen rejected {command}: {path.read_text()}")


def cleanup():
    # Namespace-scoped controls; never touch the host pktgen instance.
    verify_isolation()
    if (PROC / "pgctrl").exists():
        pg("pgctrl", "stop", False)
        for device in PROC.glob("lab0@*"):
            LOG.info("pktgen statistics\n%s", device.read_text())
        pg("pgctrl", "reset", False)
        LOG.info("pktgen stopped and configuration removed")


def allowed(deadline):
    return not STOP and not DISABLED.exists() and (time.monotonic() < deadline if deadline else in_window())


def kernel(p, deadline):
    verify_isolation()
    threads = sorted(PROC.glob("kpktgend_*"))
    if not threads:
        raise RuntimeError("pktgen unavailable: load module on the host first")
    thread = threads[0].name
    dev = "lab0@tt"
    writer = None
    try:
        pg(thread, f"add_device {dev}")
        rate = effective_rate(p)
        for command in (f"pkt_size {p['packet_size']}", "clone_skb 0", "burst 1",
                        f"dst {p['destination']}", "src_min 10.203.0.1",
                        "src_max 10.203.0.1", "dst_mac 02:00:00:00:20:02",
                        "udp_dst_min 9000", "udp_dst_max 9000",
                        f"delay {math.ceil(1_000_000_000 / rate)}", f"count {rate}"):
            pg(dev, command)
        LOG.info("pktgen configured effective_rate=%d bandwidth_cap=%d", rate, p["max_bandwidth_bps"])
        # Finite batches cap orphan output even if userspace is killed.
        while allowed(deadline):
            started = time.monotonic()
            writer = subprocess.Popen([sys.executable, __file__, "_pgstart"])
            while writer.poll() is None and allowed(deadline):
                time.sleep(0.05)
            if not allowed(deadline):
                pg("pgctrl", "stop", False)
            writer.wait(timeout=5)
            if writer.returncode:
                raise RuntimeError(f"pktgen writer exited {writer.returncode}")
            stats = (PROC / dev).read_text()
            LOG.info("pktgen batch statistics\n%s", stats)
            if "Result: OK:" not in stats or "errors: 0" not in stats:
                raise RuntimeError("pktgen reported transmit errors")
            while allowed(deadline) and time.monotonic() - started < 1:
                time.sleep(0.05)
    finally:
        cleanup()
        if writer and writer.poll() is None:
            writer.terminate()
            writer.wait(timeout=5)


async def browser(p, deadline, launcher=None):
    if launcher is None:
        from playwright.async_api import async_playwright
        async with async_playwright() as api:
            return await browser(p, deadline, api.chromium)
    browser_process = await launcher.launch(headless=True)
    count = errors = 0
    try:
        context = await browser_process.new_context(service_workers="block")
        urls = [p["base_url"] + path for path in p["pages"]]

        async def guard(route):
            if route.request.url in urls and allowed(deadline):
                await route.continue_()
            else:
                LOG.warning("blocked unauthorized request %s", route.request.url)
                await route.abort()

        await context.route("**/*", guard)
        page = await context.new_page()
        while allowed(deadline) and count < p["request_limit"]:
            started = time.monotonic()
            try:
                response = await page.goto(urls[count % len(urls)],
                                           timeout=p["timeout_seconds"] * 1000,
                                           wait_until="load")
                if response is None or not response.ok:
                    raise RuntimeError(f"HTTP failure: {getattr(response, 'status', None)}")
                LOG.info("browser request success status=%s", response.status)
            except Exception as exc:
                errors += 1
                LOG.error("browser request failed: %s", exc)
            count += 1
            LOG.info("browser count=%d errors=%d elapsed_seconds=%.3f", count, errors, time.monotonic()-started)
            while allowed(deadline) and time.monotonic() - started < p["interval_seconds"]:
                await asyncio.sleep(0.05)
    finally:
        await browser_process.close()
        LOG.info("browser stopped requests=%d errors=%d", count, errors)


async def supervised_browser(p, deadline):
    task = asyncio.create_task(browser(p, deadline))
    try:
        while not task.done() and allowed(deadline):
            await asyncio.sleep(0.05)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def stop_signal(signum, frame):
    global STOP
    STOP = True
    LOG.info("shutdown signal=%s", signum)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "run", "manual", "cleanup", "_pgstart"])
    parser.add_argument("component", nargs="?", choices=["browser", "pktgen"])
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--seconds", type=int, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.action == "_pgstart":
        verify_isolation()
        pg("pgctrl", "start", False)
        return
    if args.action == "cleanup":
        # A rejected duplicate systemd activation must not reset the controller
        # which already owns the namespace's pktgen device.
        with exclusive("pktgen"):
            cleanup()
        return
    c = load(args.config, args.component)
    LOG.setLevel(c["log_level"])
    if args.action == "validate":
        LOG.info("configuration valid; no network or kernel changes")
        return
    if not args.component:
        parser.error("run/manual requires a component")
    if not c[args.component]["enabled"]:
        LOG.info("%s disabled", args.component)
        return
    if DISABLED.exists():
        LOG.warning("emergency disable flag present")
        return
    deadline = None
    if args.action == "manual":
        number(args.seconds, 1, 60, "manual duration", True)
        deadline = time.monotonic() + args.seconds
        LOG.warning("explicit bounded manual test duration=%ds", args.seconds)
    elif not in_window():
        LOG.info("scheduled activation rejected outside weekday 08:00-17:00")
        return
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    verify_isolation()
    with exclusive(args.component):
        LOG.info("%s service started; scheduled=%s", args.component, deadline is None)
        try:
            if args.component == "browser":
                asyncio.run(supervised_browser(c["browser"], deadline))
            else:
                kernel(c["pktgen"], deadline)
        finally:
            LOG.info("%s service stopped; schedule_open=%s", args.component, in_window())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("configuration/generator failure or unexpected termination")
        sys.exit(1)

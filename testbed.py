#!/usr/bin/env python3
"""Allowlisted browser workloads and an independently isolated kernel generator."""
import argparse
import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import math
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
from browser_budget import DailyBudget

LOG = logging.getLogger("testbed")
PROC = Path("/proc/net/pktgen")
CONFIG = "/etc/traffic-testbed/config.json"
DISABLED = Path("/etc/traffic-testbed/DISABLED")
STOP = False
BROWSER_STATE = Path("/var/lib/traffic-testbed-browser/daily-budget.json")


def origin(url, external=True, origin_only=False):
    """Canonical exact origins, with no wildcard/suffix or userinfo matching."""
    if not isinstance(url, str) or not url or any(ord(x) <= 32 for x in url) or "\\" in url:
        raise ValueError("URL must not contain whitespace, controls or backslashes")
    u = urlsplit(url)
    if u.scheme not in (("https",) if external else ("http", "https")) or not u.hostname:
        raise ValueError("external websites require an absolute HTTPS URL")
    if u.username is not None or u.password is not None or u.fragment:
        raise ValueError("URL credentials and fragments are prohibited")
    host = u.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or any(not x or len(x) > 63 or x.startswith("-") or x.endswith("-") for x in host.split("."))
                or len(host) > 253 or "." not in host or host.endswith((".local", ".localhost", ".internal"))):
            raise ValueError("invalid external hostname")
    else:
        if external and not address.is_global:
            raise ValueError("external URLs cannot use nonpublic IP literals")
        if address.version == 6:
            host = f"[{host}]"
    port = u.port
    if external and port not in (None, 443):
        raise ValueError("external browsing uses HTTPS port 443 only")
    if origin_only and (u.path not in ("", "/") or u.query):
        raise ValueError("authorized origins must not contain paths or queries")
    default = 443 if u.scheme == "https" else 80
    return f"{u.scheme}://{host}" + (f":{port}" if port and port != default else "")


def browser_settings(p):
    """Keep the original local configuration usable for offline integration tests."""
    if "urls" in p:
        return p
    return dict(p, network_mode="lab", urls=[p["base_url"] + x for x in p["pages"]],
                asset_origins=[], max_requests=min(p["request_limit"] * 50, 1000),
                max_requests_per_page=50)


def browser_mode(p):
    return browser_settings(p)["network_mode"]


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
        if name == "browser" and "urls" in p:
            expected = {"enabled", "network_mode", "urls", "authorized_origins", "asset_origins",
                        "interval_seconds", "request_limit", "timeout_seconds", "max_requests", "max_requests_per_page"}
        optional = {"repeat_during_window", "cycle_pause_seconds", "daily_request_limit"} if name == "browser" else set()
        if set(p) - optional != expected or type(p["enabled"]) is not bool:
            raise ValueError(f"{name}: invalid keys or enabled flag")
        if name == "browser":
            if type(p.get("repeat_during_window", False)) is not bool:
                raise ValueError("repeat_during_window must be a boolean")
            number(p.get("cycle_pause_seconds", 900), 60, 3600, "cycle pause", True)
            number(p.get("daily_request_limit", 1000), 1, 10000, "daily HTTP request limit", True)
            b = browser_settings(p)
            if b["network_mode"] not in ("lab", "external"):
                raise ValueError("network_mode must be lab or external")
            external = b["network_mode"] == "external"
            number(p["interval_seconds"], 5 if external else 1, 3600, "interval")
            number(p["request_limit"], 1, 10000, "request limit", True)
            number(p["timeout_seconds"], 1, 30, "timeout")
            number(b["max_requests"], 1, 1000, "total HTTP request budget", True)
            number(b["max_requests_per_page"], 1, 100, "per-page HTTP request budget", True)
            if not isinstance(b["urls"], list) or not b["urls"] or len(b["urls"]) > 50:
                raise ValueError("configure between 1 and 50 website URLs")
            for key in ("authorized_origins", "asset_origins"):
                if not isinstance(b[key], list) or len(b[key]) > 100:
                    raise ValueError(f"{key} must be a list of at most 100 exact origins")
                for entry in b[key]:
                    origin(entry, external, origin_only=True)
            authorized = {origin(x, external, True) for x in b["authorized_origins"]}
            for url in b["urls"]:
                if origin(url, external) not in authorized:
                    raise ValueError("website origin is not explicitly authorized")
                if not external and url not in ("http://10.203.0.2:8000/", "http://10.203.0.2:8000/page-two",
                                                "https://10.203.0.2:8443/", "https://10.203.0.2:8443/page-two"):
                    raise ValueError("lab mode only permits the isolated test pages")
            if "urls" in p:
                continue
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
    directory = "/run/traffic-testbed-browser" if component == "browser" else "/run/traffic-testbed"
    with open(f"{directory}/{component}.lock", "a") as f:
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


async def navigation(page, url, timeout_seconds, deadline):
    """Abort through Playwright, then retrieve the pending API result.

    Never cancel page.goto's task: cancelling the wrapper before Playwright
    resolves its internal protocol future caused unhandled TargetClosedError.
    """
    pending = asyncio.create_task(page.goto(url, timeout=timeout_seconds * 1000, wait_until="load"))
    try:
        while not pending.done():
            if not allowed(deadline):
                LOG.info("closing in-flight navigation for shutdown")
                await page.close()
                await asyncio.gather(pending, return_exceptions=True)
                return None
            await asyncio.wait({pending}, timeout=0.05)
        return await pending
    finally:
        # Also drain the API operation if an unexpected caller cancellation
        # occurs. This is cleanup, not a blanket event-loop error filter.
        if not pending.done():
            try:
                await page.close()
            finally:
                await asyncio.gather(pending, return_exceptions=True)
        else:
            await asyncio.gather(pending, return_exceptions=True)


async def browser(p, deadline, launcher=None, budget=None):
    p = browser_settings(p)
    if launcher is None:
        from playwright.async_api import async_playwright
        async with async_playwright() as api:
            return await browser(p, deadline, api.chromium, budget)
    browser_process = await launcher.launch(headless=True, timeout=15000,
                                            handle_sigint=False, handle_sigterm=False, handle_sighup=False)
    count = errors = sent = blocked = page_requests = 0
    context = None
    closing = False
    budget_error = None
    try:
        context = await browser_process.new_context(service_workers="block", accept_downloads=False,
                                                     ignore_https_errors=False)
        urls = p["urls"]
        external = p["network_mode"] == "external"
        navigation_origins = {origin(x, external) for x in p["authorized_origins"]}
        asset_origins = navigation_origins | {origin(x, external) for x in p["asset_origins"]}

        async def guard(route):
            nonlocal sent, blocked, page_requests, budget_error
            request = route.request
            try:
                target = origin(request.url, external)
                permitted = target in (navigation_origins if request.is_navigation_request() else asset_origins)
                if not external:
                    permitted = permitted and request.url in urls
            except ValueError:
                permitted = False
            reason = None
            if not permitted:
                reason = "origin"
            elif request.method not in ("GET", "HEAD"):
                reason = "method"
            elif closing or not allowed(deadline):
                reason = "shutdown/schedule"
            elif budget_error is not None:
                reason = "daily budget persistence failure"
            elif sent >= p["max_requests"]:
                reason = "cycle HTTP budget"
            elif page_requests >= p["max_requests_per_page"]:
                reason = "page HTTP budget"
            elif budget is not None:
                try:
                    if not budget.reserve():
                        reason = "daily HTTP budget"
                except Exception as exc:
                    budget_error = exc
                    reason = "daily budget persistence failure"
                    LOG.error("daily budget persistence failed; stopping browser: %s", exc)
            if budget_error is not None:
                reason = "daily budget persistence failure"
            if reason is None and not allowed(deadline):
                reason = "shutdown/schedule"
            if reason is None:
                sent += 1
                page_requests += 1
                try:
                    await route.continue_()
                except Exception:
                    if not closing and allowed(deadline):
                        raise
            else:
                blocked += 1
                LOG.warning("blocked request reason=%s url=%s", reason, request.url)
                try:
                    await route.abort()
                except Exception:
                    if not closing and allowed(deadline):
                        raise

        await context.route("**/*", guard)
        async def block_socket(socket):
            await socket.close()
        await context.route_web_socket("**/*", block_socket)
        page = await context.new_page()
        while (allowed(deadline) and count < p["request_limit"] and sent < p["max_requests"]
               and budget_error is None and (budget is None or budget.available())):
            page_requests = 0
            started = time.monotonic()
            target_url = urls[count % len(urls)]
            try:
                response = await navigation(page, target_url, p["timeout_seconds"], deadline)
                if not allowed(deadline):
                    break
                if response is None or not response.ok:
                    raise RuntimeError(f"HTTP failure: {getattr(response, 'status', None)}")
                LOG.info("browser request success status=%s url=%s", response.status, target_url)
            except Exception as exc:
                if not allowed(deadline):
                    LOG.info("navigation ended during shutdown")
                    break
                errors += 1
                LOG.error("browser request failed: %s", exc)
            count += 1
            LOG.info("browser count=%d errors=%d http_requests=%d blocked=%d elapsed_seconds=%.3f",
                     count, errors, sent, blocked, time.monotonic()-started)
            while (allowed(deadline) and budget_error is None and (budget is None or budget.available())
                   and time.monotonic() - started < p["interval_seconds"]):
                await asyncio.sleep(0.05)
        if budget_error is not None:
            raise RuntimeError("daily budget could not be persisted; no further traffic permitted") from budget_error
    finally:
        closing = True
        try:
            if context is not None:
                await context.close()
        finally:
            await browser_process.close()
            LOG.info("browser stopped requests=%d errors=%d http_requests=%d blocked=%d", count, errors, sent, blocked)


async def supervised_browser(p, deadline):
    if deadline is not None or not p.get("repeat_during_window", False):
        await browser(p, deadline)
        return
    budget = DailyBudget(BROWSER_STATE, p.get("daily_request_limit", 1000))
    cycle = 0
    while allowed(None) and budget.available():
        cycle += 1
        LOG.info("scheduled browser cycle=%d started; daily_limit=%d", cycle, budget.limit)
        await browser(p, None, budget=budget)
        if not allowed(None) or not budget.available():
            break
        pause = p.get("cycle_pause_seconds", 900)
        LOG.info("browser cycle=%d complete; cooldown_seconds=%d", cycle, pause)
        until = time.monotonic() + pause
        while allowed(None) and time.monotonic() < until:
            await asyncio.sleep(0.05)
    if not budget.available():
        LOG.warning("daily browser HTTP budget exhausted; stopped until next local day")


def stop_signal(signum, frame):
    global STOP
    STOP = True
    LOG.info("shutdown signal=%s", signum)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "network-mode", "run", "manual", "cleanup", "_pgstart"])
    parser.add_argument("component", nargs="?", choices=["browser", "pktgen"])
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--network-mode", choices=["lab", "external"], help="service namespace consistency check")
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
    if args.action == "network-mode":
        print(browser_mode(c["browser"]))
        return
    LOG.setLevel(c["log_level"])
    if args.action == "validate":
        LOG.info("configuration valid; no network or kernel changes")
        return
    if not args.component:
        parser.error("run/manual requires a component")
    if args.component == "browser" and args.network_mode and browser_mode(c["browser"]) != args.network_mode:
        raise ValueError("browser config network_mode does not match the selected service")
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
    if args.component == "pktgen" or browser_mode(c["browser"]) == "lab":
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

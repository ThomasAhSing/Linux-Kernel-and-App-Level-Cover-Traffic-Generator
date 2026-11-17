"""Bounded external network operations used by the browser workload."""
import asyncio
import contextlib
import json
import os
import random
import re
import socket
import ssl
from urllib.parse import quote, urlsplit


WEBSITE_OPERATIONS = {"browser_get", "icmp_ping", "dns", "https_get", "tcp_connect", "tls_connect"}
ENDPOINT_OPERATIONS = {"https_post", "https_api"}
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def validate_settings(p, origin):
    operations = p.get("operations", ["browser_get"])
    if (not isinstance(operations, list) or not operations
            or any(not isinstance(x, str) or x not in WEBSITE_OPERATIONS for x in operations)
            or len(set(operations)) != len(operations)):
        raise ValueError("operations must be a nonempty list of unique website operations")
    endpoints = p.get("http_endpoints", [])
    if not isinstance(endpoints, list) or len(endpoints) > 50:
        raise ValueError("http_endpoints must be a list of at most 50 endpoints")
    if p.get("network_mode", "lab") != "external":
        if operations != ["browser_get"] or endpoints:
            raise ValueError("additional network operations require external mode")
        return
    authorized = {origin(x, True, True) for x in p["authorized_origins"]}
    for e in endpoints:
        if (not isinstance(e, dict) or not {"type", "url"} <= e.keys()
                or e.keys() - {"type", "url", "method", "headers", "json", "bearer_token_env"}
                or not isinstance(e["type"], str) or e["type"] not in ENDPOINT_OPERATIONS):
            raise ValueError("invalid HTTP endpoint configuration")
        if origin(e["url"]) not in authorized:
            raise ValueError("HTTP endpoint origin is not explicitly authorized")
        method = e.get("method", "POST" if e["type"] == "https_post" else "GET")
        if method not in ("GET", "POST") or (e["type"] == "https_post" and method != "POST"):
            raise ValueError("endpoint method must be GET or POST; https_post requires POST")
        if "json" in e:
            if method != "POST":
                raise ValueError("JSON bodies require POST")
            try:
                body = json.dumps(e["json"], allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid JSON body") from exc
            if len(body) > 65536:
                raise ValueError("JSON body exceeds 64 KiB")
        headers = e.get("headers", {})
        if not isinstance(headers, dict) or len(headers) > 32:
            raise ValueError("endpoint headers must be an object of at most 32 entries")
        seen = set()
        for name, value in headers.items():
            if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                    or name.lower() in seen
                    or name.lower() in {"host", "connection", "content-length", "transfer-encoding", "expect", "upgrade"}
                    or not isinstance(value, str) or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise ValueError("invalid or reserved endpoint header")
            seen.add(name.lower())
        env = e.get("bearer_token_env")
        if env is not None and (not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env)
                                or "authorization" in seen):
            raise ValueError("invalid bearer_token_env or conflicting Authorization header")


def choose_job(p):
    """Choose an operation uniformly, then a target uniformly; repeats are possible."""
    endpoints = p.get("http_endpoints", [])
    kinds = p.get("operations", ["browser_get"]) + sorted({e["type"] for e in endpoints})
    kind = random.choice(kinds)
    if kind in ENDPOINT_OPERATIONS:
        endpoint = random.choice([e for e in endpoints if e["type"] == kind])
        return kind, endpoint["url"], endpoint
    return kind, random.choice(p["urls"]), None


async def bounded_operation(awaitable, timeout, deadline, allowed):
    """Cancel and drain network work promptly on timeout or schedule/shutdown."""
    task = asyncio.create_task(awaitable)
    until = asyncio.get_running_loop().time() + timeout
    try:
        while not task.done():
            if not allowed(deadline):
                raise RuntimeError("operation interrupted by shutdown/schedule")
            if asyncio.get_running_loop().time() >= until:
                raise TimeoutError("network operation timed out")
            await asyncio.wait({task}, timeout=0.05)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def ping(host, timeout):
    # iputils uses unprivileged ICMP datagram sockets when ping_group_range permits.
    # No shell, privilege escalation, retries, or changes to host networking.
    process = await asyncio.create_subprocess_exec(
        "ping", "-n", "-c", "1", "-W", str(max(1, int(timeout))), host,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError("ICMP unavailable or no reply: " + error.decode(errors="replace").strip()[:256])
        return "echo reply"
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()


async def close_stream(writer):
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=1)


async def execute(kind, url, endpoint, timeout):
    parsed = urlsplit(url)
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or 443
    if kind == "dns":
        results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return "addresses=" + ",".join(sorted({r[4][0] for r in results}))
    if kind == "icmp_ping":
        return await ping(host, timeout)
    # Resolve missing credentials before opening a connection. Never log token/body.
    headers = {k.lower(): v for k, v in (endpoint or {}).get("headers", {}).items()}
    token_env = (endpoint or {}).get("bearer_token_env")
    if token_env:
        token = os.environ.get(token_env, "")
        if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise RuntimeError("bearer token environment variable missing or invalid")
        headers["authorization"] = "Bearer " + token
    use_tls = kind != "tcp_connect"
    kwargs = {"ssl": ssl.create_default_context(), "server_hostname": host,
              "ssl_handshake_timeout": timeout} if use_tls else {}
    reader, writer = await asyncio.open_connection(host, port, **kwargs)
    try:
        if kind in ("tcp_connect", "tls_connect"):
            return "connection established"
        method = (endpoint or {}).get("method", "POST" if kind == "https_post" else "GET")
        body = b""
        if endpoint is not None and "json" in endpoint:
            body = json.dumps(endpoint["json"], allow_nan=False).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        authority = f"[{host}]" if ":" in host else host
        headers.update({"host": authority, "connection": "close", "content-length": str(len(body))})
        headers.setdefault("user-agent", "traffic-testbed/1.0")
        headers.setdefault("accept", "*/*")
        target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            target += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
        request = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(request.encode("ascii") + b"\r\n" + body)
        await writer.drain()
        # Header-only response sampling bounds memory/bandwidth; no assets or redirects.
        while True:
            response = await reader.readuntil(b"\r\n\r\n")
            first = response.split(b"\r\n", 1)[0].split()
            if len(first) < 2 or first[0] not in (b"HTTP/1.0", b"HTTP/1.1"):
                raise RuntimeError("invalid HTTP response")
            status = int(first[1])
            if not 100 <= status < 200 or status == 101:
                break
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP status={status}; redirects are not followed")
        return f"status={status} (response headers sampled)"
    finally:
        await close_stream(writer)

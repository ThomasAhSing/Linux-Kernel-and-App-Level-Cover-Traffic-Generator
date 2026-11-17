import asyncio
import copy
import json
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_operations as n
import testbed as t


def config():
    return json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())


class Configuration(unittest.TestCase):
    def test_old_configs_default_to_random_browser_visits(self):
        c = config()
        c["browser"].pop("operations")
        c["browser"].pop("http_endpoints")
        t.validate(c)
        with patch.object(n.random, "choice", side_effect=lambda items: items[-1]):
            kind, url, endpoint = n.choose_job(c["browser"])
        self.assertEqual(kind, "browser_get")
        self.assertEqual(url, c["browser"]["urls"][-1])
        self.assertIsNone(endpoint)

    def test_endpoint_validation(self):
        valid = {"type": "https_post", "url": "https://github.com/test", "json": {"hello": "world"}}
        c = config()
        c["browser"]["http_endpoints"] = [valid]
        t.validate(c)
        for change in (
            {"url": "https://unapproved.example.com/"}, {"url": "http://github.com/"},
            {"type": []}, {"method": "DELETE"}, {"headers": {"Host": "elsewhere"}},
            {"headers": {"X-Test": "bad\r\nvalue"}}, {"headers": {"X-Test": "a", "x-test": "b"}},
            {"json": "x" * 65537}, {"json": float("nan")},
            {"bearer_token_env": "bad-name"},
            {"bearer_token_env": "TOKEN", "headers": {"Authorization": "abc"}},
            {"type": "https_api", "method": "GET"},
        ):
            bad = copy.deepcopy(c)
            bad["browser"]["http_endpoints"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                t.validate(bad)

    def test_api_get_without_body_and_post_are_supported(self):
        for method in ("GET", "POST"):
            c = config()
            c["browser"]["http_endpoints"] = [{"type": "https_api", "url": "https://github.com/api",
                                                "method": method, "bearer_token_env": "TEST_API_TOKEN"}]
            t.validate(c)

    def test_operations_are_validated_and_lab_remains_isolated(self):
        for value in ([], ["bad"], ["dns", "dns"], "dns", [True]):
            c = config()
            c["browser"]["operations"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                t.validate(c)
        lab = json.loads((Path(__file__).resolve().parents[1] / "config.lab.example.json").read_text())
        lab["browser"]["operations"] = ["dns"]
        with self.assertRaisesRegex(ValueError, "external mode"):
            t.validate(lab)

    def test_api_uses_its_own_endpoint_not_a_random_homepage(self):
        p = config()["browser"]
        endpoint = {"type": "https_api", "url": "https://github.com/api"}
        p["http_endpoints"] = [endpoint]
        with patch.object(n.random, "choice", side_effect=lambda items: items[-1]):
            self.assertEqual(n.choose_job(p), ("https_api", endpoint["url"], endpoint))


class Operations(unittest.IsolatedAsyncioTestCase):
    def streams(self, header=b"HTTP/1.1 200 OK\r\nContent-Length: 10000000\r\n\r\n"):
        reader = asyncio.StreamReader()
        reader.feed_data(header)
        reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        return reader, writer

    async def test_post_body_tls_and_no_asset_loading(self):
        reader, writer = self.streams()
        endpoint = {"type": "https_post", "json": {"message": "test"}, "bearer_token_env": "TEST_API_TOKEN"}
        with patch.object(n.asyncio, "open_connection", new=AsyncMock(return_value=(reader, writer))) as connect, \
                patch.dict(n.os.environ, {"TEST_API_TOKEN": "secret"}):
            result = await n.execute("https_post", "https://github.com/echo?q=1", endpoint, 5)
        self.assertIn("status=200", result)
        self.assertEqual(connect.call_args.args, ("github.com", 443))
        tls = connect.call_args.kwargs["ssl"]
        self.assertTrue(tls.check_hostname)
        self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(connect.call_args.kwargs["server_hostname"], "github.com")
        request = writer.write.call_args.args[0]
        headers, body = request.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"POST /echo?q=1 HTTP/1.1\r\n"))
        self.assertIn(b"authorization: Bearer secret", headers)
        self.assertIn(f"content-length: {len(body)}".encode(), headers)
        self.assertEqual(json.loads(body), {"message": "test"})
        writer.close.assert_called_once()

    async def test_api_get_and_direct_get(self):
        for kind, endpoint in (("https_get", None), ("https_api", {"method": "GET"})):
            reader, writer = self.streams()
            with patch.object(n.asyncio, "open_connection", new=AsyncMock(return_value=(reader, writer))):
                await n.execute(kind, "https://github.com/status", endpoint, 5)
            self.assertTrue(writer.write.call_args.args[0].startswith(b"GET /status HTTP/1.1\r\n"))

    async def test_redirect_is_not_followed_and_stream_is_closed(self):
        reader, writer = self.streams(b"HTTP/1.1 302 Found\r\nLocation: https://other.example/\r\n\r\n")
        with patch.object(n.asyncio, "open_connection", new=AsyncMock(return_value=(reader, writer))) as connect:
            with self.assertRaisesRegex(RuntimeError, "302"):
                await n.execute("https_get", "https://github.com/", None, 5)
        connect.assert_awaited_once()
        writer.close.assert_called_once()

    async def test_missing_credentials_send_nothing(self):
        with patch.dict(n.os.environ, {}, clear=True), patch.object(n.asyncio, "open_connection", new=AsyncMock()) as connect:
            with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                await n.execute("https_api", "https://github.com/", {"bearer_token_env": "TOKEN"}, 5)
        connect.assert_not_awaited()

    async def test_tcp_and_tls_do_not_send_http(self):
        for kind in ("tcp_connect", "tls_connect"):
            reader, writer = self.streams()
            with patch.object(n.asyncio, "open_connection", new=AsyncMock(return_value=(reader, writer))) as connect:
                await n.execute(kind, "https://github.com/path", None, 5)
            self.assertEqual("ssl" in connect.call_args.kwargs, kind == "tls_connect")
            writer.write.assert_not_called()
            writer.close.assert_called_once()

    async def test_dns_extracts_hostname(self):
        resolver = AsyncMock(return_value=[(None, None, None, None, ("192.0.2.1", 443))])
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
            result = await n.execute("dns", "https://github.com/path?q=1", None, 5)
        self.assertEqual(resolver.call_args.args, ("github.com", 443))
        self.assertIn("192.0.2.1", result)

    async def test_shutdown_and_timeout_cancel_and_drain(self):
        for active, timeout, exception in ((False, 5, RuntimeError), (True, 0, TimeoutError)):
            with self.subTest(active=active):
                pending = asyncio.Event()
                with self.assertRaises(exception):
                    await n.bounded_operation(pending.wait(), timeout, None, lambda _: active)

    async def test_in_flight_timeout_runs_cleanup(self):
        cleaned = asyncio.Event()
        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with self.assertRaises(TimeoutError):
            await n.bounded_operation(operation(), 0.02, None, lambda _: True)
        self.assertTrue(cleaned.is_set())

    async def test_ping_shutdown_kills_and_reaps_child(self):
        entered = asyncio.Event()
        async def communicate():
            entered.set()
            await asyncio.Event().wait()
        process = Mock(returncode=None, communicate=AsyncMock(side_effect=communicate), wait=AsyncMock())
        with patch.object(n.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            task = asyncio.create_task(n.ping("github.com", 5))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(spawn.call_args.args[:5], ("ping", "-n", "-c", "1", "-W"))
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_runner_budgets_probes_and_keeps_browser_guard(self):
        p = config()["browser"]
        p.update(request_limit=3, max_requests=2, interval_seconds=0)
        page = Mock()
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock(), close=AsyncMock(),
                       new_page=AsyncMock(return_value=page))
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        budget = Mock(available=Mock(return_value=True), reserve=Mock(return_value=True))
        with patch.object(t, "allowed", return_value=True), \
                patch.object(t, "choose_job", return_value=("dns", p["urls"][0], None)), \
                patch.object(t, "execute", new=AsyncMock(return_value="resolved")) as execute:
            await t.browser(p, None, launcher, budget)
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(budget.reserve.call_count, 2)
        context.route.assert_awaited_once()
        context.close.assert_awaited_once()

    async def test_probe_budget_persistence_failure_prevents_network_io(self):
        p = config()["browser"]
        p.update(request_limit=1, interval_seconds=0)
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock(), close=AsyncMock(),
                       new_page=AsyncMock(return_value=Mock()))
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        budget = Mock(available=Mock(return_value=True), reserve=Mock(side_effect=OSError("disk full")))
        with patch.object(t, "allowed", return_value=True), \
                patch.object(t, "choose_job", return_value=("dns", p["urls"][0], None)), \
                patch.object(t, "execute", new=AsyncMock()) as execute:
            with self.assertRaisesRegex(RuntimeError, "could not be persisted"):
                await t.browser(p, None, launcher, budget)
        execute.assert_not_called()
        context.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

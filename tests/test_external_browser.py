import asyncio
import contextlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testbed as t
import schedule

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / "config.example.json").read_text())


class ExternalConfiguration(unittest.TestCase):
    def test_disabled_external_template_is_valid(self):
        c = t.validate(config())
        self.assertFalse(c["browser"]["enabled"])
        self.assertEqual(t.browser_mode(c["browser"]), "external")

    def test_multiple_websites_and_paths(self):
        c = config()
        c["browser"]["urls"].append("https://www.google.com/search?q=test")
        t.validate(c)

    def test_unapproved_origin_is_rejected(self):
        c = config()
        c["browser"]["urls"].append("https://unapproved.example.org/")
        with self.assertRaisesRegex(ValueError, "not explicitly authorized"):
            t.validate(c)

    def test_ambiguous_or_non_https_urls_are_rejected(self):
        for url in ("http://www.google.com/", "https://www.google.com@evil.example/",
                    "https://www.google.com\\@evil.example/", "https://www.google.com/\n",
                    "https://127.0.0.1/", "https://10.0.0.1/", "https://[::1]/",
                    "https://localhost/", "https://foo.local/", "https://www.google.com:444/",
                    "https://*.google.com/", "https://www.google.com/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                t.origin(url)

    def test_origins_must_not_contain_paths(self):
        c = config()
        c["browser"]["asset_origins"] = ["https://www.gstatic.com/assets"]
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_conservative_external_limits(self):
        for key, value in (("interval_seconds", 4), ("max_requests", 1001),
                           ("max_requests_per_page", 101), ("max_requests", True)):
            c = config()
            c["browser"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                t.validate(c)

    def test_external_does_not_relax_pktgen_validation(self):
        c = config()
        c["pktgen"].update(enabled=True, destination="8.8.8.8", interface="eth0")
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_exact_origin_normalization(self):
        self.assertEqual(t.origin("https://WWW.GOOGLE.COM:443/test"), "https://www.google.com")
        self.assertNotEqual(t.origin("https://www.google.com.evil.example/"), "https://www.google.com")


class Scheduling(unittest.TestCase):
    def test_external_entry_does_not_require_lab_namespace(self):
        c = config()
        c["browser"]["enabled"] = True
        with patch.object(sys, "argv", ["testbed.py", "manual", "browser"]), \
             patch.object(t, "load", return_value=c), patch.object(t.DISABLED.__class__, "exists", return_value=False), \
             patch.object(t.signal, "signal"), patch.object(t, "exclusive", return_value=contextlib.nullcontext()), \
             patch.object(t, "verify_isolation") as isolation, patch.object(t.asyncio, "run") as run, \
             patch.object(t, "supervised_browser", new=Mock(return_value="workload")):
            t.main()
        isolation.assert_not_called()
        run.assert_called_once_with("workload")

    def test_wrong_service_mode_fails_before_launch(self):
        with patch.object(sys, "argv", ["testbed.py", "run", "browser", "--network-mode", "lab"]), \
             patch.object(t, "load", return_value=config()), patch.object(t.asyncio, "run") as run:
            with self.assertRaisesRegex(ValueError, "does not match"):
                t.main()
        run.assert_not_called()

    def test_selects_external_and_stops_lab_service(self):
        run = Mock(return_value=Mock(returncode=0))
        schedule.reconcile(lambda: True, run, lambda *args: config())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["systemctl", "stop", "traffic-browser-lab.service"], commands)
        self.assertIn(["systemctl", "start", "traffic-browser.service"], commands)
        self.assertIn(["systemctl", "start", "traffic-pktgen.service"], commands)

    def test_invalid_browser_still_reconciles_kernel(self):
        run = Mock(return_value=Mock(returncode=0))
        read = Mock(side_effect=ValueError("invalid browser"))
        with self.assertLogs(level="ERROR"):
            self.assertEqual(schedule.reconcile(lambda: True, run, read), 1)
        self.assertEqual(run.call_args.args[0], ["systemctl", "start", "traffic-pktgen.service"])


class BrowserRoutes(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, requests, max_requests=200, per_page=50):
        routes = []
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock())

        async def navigate(*args, **kwargs):
            guard = context.route.call_args.args[1]
            for url, nav, method in requests:
                request = Mock(url=url, method=method)
                request.is_navigation_request.return_value = nav
                route = Mock(request=request, continue_=AsyncMock(), abort=AsyncMock())
                await guard(route)
                routes.append(route)
            return Mock(ok=True, status=200)

        page = Mock(goto=AsyncMock(side_effect=navigate))
        context.new_page = AsyncMock(return_value=page)
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        p = config()["browser"]
        p.update(request_limit=1, interval_seconds=0, max_requests=max_requests,
                 max_requests_per_page=per_page)
        with patch.object(t, "allowed", return_value=True):
            await t.browser(p, None, launcher)
        process.close.assert_awaited_once()
        self.assertFalse(process.new_context.call_args.kwargs["ignore_https_errors"])
        socket = Mock(close=AsyncMock())
        await context.route_web_socket.call_args.args[1](socket)
        socket.close.assert_awaited_once()
        return routes

    async def test_assets_redirects_and_unapproved_requests(self):
        routes = await self.exercise([
            ("https://www.google.com/", True, "GET"),
            ("https://www.google.com/redirected", True, "GET"),
            ("https://www.gstatic.com/script.js", False, "GET"),
            ("https://www.gstatic.com/", True, "GET"),
            ("https://www.google.com.evil.example/", True, "GET"),
            ("https://unapproved.example.org/", False, "GET"),
            ("https://www.google.com/", False, "POST"),
            ("http://www.google.com/", True, "GET"),
        ])
        for i, route in enumerate(routes):
            (route.continue_ if i < 3 else route.abort).assert_awaited_once()

    async def test_total_request_budget_includes_assets(self):
        routes = await self.exercise([("https://www.google.com/", True, "GET")]*3, max_requests=2)
        routes[0].continue_.assert_awaited_once()
        routes[1].continue_.assert_awaited_once()
        routes[2].abort.assert_awaited_once()

    async def test_per_page_budget(self):
        routes = await self.exercise([("https://www.gstatic.com/a.js", False, "GET")]*2, per_page=1)
        routes[0].continue_.assert_awaited_once()
        routes[1].abort.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

import asyncio
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testbed as t
import schedule

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / "config.lab.example.json").read_text())


def enabled():
    c = config()
    c["browser"]["enabled"] = True
    c["pktgen"].update(enabled=True, destination="10.203.0.2",
                        authorized_destinations=["10.203.0.2"], interface="lab0")
    return c


class Configuration(unittest.TestCase):
    def test_safe_defaults(self):
        c = t.validate(config())
        self.assertFalse(c["browser"]["enabled"])
        self.assertFalse(c["pktgen"]["enabled"])

    def test_enabled_configuration(self):
        t.validate(enabled())

    def test_invalid_destinations(self):
        for dst in ("", "8.8.8.8", "127.0.0.1", "224.0.0.1", "10.203.0.3", "::1", "10.203.0.2\nstart"):
            c = enabled()
            c["pktgen"]["destination"] = dst
            with self.subTest(dst=dst), self.assertRaises(ValueError):
                t.validate(c)

    def test_missing_authorization(self):
        c = enabled()
        c["pktgen"]["authorized_destinations"] = []
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_interface_rejected(self):
        c = enabled()
        c["pktgen"]["interface"] = "eth0"
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_browser_urls(self):
        for url in ("https://example.org", "http://127.0.0.1:8000", "http://10.203.0.2:8000@evil.org",
                    "http://10.203.0.2:8000/", "http://10.203.0.2:8000#x"):
            c = config()
            c["browser"]["base_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                t.validate(c)

    def test_numeric_limits_and_types(self):
        for key, value in (("packet_rate", 1001), ("packet_size", 1515), ("packet_size", True),
                           ("max_bandwidth_bps", 1000001), ("packet_rate", 1.5)):
            c = config()
            c["pktgen"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                t.validate(c)

    def test_nan_interval(self):
        c = config()
        c["browser"]["interval_seconds"] = float("nan")
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_unknown_key(self):
        c = config()
        c["pktgen"]["scheduel"] = False
        with self.assertRaises(ValueError):
            t.validate(c)

    def test_rate_budget_includes_wire_overhead(self):
        p = enabled()["pktgen"]
        p.update(packet_rate=1000, max_bandwidth_bps=10000)
        rate = t.effective_rate(p)
        self.assertEqual(rate, 8)
        self.assertLessEqual(rate * (p["packet_size"] + 24) * 8, 10000)

    def test_component_configuration_independent(self):
        c = config()
        c["pktgen"]["packet_rate"] = -1
        t.validate(c, "browser")
        with self.assertRaises(ValueError):
            t.validate(c, "pktgen")

    def test_dry_run_has_no_linux_dependency(self):
        result = subprocess.run([sys.executable, str(ROOT / "testbed.py"), "validate",
                                 "--config", str(ROOT / "config.example.json")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class Schedule(unittest.TestCase):
    def test_boundaries_all_week(self):
        monday = dt.datetime(2026, 10, 26)
        for day in range(7):
            for hour, minute, expect in ((7, 59, False), (8, 0, True), (16, 59, True), (17, 0, False)):
                date = monday + dt.timedelta(days=day, hours=hour, minutes=minute)
                with self.subTest(date=date):
                    self.assertEqual(t.in_window(date), expect and day < 5)

    def test_dst_offsets_do_not_shift_local_window(self):
        for offset in (-7, -8):
            tz = dt.timezone(dt.timedelta(hours=offset))
            self.assertTrue(t.in_window(dt.datetime(2026, 11, 2, 8, tzinfo=tz)))
            self.assertFalse(t.in_window(dt.datetime(2026, 11, 2, 17, tzinfo=tz)))

    def test_boot_missed_event_and_restart_reconcile_current_time(self):
        for inside in (True, False):
            run = Mock(return_value=Mock(returncode=0))
            self.assertEqual(schedule.reconcile(lambda: inside, run, lambda *a: config()), 0)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[-1].args[0][1], "start" if inside else "stop")

    def test_one_service_failure_does_not_block_other(self):
        run = Mock(side_effect=[Mock(returncode=0), Mock(returncode=1), Mock(returncode=0)])
        self.assertEqual(schedule.reconcile(lambda: True, run, lambda *a: config()), 1)
        self.assertEqual(run.call_count, 3)

    def test_emergency_flag_and_stop_signal(self):
        with patch.object(t.DISABLED.__class__, "exists", return_value=True):
            self.assertFalse(t.allowed(100000000000))
        with patch.object(t, "STOP", True):
            self.assertFalse(t.allowed(100000000000))

    def test_manual_is_bounded_and_schedule_guard_active(self):
        with patch.object(t.DISABLED.__class__, "exists", return_value=False), patch.object(t, "in_window", return_value=False):
            self.assertFalse(t.allowed(None))
            self.assertFalse(t.allowed(1))
            with patch.object(t.time, "monotonic", return_value=1):
                self.assertTrue(t.allowed(2))


class Browser(unittest.IsolatedAsyncioTestCase):
    async def test_boundary_cancels_inflight_navigation_and_closes_browser(self):
        entered = asyncio.Event()

        async def navigate(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        page = Mock(goto=AsyncMock(side_effect=navigate))
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock(), new_page=AsyncMock(return_value=page))
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        task = asyncio.create_task(t.browser(config()["browser"], None, launcher))
        with patch.object(t, "allowed", return_value=True):
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.close.assert_awaited_once()

    async def test_success_failure_counts_and_cleanup(self):
        page = Mock()
        page.goto = AsyncMock(side_effect=[Mock(ok=True, status=200), RuntimeError("timeout")])
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock(), new_page=AsyncMock(return_value=page))
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        p = config()["browser"]
        p.update(request_limit=2, interval_seconds=0)
        with patch.object(t, "allowed", return_value=True), self.assertLogs(t.LOG, level="INFO") as logs:
            await t.browser(p, 1, launcher)
        self.assertEqual(page.goto.await_count, 2)
        process.close.assert_awaited_once()
        self.assertTrue(any("errors=1" in x for x in logs.output))
        guard = context.route.call_args.args[1]
        route = Mock(request=Mock(url="http://8.8.8.8/"), abort=AsyncMock())
        await guard(route)
        route.abort.assert_awaited_once()

    async def test_launch_followed_by_context_failure_closes_browser(self):
        process = Mock(new_context=AsyncMock(side_effect=RuntimeError("context")), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        with self.assertRaises(RuntimeError):
            await t.browser(config()["browser"], None, launcher)
        process.close.assert_awaited_once()


class Kernel(unittest.TestCase):
    def test_finite_batch_start_statistics_and_cleanup(self):
        p = enabled()["pktgen"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kpktgend_0").touch()
            (root / "lab0@tt").write_text("Result: OK: complete pkts-sofar: 100 errors: 0")
            writer = Mock(returncode=0)
            writer.poll.return_value = 0
            with patch.object(t, "PROC", root), patch.object(t, "verify_isolation"), \
                 patch.object(t, "pg") as pg, patch.object(t, "cleanup") as clean, \
                 patch.object(t, "allowed", side_effect=[True, True, False, False]), \
                 patch.object(t.subprocess, "Popen", return_value=writer):
                t.kernel(p, None)
            commands = [x.args[1] for x in pg.call_args_list]
            self.assertIn("count 100", commands)
            self.assertIn("delay 10000000", commands)
            self.assertNotIn("count 0", commands)
            writer.wait.assert_called_once()
            clean.assert_called_once()

    def test_configuration_failure_still_cleans(self):
        fake_proc = Mock()
        fake_proc.glob.return_value = [Path("kpktgend_0")]
        with patch.object(t, "verify_isolation"), patch.object(t, "PROC", fake_proc), \
             patch.object(t, "pg", side_effect=RuntimeError("rejected")), patch.object(t, "cleanup") as clean:
            with self.assertRaises(RuntimeError):
                t.kernel(enabled()["pktgen"], None)
            clean.assert_called_once()

    def test_cleanup_stops_before_reset_and_records_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pgctrl").touch()
            (root / "lab0@tt").write_text("pkts-sofar: 100 errors: 0")
            with patch.object(t, "PROC", root), patch.object(t, "verify_isolation"), patch.object(t, "pg") as pg:
                t.cleanup()
            self.assertEqual([x.args[1] for x in pg.call_args_list], ["stop", "reset"])

    @unittest.skipIf(sys.platform != "linux", "Linux flock required")
    def test_duplicate_instance(self):
        import builtins
        real_open = builtins.open
        with tempfile.TemporaryDirectory() as directory:
            with patch("builtins.open", side_effect=lambda *a, **kw: real_open(Path(directory) / "lock", "a")):
                with t.exclusive("pktgen"):
                    with self.assertRaisesRegex(RuntimeError, "duplicate"):
                        with t.exclusive("pktgen"):
                            self.fail("duplicate lock acquired")
                with t.exclusive("pktgen"):
                    pass  # Released on exit; a subsequent controller can start.


if __name__ == "__main__":
    unittest.main()

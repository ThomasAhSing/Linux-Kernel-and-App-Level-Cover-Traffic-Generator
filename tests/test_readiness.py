import asyncio
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testbed as t
import schedule
from browser_budget import DailyBudget

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / "config.example.json").read_text())


class Budgets(unittest.TestCase):
    def test_cap_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "budget.json"
            budget = DailyBudget(path, 2, lambda: "2026-11-02")
            self.assertTrue(budget.reserve())
            restarted = DailyBudget(path, 2, lambda: "2026-11-02")
            self.assertTrue(restarted.reserve())
            self.assertFalse(restarted.reserve())
            self.assertFalse(DailyBudget(path, 2, lambda: "2026-11-02").available())

    def test_local_day_rollover_and_backward_clock(self):
        with tempfile.TemporaryDirectory() as d:
            day = "2026-11-02"
            budget = DailyBudget(Path(d) / "budget.json", 1, lambda: day)
            self.assertTrue(budget.reserve())
            day = "2026-11-03"
            self.assertTrue(budget.reserve())
            day = "2026-11-02"
            self.assertFalse(budget.reserve())

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "budget.json"
            for contents in ("broken", '[]', '{"invalid-date": 1}', '{"2026-11-02": true}', '{"2026-11-02": -1}'):
                path.write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    DailyBudget(path, 10)

    def test_write_failure_is_not_silently_refunded_or_sent(self):
        with tempfile.TemporaryDirectory() as d:
            budget = DailyBudget(Path(d) / "budget.json", 2)
            with patch("browser_budget.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    budget.reserve()
            self.assertFalse(budget.path.exists())
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_repeat_options_are_optional_and_validated(self):
        c = config()
        for field in ("repeat_during_window", "cycle_pause_seconds", "daily_request_limit"):
            c["browser"].pop(field, None)
        t.validate(c)
        for field, value in (("repeat_during_window", "true"), ("cycle_pause_seconds", 0),
                             ("daily_request_limit", 10001), ("daily_request_limit", True)):
            c = config()
            c["browser"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                t.validate(c)


class CycleLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_budget_write_failure_blocks_request_and_fails_workload(self):
        p = config()["browser"]
        p.update(request_limit=1, interval_seconds=0)
        context = Mock(route=AsyncMock(), route_web_socket=AsyncMock(), close=AsyncMock())
        request = Mock(url=p["urls"][0], method="GET")
        request.is_navigation_request.return_value = True
        route = Mock(request=request, continue_=AsyncMock(), abort=AsyncMock())
        async def goto(*args, **kwargs):
            await context.route.call_args.args[1](route)
            raise RuntimeError("navigation aborted")
        page = Mock(goto=AsyncMock(side_effect=goto))
        context.new_page = AsyncMock(return_value=page)
        process = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        launcher = Mock(launch=AsyncMock(return_value=process))
        budget = Mock(available=Mock(return_value=True), reserve=Mock(side_effect=OSError("disk full")))
        with patch.object(t, "allowed", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "could not be persisted"):
                await t.browser(p, None, launcher, budget)
        route.continue_.assert_not_awaited()
        route.abort.assert_awaited_once()
        context.close.assert_awaited_once()
        process.close.assert_awaited_once()

    async def test_repeat_is_opt_in_and_manual_never_replenishes(self):
        p = config()["browser"]
        with patch.object(t, "browser", new=AsyncMock()) as workload, patch.object(t, "DailyBudget") as persistent:
            await t.supervised_browser(p, None)
            p["repeat_during_window"] = True
            await t.supervised_browser(p, 123)
        self.assertEqual(workload.await_count, 2)
        persistent.assert_not_called()

    async def test_repeats_after_cooldown_but_respects_daily_cap(self):
        p = config()["browser"]
        p.update(repeat_during_window=True, cycle_pause_seconds=60, daily_request_limit=2)
        with tempfile.TemporaryDirectory() as d:
            async def workload(settings, deadline, budget=None):
                self.assertTrue(budget.reserve())
            with patch.object(t, "BROWSER_STATE", Path(d) / "budget.json"), \
                 patch.object(t, "allowed", return_value=True), \
                 patch.object(t, "time", new=Mock(monotonic=Mock(side_effect=[0, 61]))), \
                 patch.object(t, "browser", new=AsyncMock(side_effect=workload)) as run:
                await t.supervised_browser(p, None)
            self.assertEqual(run.await_count, 2)
            self.assertFalse(DailyBudget(Path(d) / "budget.json", 2).available())

    async def test_shutdown_interrupts_cooldown_without_new_cycle(self):
        p = config()["browser"]
        p["repeat_during_window"] = True
        active = True
        async def sleep(seconds):
            nonlocal active
            active = False
        with tempfile.TemporaryDirectory() as d, \
             patch.object(t, "BROWSER_STATE", Path(d) / "budget.json"), \
             patch.object(t, "allowed", side_effect=lambda deadline: active), \
             patch.object(t.asyncio, "sleep", new=sleep), \
             patch.object(t, "browser", new=AsyncMock()) as workload:
            await t.supervised_browser(p, None)
        workload.assert_awaited_once()

    async def test_navigation_shutdown_retrieves_error_without_cancelling_api(self):
        entered = asyncio.Event()
        pending = asyncio.get_running_loop().create_future()
        active = True
        async def goto(*args, **kwargs):
            entered.set()
            return await pending
        async def close():
            self.assertFalse(pending.cancelled())
            pending.set_exception(RuntimeError("TargetClosedError"))
        page = Mock(goto=AsyncMock(side_effect=goto), close=AsyncMock(side_effect=close))
        with patch.object(t, "allowed", side_effect=lambda deadline: active):
            task = asyncio.create_task(t.navigation(page, "https://example.org/", 15, None))
            await entered.wait()
            active = False
            self.assertIsNone(await task)
        self.assertFalse(pending.cancelled())
        page.close.assert_awaited_once()

    async def test_unexpected_caller_cancellation_also_drains_navigation(self):
        entered = asyncio.Event()
        pending = asyncio.get_running_loop().create_future()
        async def goto(*args, **kwargs):
            entered.set()
            return await pending
        async def close():
            pending.set_exception(RuntimeError("page closed"))
        page = Mock(goto=AsyncMock(side_effect=goto), close=AsyncMock(side_effect=close))
        with patch.object(t, "allowed", return_value=True):
            task = asyncio.create_task(t.navigation(page, "https://example.org/", 15, None))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(pending.cancelled())


class SchedulingAcceptance(unittest.TestCase):
    def test_manual_restart_cannot_bypass_closed_window(self):
        c = config()
        c["browser"]["enabled"] = True
        with patch.object(sys, "argv", ["testbed.py", "run", "browser"]), \
             patch.object(t, "load", return_value=c), patch.object(t, "in_window", return_value=False), \
             patch.object(t.DISABLED.__class__, "exists", return_value=False), \
             patch.object(t, "exclusive") as lock, patch.object(t.asyncio, "run") as launch:
            t.main()
        lock.assert_not_called()
        launch.assert_not_called()

    def test_boot_recovery_before_during_after_and_weekend(self):
        for date, action in ((dt.datetime(2026, 11, 2, 7, 59), "stop"),
                             (dt.datetime(2026, 11, 2, 12), "start"),
                             (dt.datetime(2026, 11, 2, 17), "stop"),
                             (dt.datetime(2026, 11, 7, 12), "stop")):
            execute = Mock(return_value=Mock(returncode=0))
            schedule.reconcile(lambda: t.in_window(date), execute, lambda *args: config())
            commands = [call.args[0] for call in execute.call_args_list]
            with self.subTest(date=date):
                self.assertIn(["systemctl", action, "traffic-pktgen.service"], commands)
                self.assertIn(["systemctl", action, "traffic-browser.service"], commands)

    def test_dst_uses_real_local_timezone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            zone = ZoneInfo("America/Vancouver")
        except ZoneInfoNotFoundError:
            self.skipTest("IANA timezone database not installed")
        for date in (dt.datetime(2026, 3, 6), dt.datetime(2026, 3, 9), dt.datetime(2026, 11, 2)):
            self.assertTrue(t.in_window(date.replace(hour=8, tzinfo=zone)))
            self.assertFalse(t.in_window(date.replace(hour=17, tzinfo=zone)))


if __name__ == "__main__":
    unittest.main()

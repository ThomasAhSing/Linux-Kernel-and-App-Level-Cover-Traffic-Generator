"""Opt-in real Chromium regression, localhost only; no public website requests.

Run with the installed venv: python tests/browser_shutdown_live.py -v
The localhost fixture is injected directly into the workload, not production
configuration. No production schedule bypass or additional destination is added.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import testbed as t


class ChromiumShutdown(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, use_signal):
        from playwright.async_api import async_playwright
        stop = asyncio.Event()
        release = asyncio.Event()
        requested = asyncio.Event()
        connections = set()
        failures = []
        browsers = []
        stopped_at = []
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: failures.append(context))

        async def respond(reader, writer):
            task = asyncio.current_task()
            connections.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                requested.set()
                def callback():
                    stopped_at.append(time.monotonic())
                    if use_signal:
                        t.stop_signal(signal.SIGTERM, None)
                    else:
                        stop.set()
                loop.call_later(0.2, callback)
                # Keep navigation genuinely in flight at the stop boundary.
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                connections.discard(task)

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with async_playwright() as api:
                class Launcher:
                    async def launch(self, **kwargs):
                        executable = os.environ.get("TRAFFIC_TESTBED_BROWSER_EXECUTABLE")
                        if executable:
                            kwargs["executable_path"] = executable
                        instance = await api.chromium.launch(**kwargs)
                        browsers.append(instance)
                        return instance
                p = json.loads((Path(__file__).parents[1] / "config.lab.example.json").read_text())["browser"]
                p.update(base_url=f"http://127.0.0.1:{port}", pages=["/"],
                         authorized_origins=[f"http://127.0.0.1:{port}"], request_limit=10)
                with tempfile.TemporaryDirectory() as directory, \
                     patch.object(t, "DISABLED", Path(directory) / "DISABLED"), \
                     patch.object(t, "STOP", False), patch.object(t, "in_window", side_effect=lambda: not stop.is_set()):
                    await asyncio.wait_for(t.browser(p, None, Launcher()), timeout=15)
                self.assertTrue(stopped_at)
                self.assertLess(time.monotonic() - stopped_at[0], 3,
                                "shutdown waited for the page timeout instead of interrupting the operation")
                await asyncio.sleep(0.1)
            self.assertTrue(requested.is_set(), "navigation never reached the local server")
            self.assertTrue(browsers)
            self.assertTrue(all(not x.is_connected() for x in browsers))
            self.assertEqual(failures, [], f"unhandled asynchronous errors: {failures}")
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            if connections:
                await asyncio.gather(*list(connections), return_exceptions=True)
            loop.set_exception_handler(original_handler)

    async def test_operating_window_closes_during_page_load(self):
        await self.exercise(use_signal=False)

    async def test_sigterm_handler_during_page_load(self):
        await self.exercise(use_signal=True)


if __name__ == "__main__":
    unittest.main()

"""Portable checks for integration sequencing and error reporting."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import integration


class Readiness(unittest.TestCase):
    def test_kernel_readiness_requires_actual_nonzero_packets(self):
        logs = Mock(side_effect=["pktgen configured", "pktgen batch statistics pkts-sofar: 0",
                                 "pktgen batch statistics pkts-sofar: 100"])
        integration.wait_pktgen_ready("id", Mock(poll=Mock(return_value=None)),
                                       clock=Mock(side_effect=[0, 1, 2, 3]), pause=Mock(), read_logs=logs)
        self.assertEqual(logs.call_count, 3)

    def test_waits_for_http_success_not_service_active_or_five_seconds(self):
        ticks = iter([0, 1, 6, 12])
        logs = Mock(side_effect=["service started", "Chromium starting", "browser request success status=200"])
        pause = Mock()
        result = integration.wait_browser_ready("id", Mock(poll=Mock(return_value=None)),
                                                clock=lambda: next(ticks), pause=pause, read_logs=logs)
        self.assertIn("status=200", result)
        self.assertEqual(pause.call_count, 2)

    def test_reports_early_browser_failure_and_its_real_logs(self):
        with self.assertRaisesRegex(RuntimeError, "driver connection failed"):
            integration.wait_browser_ready("id", Mock(poll=Mock(return_value=1)),
                                            clock=Mock(side_effect=[0, 1]),
                                            read_logs=lambda invocation: "driver connection failed")

    def test_timeout_reports_latest_diagnostic(self):
        with self.assertRaisesRegex(RuntimeError, "still starting"):
            integration.wait_browser_ready("id", Mock(poll=Mock(return_value=None)), timeout=2,
                                            clock=Mock(side_effect=[0, 1, 3]), pause=Mock(),
                                            read_logs=lambda invocation: "still starting")

    def test_cleanup_skips_collected_transient_units(self):
        with patch.object(integration.subprocess, "run", return_value=Mock(stdout="not-found\n")) as run:
            integration.stop_if_loaded("traffic-manual-browser.service")
        self.assertEqual(run.call_count, 1)

    def test_cleanup_stops_a_loaded_unit(self):
        with patch.object(integration.subprocess, "run", return_value=Mock(stdout="loaded\n")) as run:
            integration.stop_if_loaded("traffic-manual-browser.service")
        self.assertEqual(run.call_args.args[0], ["systemctl", "stop", "traffic-manual-browser.service"])


if __name__ == "__main__":
    unittest.main()

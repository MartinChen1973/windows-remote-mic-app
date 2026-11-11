"""Suspend-aware time contract for the Google receiver.

The receiver is two processes (owner and elevated worker) that exchange
heartbeats and report stamps. Both must agree on elapsed *running* time. On
Windows ``time.monotonic()`` keeps counting through suspend and hibernate, so
every liveness budget expired at the instant of resume: the worker ended its own
run before it could drain the heartbeats the owner had already queued.
"""
import time
import unittest
from unittest import mock

from ovb_rc003 import chromecast_channel, chromecast_runtime, suspend_clock
from ovb_rc003.chromecast_channel import ChannelError, ParentLease


class ClockContractTests(unittest.TestCase):
    def test_clock_never_exceeds_the_suspend_inclusive_base(self):
        # The unbiased reading removes the suspend bias, so it can never run
        # ahead of the ordinary monotonic base on the same host.
        self.assertLessEqual(suspend_clock.now(), time.monotonic())

    def test_clock_excludes_a_simulated_suspend(self):
        with mock.patch.object(suspend_clock, "now", return_value=100.0):
            started = suspend_clock.now()
        with mock.patch.object(suspend_clock, "now", return_value=102.0):
            # 55 minutes of hibernation is invisible to the budget.
            self.assertEqual(suspend_clock.now() - started, 2.0)

    def test_lease_survives_a_suspend_longer_than_its_heartbeat_window(self):
        # A real hibernation is far longer than HEARTBEAT_TIMEOUT. Because both
        # sides read the suspend-exclusive clock, the lease is still fresh.
        lease = ParentLease(1000.0)
        self.assertTrue(lease.alive(1000.0 + 5.0))
        self.assertFalse(lease.alive(1000.0 + chromecast_channel.HEARTBEAT_TIMEOUT + 0.5))

    def test_lease_still_expires_when_time_really_passes(self):
        # The contract must not weaken ordinary liveness detection.
        lease = ParentLease(1000.0)
        with self.assertRaises(ChannelError):
            lease.renew(1000.0 + chromecast_channel.HEARTBEAT_TIMEOUT + 1.0)
        self.assertTrue(lease.closed)

    def test_resume_detector_reports_one_resume_per_suspend(self):
        detector = suspend_clock.ResumeDetector()
        self.assertFalse(detector.resumed())
        # A suspend shows up as the biased clock pulling ahead.
        with mock.patch.object(time, "monotonic", return_value=time.monotonic() + 3600):
            self.assertTrue(detector.resumed())
            self.assertFalse(detector.resumed())


class RecoveryBudgetTests(unittest.TestCase):
    def test_resume_schedule_is_bounded_and_never_shorter(self):
        self.assertEqual(
            len(chromecast_channel.RESUME_RECOVERY_DELAYS),
            len(chromecast_channel.RECOVERY_DELAYS),
        )
        for resume, ordinary in zip(
            chromecast_channel.RESUME_RECOVERY_DELAYS,
            chromecast_channel.RECOVERY_DELAYS,
        ):
            self.assertGreaterEqual(resume, ordinary)

    def test_lost_cause_reports_recovering_while_a_retry_remains(self):
        services = mock.Mock()
        owner = chromecast_runtime.ChromecastRuntime(services)
        owner.client = mock.Mock(reason="peer_lost")
        self.assertEqual(owner.loss_state(), "chromecast_recovering")

    def test_terminal_cause_reports_its_own_reason(self):
        services = mock.Mock()
        owner = chromecast_runtime.ChromecastRuntime(services)
        owner.client = mock.Mock(reason="unsupported_layout")
        self.assertEqual(owner.loss_state(), "chromecast_unsupported_layout")


if __name__ == "__main__":
    unittest.main()

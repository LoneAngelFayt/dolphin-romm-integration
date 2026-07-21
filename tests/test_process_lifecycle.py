"""Dolphin's lifecycle: crash relaunch, launch serialisation, display wait.

None of this has an HTTP surface, and all of it fails the same way in
production: the stream goes black and the logs fill with identical lines.
"""

import time
import unittest
import unittest.mock
from pathlib import Path

from support import broker, reset_session


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid

    def wait(self):
        return 0

    def poll(self):
        return 0


class CrashRelaunch(unittest.TestCase):
    """_monitor_process respawns the dashboard, but must not do so forever."""

    def setUp(self):
        reset_session()
        self.proc = FakeProc()
        with broker._session_lock:
            broker._session["process"] = self.proc
            broker._session["is_managed"] = True
        self.relaunches = 0
        self.sleeps = []
        self.patches = [
            unittest.mock.patch.object(broker, "_launch_dolphin", self._relaunch),
            unittest.mock.patch.object(broker.time, "sleep", self.sleeps.append),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        reset_session()

    def _relaunch(self, rom_path, state_path=None):
        self.relaunches += 1

    def monitor(self, lifetime):
        """Run one _monitor_process cycle for a process that lived `lifetime`."""
        broker._monitor_process(self.proc, time.monotonic() - lifetime)

    def test_a_healthy_exit_relaunches_promptly(self):
        self.monitor(broker.RELAUNCH_HEALTHY_SECONDS + 1)
        self.assertEqual(self.relaunches, 1)
        self.assertEqual(self.sleeps, [1])

    def test_a_healthy_exit_clears_earlier_failures(self):
        with broker._session_lock:
            broker._session["relaunch_failures"] = 3
        self.monitor(broker.RELAUNCH_HEALTHY_SECONDS + 1)
        with broker._session_lock:
            self.assertEqual(broker._session["relaunch_failures"], 0)

    def test_repeated_instant_exits_back_off(self):
        waits = []
        for _ in range(3):
            self.sleeps.clear()
            self.monitor(0.1)
            waits.append(self.sleeps[0])
        self.assertEqual(waits, sorted(waits))
        self.assertLess(waits[0], waits[-1])

    def test_the_backoff_is_capped(self):
        with broker._session_lock:
            broker._session["relaunch_failures"] = broker.RELAUNCH_MAX_FAILURES - 1
        self.monitor(0.1)
        self.assertLessEqual(self.sleeps[0], broker.RELAUNCH_BACKOFF_CAP)

    def test_a_crash_loop_is_eventually_abandoned(self):
        # Uncapped, a Dolphin that dies on startup respawned forever and buried
        # the reason under thousands of identical log lines.
        for _ in range(broker.RELAUNCH_MAX_FAILURES + 2):
            self.monitor(0.1)
        self.assertEqual(self.relaunches, broker.RELAUNCH_MAX_FAILURES)
        with broker._session_lock:
            self.assertFalse(broker._session["is_managed"])

    def test_an_unmanaged_session_is_not_relaunched(self):
        with broker._session_lock:
            broker._session["is_managed"] = False
        self.monitor(0.1)
        self.assertEqual(self.relaunches, 0)

    def test_a_superseded_process_is_not_relaunched(self):
        with broker._session_lock:
            broker._session["process"] = FakeProc(pid=9999)
        self.monitor(0.1)
        self.assertEqual(self.relaunches, 0)


class LaunchSerialisation(unittest.TestCase):
    def test_launches_do_not_interleave(self):
        # Two concurrent /launch requests used to interleave their
        # kill/patch/spawn steps and leave two Dolphins running, or none.
        from threading import Thread

        overlaps = []
        active = []

        def slow_inner(rom_path, state_path=None):
            active.append(rom_path)
            overlaps.append(len(active))
            time.sleep(0.05)
            active.pop()

        with unittest.mock.patch.object(
            broker, "_launch_dolphin_locked", slow_inner
        ):
            threads = [
                Thread(target=broker._launch_dolphin, args=(f"rom{i}",))
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(overlaps, [1, 1, 1, 1])


class DisplayWait(unittest.TestCase):
    """The broker used to sleep a flat 5 s and hope the desktop was up."""

    def test_returns_as_soon_as_the_x_socket_exists(self):
        with unittest.mock.patch.object(Path, "exists", lambda self: True):
            started = time.monotonic()
            self.assertTrue(broker._wait_for_display(timeout=5))
        self.assertLess(time.monotonic() - started, 1)

    def test_gives_up_and_starts_anyway(self):
        # Starting without a display is better than never answering /health.
        with unittest.mock.patch.object(Path, "exists", lambda self: False):
            self.assertFalse(broker._wait_for_display(timeout=0.3))


if __name__ == "__main__":
    unittest.main()

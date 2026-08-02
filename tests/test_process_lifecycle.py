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

    def test_a_launch_during_the_backoff_is_not_stomped_on(self):
        # The monitor sleeps up to a minute. A /launch landing in that window
        # installs a new process, and a monitor that only rechecked is_managed
        # went on to relaunch the dashboard over the game just started.
        def launch_lands(_seconds):
            with broker._session_lock:
                broker._session["process"] = FakeProc(pid=777)

        with unittest.mock.patch.object(broker.time, "sleep", launch_lands):
            self.monitor(0.1)
        self.assertEqual(self.relaunches, 0)

    def test_a_superseded_process_is_not_relaunched(self):
        with broker._session_lock:
            broker._session["process"] = FakeProc(pid=9999)
        self.monitor(0.1)
        self.assertEqual(self.relaunches, 0)


class KillProc:
    """A Popen stand-in with a scriptable poll() and wait()."""

    def __init__(self, pid=4242, poll_returns=None, wait_timeouts=0):
        self.pid = pid
        self._poll = list(poll_returns) if poll_returns else [None]
        self._wait_timeouts = wait_timeouts  # leading wait() calls that time out
        self.waits = []

    def poll(self):
        return self._poll.pop(0) if len(self._poll) > 1 else self._poll[0]

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._wait_timeouts > 0:
            self._wait_timeouts -= 1
            raise broker.subprocess.TimeoutExpired(cmd="dolphin", timeout=timeout)
        return 0


class GracefulQuit(unittest.TestCase):
    """Dolphin only flushes GUI graphics changes to GFX.ini on a clean Qt
    shutdown; a group SIGTERM discards them. Teardown asks for the clean quit
    first and only signals if that is ignored."""

    def test_alt_f4_is_sent_and_a_clean_exit_skips_the_signal(self):
        proc = KillProc(poll_returns=[0])  # already down by the first poll
        sent = []
        with unittest.mock.patch.object(broker, "_xdotool_find_window", lambda: "42"), \
             unittest.mock.patch.object(broker, "_xdotool_key",
                                        lambda wid, key: sent.append((wid, key)) or True):
            self.assertTrue(broker._graceful_quit(proc, timeout=2))
        self.assertEqual(sent, [("42", "alt+F4")])

    def test_no_window_means_no_graceful_quit(self):
        called = []
        with unittest.mock.patch.object(broker, "_xdotool_find_window", lambda: None), \
             unittest.mock.patch.object(broker, "_xdotool_key",
                                        lambda *a: called.append(a) or True):
            self.assertFalse(broker._graceful_quit(KillProc(), timeout=2))
        self.assertEqual(called, [])  # never tried to send a key

    def test_a_process_that_ignores_alt_f4_reports_failure(self):
        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        proc = KillProc(poll_returns=[None])  # never exits on its own
        with unittest.mock.patch.object(broker, "_xdotool_find_window", lambda: "42"), \
             unittest.mock.patch.object(broker, "_xdotool_key", lambda *a: True), \
             unittest.mock.patch.object(broker.time, "sleep", lambda _s: None), \
             unittest.mock.patch.object(broker.time, "monotonic", lambda: next(clock)):
            self.assertFalse(broker._graceful_quit(proc, timeout=5))


class KillDolphin(unittest.TestCase):
    def setUp(self):
        reset_session()

    def tearDown(self):
        reset_session()

    def _install(self, proc):
        with broker._session_lock:
            broker._session["process"] = proc
            broker._session["is_managed"] = True

    def test_a_clean_graceful_quit_sends_no_signal(self):
        self._install(KillProc())
        killed = []
        with unittest.mock.patch.object(broker, "_graceful_quit", lambda proc: True), \
             unittest.mock.patch.object(broker.os, "killpg",
                                        lambda *a: killed.append(a)):
            broker._kill_dolphin()
        self.assertEqual(killed, [])

    def test_a_stubborn_process_falls_back_to_sigterm(self):
        proc = KillProc()
        self._install(proc)
        killed = []
        with unittest.mock.patch.object(broker, "_graceful_quit", lambda proc: False), \
             unittest.mock.patch.object(broker.os, "getpgid", lambda pid: pid), \
             unittest.mock.patch.object(broker.os, "killpg",
                                        lambda pgid, sig: killed.append(sig)):
            broker._kill_dolphin()
        self.assertEqual(killed, [broker.signal.SIGTERM])

    def test_a_process_surviving_sigterm_is_sigkilled(self):
        proc = KillProc(wait_timeouts=1)  # ignores SIGTERM, then dies
        self._install(proc)
        killed = []
        with unittest.mock.patch.object(broker, "_graceful_quit", lambda proc: False), \
             unittest.mock.patch.object(broker.os, "getpgid", lambda pid: pid), \
             unittest.mock.patch.object(broker.os, "killpg",
                                        lambda pgid, sig: killed.append(sig)):
            broker._kill_dolphin()
        self.assertEqual(killed, [broker.signal.SIGTERM, broker.signal.SIGKILL])


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

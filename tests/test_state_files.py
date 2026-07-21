"""Savestate discovery and write confirmation.

Confirming the write is what stops a kill landing mid-flush and truncating the
state, so the slot-matching and stability rules are pinned here.
"""

import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from support import broker, write


class StateDirResolution(unittest.TestCase):
    def test_env_override_wins_over_the_probed_candidates(self):
        with unittest.mock.patch.dict("os.environ", {"SSTATE_DIR": "/custom/states"}):
            self.assertEqual(broker._sstate_dir(), Path("/custom/states"))

    def test_returns_none_when_no_candidate_exists(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=False):
            with unittest.mock.patch.object(
                broker, "_SSTATE_DIR_CANDIDATES", (Path("/nope/a"), Path("/nope/b"))
            ):
                self.assertIsNone(broker._sstate_dir())


class NewestStateForSlot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = unittest.mock.patch.dict(
            "os.environ", {"SSTATE_DIR": str(self.dir)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_returns_none_when_the_slot_has_no_state(self):
        self.assertIsNone(broker._newest_state_for_slot(8))

    def test_matches_the_slot_suffix_exactly(self):
        write(self.dir / "GALE01.s08")
        self.assertIsNone(broker._newest_state_for_slot(1))
        self.assertIsNotNone(broker._newest_state_for_slot(8))

    def test_slot_is_zero_padded_to_two_digits(self):
        write(self.dir / "GALE01.s1")
        self.assertIsNone(broker._newest_state_for_slot(1))
        write(self.dir / "GALE01.s01")
        self.assertIsNotNone(broker._newest_state_for_slot(1))

    def test_picks_the_newest_when_several_games_share_a_slot(self):
        write(self.dir / "OLD001.s08", mtime=time.time() - 600)
        newest = write(self.dir / "NEW001.s08", mtime=time.time())
        self.assertEqual(broker._newest_state_for_slot(8), newest)

    def test_ignores_the_undo_backup(self):
        write(self.dir / "lastState.sav")
        self.assertIsNone(broker._newest_state_for_slot(8))


class WaitForStateWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, before, timeout=3.0, slot=8):
        return broker._wait_for_sstate_write(
            self.dir, before, time.monotonic() + timeout, slot
        )

    def test_times_out_when_nothing_is_written(self):
        self.assertFalse(self.wait(broker._sstate_snapshot(self.dir), timeout=0.5))

    def test_confirms_a_newly_created_state(self):
        before = broker._sstate_snapshot(self.dir)
        write(self.dir / "GALE01.s08", b"state")
        self.assertTrue(self.wait(before))

    def test_confirms_an_overwrite_of_an_existing_state(self):
        target = write(self.dir / "GALE01.s08", b"old", mtime=time.time() - 600)
        before = broker._sstate_snapshot(self.dir)
        write(target, b"new")
        self.assertTrue(self.wait(before))

    def test_ignores_writes_to_other_slots(self):
        before = broker._sstate_snapshot(self.dir)
        write(self.dir / "GALE01.s01", b"other slot")
        self.assertFalse(self.wait(before, timeout=0.8, slot=8))

    def test_ignores_the_undo_backup_file(self):
        before = broker._sstate_snapshot(self.dir)
        write(self.dir / "lastState.sav", b"undo")
        self.assertFalse(self.wait(before, timeout=0.8))

    def test_waits_for_the_size_to_stop_growing(self):
        target = self.dir / "GALE01.s08"
        before = broker._sstate_snapshot(self.dir)
        stop = threading.Event()

        def grow():
            data = b""
            for _ in range(6):
                if stop.is_set():
                    return
                data += b"x" * 1024
                target.write_bytes(data)
                time.sleep(0.15)

        writer = threading.Thread(target=grow, daemon=True)
        start = time.monotonic()
        writer.start()
        confirmed = self.wait(before, timeout=5.0)
        stop.set()
        writer.join(timeout=2)
        self.assertTrue(confirmed)
        self.assertGreaterEqual(time.monotonic() - start, 0.9)

    def test_survives_a_state_file_deleted_mid_wait(self):
        target = write(self.dir / "GALE01.s08", b"partial")
        before = {}
        target.unlink()
        self.assertFalse(self.wait(before, timeout=0.8))

    def test_a_missing_state_dir_is_not_an_exception(self):
        missing = Path(self.tmp.name, "gone")
        self.assertEqual(broker._sstate_snapshot(missing), {})
        self.assertFalse(
            broker._wait_for_sstate_write(missing, {}, time.monotonic() + 0.3, 8)
        )


if __name__ == "__main__":
    unittest.main()

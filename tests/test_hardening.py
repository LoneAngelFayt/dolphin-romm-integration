"""Guards against hostile or interrupted input: zip bombs, planted symlinks,
and a memory-card swap that died halfway through.

These are the paths where a bad archive or a badly timed crash costs the user
their saves, so each one is pinned to the specific failure it prevents.
"""

import io
import os
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from support import broker, make_zip, write


def lying_zip(name: str, payload: bytes, claimed: int) -> bytes:
    """A zip whose headers understate a member's real size.

    The declared size is what the cheap up-front sum(file_size) check reads, so
    a member that lies about it is the archive that gets waved through.
    """
    raw = make_zip({name: payload})
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        real = zf.infolist()[0].file_size
    # The size appears in both the local header and the central directory, so
    # patch every occurrence rather than hunting for the two offsets.
    return raw.replace(real.to_bytes(4, "little"), claimed.to_bytes(4, "little"))


class ZipBombs(unittest.TestCase):
    """A member that decompresses past the limit is refused, not written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "dolphin-emu")
        self.root.mkdir(parents=True)
        self.env = unittest.mock.patch.dict(
            os.environ, {"SAVE_DATA_ROOT": str(self.root)}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_a_save_member_over_the_budget_is_refused(self):
        payload = b"A" * 4096
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 1024):
            result = broker._extract_save_archive(make_zip({"GC/big.gci": payload}))
        self.assertIsInstance(result, str)
        self.assertFalse((self.root / "GC" / "big.gci").exists())

    def test_a_member_that_lies_about_its_size_is_a_rejection_not_a_crash(self):
        # The cheap sum(file_size) pass sees 1 byte and waves it through, so the
        # lie is only caught during the read, where zipfile raises BadZipFile.
        # That has to come back as a refused archive, not an unhandled 500.
        content = lying_zip("GC/bomb.gci", b"A" * 65536, claimed=1)
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 4096):
            result = broker._extract_save_archive(content)
        self.assertIsInstance(result, str)
        self.assertFalse((self.root / "GC" / "bomb.gci").exists())

    def test_a_card_member_over_the_budget_leaves_the_old_card_intact(self):
        card = self.root / "romm" / "Card A"
        write(card / "keep.gci", b"original")
        with unittest.mock.patch.dict(os.environ, {"GCI_CARD_DIR": str(card)}):
            with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 1024):
                result = broker._replace_memory_card(
                    make_zip({"big.gci": b"A" * 4096})
                )
        self.assertIsInstance(result, str)
        self.assertEqual((card / "keep.gci").read_bytes(), b"original")


class SymlinkedDirectories(unittest.TestCase):
    """A symlinked directory is never descended into when building an archive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "dolphin-emu")
        self.outside = Path(self.tmp.name, "outside")
        write(self.outside / "secret.txt", b"not yours")
        self.root.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_the_card_archive_skips_a_planted_symlink(self):
        card = self.root / "romm" / "Card A"
        write(card / "real.gci", b"card")
        (card / "escape").symlink_to(self.outside, target_is_directory=True)
        with unittest.mock.patch.dict(os.environ, {"GCI_CARD_DIR": str(card)}):
            archive = broker._build_memory_card_archive()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = {i.filename for i in zf.infolist()}
        self.assertEqual(names, {"real.gci"})

    def test_the_save_archive_skips_a_planted_symlink(self):
        write(self.root / "GC" / "real.gci", b"save")
        (self.root / "GC" / "escape").symlink_to(self.outside, target_is_directory=True)
        found = {p.name for p in broker._iter_save_files(self.root)}
        self.assertEqual(found, {"real.gci"})

    def test_the_screenshot_scan_skips_a_planted_symlink(self):
        shots = self.root / "ScreenShots"
        write(shots / "GALE01" / "shot.png", b"png")
        write(self.outside / "elsewhere.png", b"png")
        (shots / "escape").symlink_to(self.outside, target_is_directory=True)
        found = {p.name for p in broker._screenshot_snapshot(shots)}
        self.assertEqual(found, {"shot.png"})


class InterruptedCardSwap(unittest.TestCase):
    """A card stranded in the backup dir is reclaimed on the next startup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.card = Path(self.tmp.name, "dolphin-emu", "romm", "Card A")
        self.backup = self.card.parent / f".{self.card.name}.old"
        self.env = unittest.mock.patch.dict(
            os.environ, {"GCI_CARD_DIR": str(self.card)}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_the_backup_is_restored_when_no_card_is_present(self):
        write(self.backup / "save.gci", b"the only copy")
        broker._recover_memory_card()
        self.assertEqual((self.card / "save.gci").read_bytes(), b"the only copy")
        self.assertFalse(self.backup.exists())

    def test_a_live_card_is_never_overwritten_by_a_stale_backup(self):
        write(self.card / "save.gci", b"live")
        write(self.backup / "save.gci", b"stale")
        broker._recover_memory_card()
        self.assertEqual((self.card / "save.gci").read_bytes(), b"live")

    def test_recovery_is_a_no_op_with_nothing_to_recover(self):
        broker._recover_memory_card()
        self.assertFalse(self.card.exists())


class SaveIdleWait(unittest.TestCase):
    """The wait reports whether it succeeded, so callers can refuse to serve."""

    def tearDown(self):
        with broker._session_lock:
            broker._session["save_in_progress"] = False

    def test_it_returns_true_when_no_save_is_running(self):
        with broker._session_lock:
            broker._session["save_in_progress"] = False
        self.assertTrue(broker._wait_for_save_idle(broker.time.monotonic() + 1))

    def test_it_returns_false_when_the_save_outlasts_the_deadline(self):
        with broker._session_lock:
            broker._session["save_in_progress"] = True
        self.assertFalse(broker._wait_for_save_idle(broker.time.monotonic() + 0.3))


class RelaunchCounter(unittest.TestCase):
    """The failure count is committed before the backoff sleep, not after."""

    def test_the_count_is_visible_while_the_backoff_is_still_sleeping(self):
        # A second monitor thread waking during the sleep used to read the old
        # value, so two crash loops each counted from zero and neither gave up.
        seen = []

        class FakeProc:
            def wait(self):
                return 0

            def poll(self):
                return 0

        proc = FakeProc()
        broker._session.update(process=proc, is_managed=True, relaunch_failures=0)

        def record_sleep(_seconds):
            with broker._session_lock:
                seen.append(broker._session["relaunch_failures"])

        with unittest.mock.patch.object(broker.time, "sleep", record_sleep):
            with unittest.mock.patch.object(broker, "_launch_dolphin", lambda *a, **k: None):
                broker._monitor_process(proc, broker.time.monotonic() - 0.1)

        self.assertEqual(seen, [1])
        broker._session.update(process=None, is_managed=False, relaunch_failures=0)


if __name__ == "__main__":
    unittest.main()

"""Guards against hostile or interrupted input: zip bombs, planted symlinks,
and a memory-card swap that died halfway through.

These are the paths where a bad archive or a badly timed crash costs the user
their saves, so each one is pinned to the specific failure it prevents.
"""

import io
import os
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from support import broker, make_zip, reset_session, write


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

    def setUp(self):
        reset_session()

    def tearDown(self):
        # In tearDown, not at the end of the test body: restoring inline meant a
        # single failed assertion leaked is_managed=True into every test after it.
        reset_session()

    def test_the_count_is_visible_while_the_backoff_is_still_sleeping(self):
        # A second monitor thread waking during the sleep used to read the old
        # value, so two crash loops each counted from zero and neither gave up.
        seen = []

        class FakeProc:
            returncode = 0

            def wait(self):
                return 0

            def poll(self):
                return 0

        proc = FakeProc()
        with broker._session_lock:
            broker._session.update(process=proc, is_managed=True, relaunch_failures=0)

        def record_sleep(_seconds):
            with broker._session_lock:
                seen.append(broker._session["relaunch_failures"])

        with unittest.mock.patch.object(broker.time, "sleep", record_sleep):
            with unittest.mock.patch.object(broker, "_launch_dolphin", lambda *a, **k: None):
                broker._monitor_process(proc, broker.time.monotonic() - 0.1)

        self.assertEqual(seen, [1])


class UnreadableMembers(unittest.TestCase):
    """A member this Python cannot decode is the archive's fault, not ours."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "dolphin-emu")
        self.root.mkdir()
        self.env = unittest.mock.patch.dict(
            "os.environ", {"SAVE_DATA_ROOT": str(self.root)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def encrypted(self) -> bytes:
        """A zip claiming its member is encrypted, by flipping the header bit."""
        raw = bytearray(make_zip({"GC/a.gci": b"hello"}))
        for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = raw.index(sig)
            raw[at + offset] |= 0x01
        return bytes(raw)

    def test_an_encrypted_member_is_a_rejection_not_a_crash(self):
        # zipfile raises RuntimeError here, which used to escape the handler as
        # a 500 and leave the staged files on disk.
        result = broker._extract_save_archive(self.encrypted())
        self.assertIsInstance(result, str)
        self.assertIn("unreadable", result)

    def test_an_unsupported_compression_method_is_a_rejection(self):
        archive = make_zip({"GC/a.gci": b"x"})
        real_open = zipfile.ZipFile.open

        def unsupported(self, member, mode="r", *args, **kwargs):
            if mode == "r":
                raise NotImplementedError("That compression method is not supported")
            return real_open(self, member, mode, *args, **kwargs)

        with unittest.mock.patch.object(zipfile.ZipFile, "open", unsupported):
            result = broker._extract_save_archive(archive)
        self.assertIsInstance(result, str)
        self.assertIn("unreadable", result)

    def test_a_rejected_archive_leaves_nothing_staged(self):
        broker._extract_save_archive(self.encrypted())
        self.assertFalse((self.root / broker.RESTORE_STAGING_DIR).exists())


class RestoreStaging(unittest.TestCase):
    """Staging lives beside the save subtrees, never inside them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "dolphin-emu")
        self.root.mkdir()
        self.env = unittest.mock.patch.dict(
            "os.environ", {"SAVE_DATA_ROOT": str(self.root)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_the_staging_dir_is_outside_every_synced_subtree(self):
        for sub in broker.SAVE_SYNC_SUBTREES:
            self.assertFalse(
                broker.RESTORE_STAGING_DIR.startswith(sub.split("/")[0]),
                broker.RESTORE_STAGING_DIR,
            )

    def test_a_successful_restore_leaves_no_staging_dir(self):
        broker._extract_save_archive(make_zip({"GC/a.gci": b"one"}))
        self.assertEqual((self.root / "GC" / "a.gci").read_bytes(), b"one")
        self.assertFalse((self.root / broker.RESTORE_STAGING_DIR).exists())

    def test_leftover_staging_is_never_shipped_back_to_romm(self):
        # A crash mid-restore used to leave .gci.tmp files among the saves,
        # where the next GET /save-file zipped them up and sent them onward.
        stale = self.root / broker.RESTORE_STAGING_DIR / "000000"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"half a save")
        write(self.root / "GC" / "real.gci", b"real")
        names = {p.name for p in broker._iter_save_files(self.root)}
        self.assertEqual(names, {"real.gci"})

    def slow_extraction(self, seconds=0.05):
        """Widen the window between staging a member and committing it."""
        real = broker._extract_member

        def slow(zf, info, dest, budget):
            time.sleep(seconds)
            return real(zf, info, dest, budget)

        return unittest.mock.patch.object(broker, "_extract_member", slow)

    def test_two_real_restores_do_not_eat_each_other(self):
        # Both stage into the same directory and wipe it on entry, so the
        # second restore used to delete the first one's staged members and
        # then rename paths that were no longer there.
        from threading import Thread

        results = {}

        def restore(name, payload):
            results[name] = broker._extract_save_archive(make_zip({name: payload}))

        with self.slow_extraction():
            threads = [
                Thread(target=restore, args=(f"GC/{n}.gci", n.encode() * 8))
                for n in ("a", "b", "c")
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        for name, result in results.items():
            self.assertIsInstance(result, tuple, f"{name}: {result}")
        for n in ("a", "b", "c"):
            self.assertEqual((self.root / "GC" / f"{n}.gci").read_bytes(), n.encode() * 8)

    def test_a_get_never_zips_a_tree_mid_restore(self):
        # The archive RomM pulls has to be all of the restore or none of it,
        # never the half of it that had been renamed into place so far.
        from threading import Thread

        members = {f"GC/{n}.gci": b"restored" for n in ("a", "b", "c")}
        archives = []

        def restore():
            broker._extract_save_archive(make_zip(members))

        with self.slow_extraction():
            worker = Thread(target=restore)
            worker.start()
            time.sleep(0.02)
            archives.append(broker._build_save_archive(0))
            worker.join(timeout=10)

        with zipfile.ZipFile(io.BytesIO(archives[0])) as zf:
            self.assertEqual({i.filename for i in zf.infolist()}, set(members))

    def test_a_staging_dir_that_cannot_be_made_names_itself_in_the_error(self):
        with unittest.mock.patch.object(
            broker, "_mkdirs_owned", side_effect=OSError("read-only file system")
        ):
            error = broker._extract_save_archive(make_zip({"GC/a.gci": b"one"}))
        self.assertIn(broker.RESTORE_STAGING_DIR, error)


class Spool(unittest.TestCase):
    """Uploads are buffered on the mounted volume, not in the system temp dir."""

    def test_the_spool_dir_is_created_and_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "spool")
            with unittest.mock.patch.dict(
                "os.environ", {"BROKER_SPOOL_DIR": str(target)}
            ):
                self.assertEqual(broker._spool_dir(), str(target))
            self.assertTrue(target.is_dir())

    def test_an_unusable_spool_dir_falls_back_to_the_system_default(self):
        with unittest.mock.patch.dict(
            "os.environ", {"BROKER_SPOOL_DIR": "/proc/nope/spool"}
        ):
            self.assertIsNone(broker._spool_dir())

    def test_stale_uploads_are_cleared_at_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.dict("os.environ", {"BROKER_SPOOL_DIR": tmp}):
                stale = Path(tmp, ".broker-upload-abc123")
                stale.write_bytes(b"half an archive")
                keep = Path(tmp, "unrelated")
                keep.write_bytes(b"x")
                broker._clear_spool()
                self.assertFalse(stale.exists())
                self.assertTrue(keep.exists())

    def test_the_fallback_location_is_cleared_too(self):
        # An unusable /config is exactly when clearing matters: every upload
        # has been landing in the system temp dir, which nothing else prunes.
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp, ".broker-upload-def456")
            stale.write_bytes(b"half an archive")
            with unittest.mock.patch.dict(
                "os.environ", {"BROKER_SPOOL_DIR": "/proc/nope/spool"}
            ):
                with unittest.mock.patch.object(
                    broker.tempfile, "gettempdir", lambda: tmp
                ):
                    broker._clear_spool()
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()

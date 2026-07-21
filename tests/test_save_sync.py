"""In-game save sync: GET builds an archive, PUT restores one.

PUT extracts an attacker-influenced zip while running as root, so the member
checks get the most attention here.
"""

import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from support import broker, make_zip, past, write, zip_names


class SaveSyncBase(unittest.TestCase):
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


class BuildSaveArchive(SaveSyncBase):
    def test_returns_none_when_nothing_changed_since_baseline(self):
        write(self.root / "GC" / "USA" / "Card A" / "old.gci", mtime=past())
        self.assertIsNone(broker._build_save_archive(time.time()))

    def test_includes_only_files_modified_since_baseline(self):
        baseline = time.time()
        write(self.root / "GC" / "stale.gci", mtime=past())
        write(self.root / "GC" / "fresh.gci", mtime=baseline + 1)
        archive = broker._build_save_archive(baseline)
        self.assertEqual(zip_names(archive), {"GC/fresh.gci"})

    def test_covers_every_declared_subtree(self):
        baseline = time.time()
        for sub in broker.SAVE_SYNC_SUBTREES:
            write(self.root / sub / "f.bin", mtime=baseline + 1)
        archive = broker._build_save_archive(baseline)
        self.assertEqual(
            zip_names(archive),
            {f"{sub}/f.bin" for sub in broker.SAVE_SYNC_SUBTREES},
        )

    def test_ignores_files_outside_the_declared_subtrees(self):
        baseline = time.time()
        write(self.root / "Config" / "Dolphin.ini", mtime=baseline + 1)
        write(self.root / "StateSaves" / "GALE01.s08", mtime=baseline + 1)
        self.assertIsNone(broker._build_save_archive(baseline))

    def test_member_paths_are_relative_to_the_data_root(self):
        baseline = time.time()
        write(self.root / "Wii" / "title" / "00010000" / "save.bin", mtime=baseline + 1)
        self.assertEqual(
            zip_names(broker._build_save_archive(baseline)),
            {"Wii/title/00010000/save.bin"},
        )

    def test_identical_content_produces_identical_bytes(self):
        baseline = time.time()
        for name in ("b.gci", "a.gci", "c.gci"):
            write(self.root / "GC" / name, b"data", mtime=baseline + 1)
        first = broker._build_save_archive(baseline)
        second = broker._build_save_archive(baseline)
        self.assertEqual(first, second)

    def test_skips_symlinked_files(self):
        baseline = time.time()
        secret = write(Path(self.tmp.name, "secret.key"), b"s", mtime=baseline + 1)
        (self.root / "GC").mkdir()
        os.symlink(secret, self.root / "GC" / "link.gci")
        self.assertIsNone(broker._build_save_archive(baseline))

    def test_refuses_to_build_an_oversized_archive(self):
        baseline = time.time()
        write(self.root / "GC" / "big.gci", b"x", mtime=baseline + 1)
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 0):
            self.assertIsNone(broker._build_save_archive(baseline))

    def test_returns_none_when_no_data_dir_exists(self):
        with unittest.mock.patch.dict(
            "os.environ", {"SAVE_DATA_ROOT": str(Path(self.tmp.name, "missing"))}
        ):
            self.assertIsNone(broker._build_save_archive(0))


class ExtractSaveArchive(SaveSyncBase):
    def test_restores_members_into_the_data_root(self):
        result = broker._extract_save_archive(make_zip({"GC/save.gci": b"hello"}))
        self.assertEqual(result, (1, 0))
        self.assertEqual((self.root / "GC" / "save.gci").read_bytes(), b"hello")

    def test_creates_missing_parent_directories(self):
        broker._extract_save_archive(
            make_zip({"Wii/title/00010000/00000001/data/save.bin": b"w"})
        )
        self.assertTrue(
            (self.root / "Wii/title/00010000/00000001/data/save.bin").exists()
        )

    def test_rejects_a_body_that_is_not_a_zip(self):
        self.assertIsInstance(broker._extract_save_archive(b"not a zip"), str)

    def test_rejects_absolute_member_paths(self):
        result = broker._extract_save_archive(make_zip({"/etc/passwd": b"pwned"}))
        self.assertIsInstance(result, str)

    def test_rejects_dotdot_member_paths(self):
        result = broker._extract_save_archive(
            make_zip({"GC/../../.config/dolphin-emu/Dolphin.ini": b"pwned"})
        )
        self.assertIsInstance(result, str)

    def test_rejects_members_outside_the_declared_subtrees(self):
        for name in ("StateSaves/GALE01.s08", "Config/Dolphin.ini", "keys.bin"):
            with self.subTest(member=name):
                self.assertIsInstance(
                    broker._extract_save_archive(make_zip({name: b"x"})), str
                )

    def test_rejects_a_subtree_name_used_as_a_prefix(self):
        # "GC" must not authorise "GCevil/".
        self.assertIsInstance(
            broker._extract_save_archive(make_zip({"GCevil/x.bin": b"x"})), str
        )

    def test_rejects_an_archive_that_expands_past_the_size_limit(self):
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 1):
            self.assertIsInstance(
                broker._extract_save_archive(make_zip({"GC/a.gci": b"aaaa"})), str
            )

    def test_writes_nothing_when_any_member_is_rejected(self):
        content = make_zip({"GC/good.gci": b"ok", "/etc/passwd": b"pwned"})
        self.assertIsInstance(broker._extract_save_archive(content), str)
        self.assertFalse((self.root / "GC" / "good.gci").exists())

    def test_skips_local_files_newer_than_the_archive_member(self):
        target = write(self.root / "GC" / "save.gci", b"newer", mtime=time.time())
        result = broker._extract_save_archive(
            make_zip({"GC/save.gci": b"older"}, date_time=(2020, 1, 1, 0, 0, 0))
        )
        self.assertEqual(result, (0, 1))
        self.assertEqual(target.read_bytes(), b"newer")

    def test_overwrites_local_files_older_than_the_archive_member(self):
        ancient = time.mktime((2010, 1, 1, 0, 0, 0, 0, 0, -1))
        target = write(self.root / "GC" / "save.gci", b"older", mtime=ancient)
        result = broker._extract_save_archive(make_zip({"GC/save.gci": b"newer"}))
        self.assertEqual(result, (1, 0))
        self.assertEqual(target.read_bytes(), b"newer")

    def test_restored_mtime_matches_the_archive_member(self):
        broker._extract_save_archive(
            make_zip({"GC/save.gci": b"x"}, date_time=(2021, 6, 1, 12, 0, 0))
        )
        stat = (self.root / "GC" / "save.gci").stat()
        expected = time.mktime((2021, 6, 1, 12, 0, 0, 0, 0, -1))
        self.assertAlmostEqual(stat.st_mtime, expected, delta=2)

    def test_leaves_no_temp_file_behind(self):
        broker._extract_save_archive(make_zip({"GC/save.gci": b"x"}))
        leftovers = [p.name for p in (self.root / "GC").iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_round_trips_a_built_archive(self):
        baseline = time.time()
        write(self.root / "GC" / "a.gci", b"one", mtime=baseline + 1)
        write(self.root / "Wii" / "title" / "b.bin", b"two", mtime=baseline + 1)
        archive = broker._build_save_archive(baseline)
        for p in (self.root / "GC" / "a.gci", self.root / "Wii" / "title" / "b.bin"):
            p.unlink()
        self.assertEqual(broker._extract_save_archive(archive), (2, 0))
        self.assertEqual((self.root / "GC" / "a.gci").read_bytes(), b"one")


if __name__ == "__main__":
    unittest.main()

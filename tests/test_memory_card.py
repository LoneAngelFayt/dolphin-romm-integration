"""Whole-card GameCube sync.

PUT wipes the live card, so the failure that matters is a hydrate that leaves
the user with no card at all.
"""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support import broker, make_zip, write, zip_names


class MemoryCardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.card = Path(self.tmp.name, "Card A")
        self.env = unittest.mock.patch.dict(
            "os.environ", {"GCI_CARD_DIR": str(self.card)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class BuildMemoryCardArchive(MemoryCardBase):
    def test_returns_none_when_no_card_exists(self):
        self.assertIsNone(broker._build_memory_card_archive())

    def test_returns_an_error_when_the_card_path_is_a_file(self):
        write(self.card, b"not a folder")
        self.assertIsInstance(broker._build_memory_card_archive(), str)

    def test_zips_an_empty_card_directory(self):
        self.card.mkdir()
        self.assertEqual(zip_names(broker._build_memory_card_archive()), set())

    def test_member_paths_are_relative_to_the_card_root(self):
        write(self.card / "USA" / "GALE01.gci", b"melee")
        self.assertEqual(
            zip_names(broker._build_memory_card_archive()), {"USA/GALE01.gci"}
        )

    def test_refuses_to_build_an_oversized_card(self):
        write(self.card / "big.gci", b"xxxx")
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 1):
            self.assertIsInstance(broker._build_memory_card_archive(), str)


class ReplaceMemoryCard(MemoryCardBase):
    def test_lays_down_a_card_where_none_existed(self):
        self.assertEqual(
            broker._replace_memory_card(make_zip({"GALE01.gci": b"melee"})), (1,)
        )
        self.assertEqual((self.card / "GALE01.gci").read_bytes(), b"melee")

    def test_replaces_the_card_wholesale(self):
        write(self.card / "stale.gci", b"old")
        broker._replace_memory_card(make_zip({"fresh.gci": b"new"}))
        self.assertFalse((self.card / "stale.gci").exists())
        self.assertTrue((self.card / "fresh.gci").exists())

    def test_ignores_local_mtimes_when_hydrating(self):
        # Unlike /save-file, the card is not merged: the pulled image wins even
        # when the local file is newer.
        write(self.card / "GALE01.gci", b"local")
        broker._replace_memory_card(
            make_zip({"GALE01.gci": b"pulled"}, date_time=(1990, 1, 1, 0, 0, 0))
        )
        self.assertEqual((self.card / "GALE01.gci").read_bytes(), b"pulled")

    def test_rejects_a_body_that_is_not_a_zip(self):
        self.assertIsInstance(broker._replace_memory_card(b"garbage"), str)

    def test_rejects_absolute_member_paths(self):
        self.assertIsInstance(
            broker._replace_memory_card(make_zip({"/etc/passwd": b"x"})), str
        )

    def test_rejects_dotdot_member_paths(self):
        self.assertIsInstance(
            broker._replace_memory_card(make_zip({"../../Dolphin.ini": b"x"})), str
        )

    def test_rejects_an_archive_that_expands_past_the_size_limit(self):
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 1):
            self.assertIsInstance(
                broker._replace_memory_card(make_zip({"a.gci": b"aaaa"})), str
            )

    def test_a_rejected_archive_leaves_the_existing_card_untouched(self):
        write(self.card / "GALE01.gci", b"precious")
        self.assertIsInstance(broker._replace_memory_card(b"garbage"), str)
        self.assertEqual((self.card / "GALE01.gci").read_bytes(), b"precious")

    def test_the_card_survives_a_failure_partway_through_extraction(self):
        write(self.card / "GALE01.gci", b"precious")
        archive = make_zip({"a.gci": b"1", "b.gci": b"2"})
        real_write_bytes = Path.write_bytes
        calls = {"n": 0}

        def flaky(self, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_write_bytes(self, data)

        with unittest.mock.patch.object(Path, "write_bytes", flaky):
            self.assertIsInstance(broker._replace_memory_card(archive), str)
        self.assertEqual((self.card / "GALE01.gci").read_bytes(), b"precious")

    def test_leaves_no_staging_directory_behind(self):
        broker._replace_memory_card(make_zip({"a.gci": b"1"}))
        leftovers = [p.name for p in self.card.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_round_trips_a_built_card(self):
        write(self.card / "USA" / "GALE01.gci", b"melee")
        archive = broker._build_memory_card_archive()
        broker._replace_memory_card(make_zip({"JAP/GALJ01.gci": b"other"}))
        self.assertEqual(broker._replace_memory_card(archive), (1,))
        self.assertEqual((self.card / "USA" / "GALE01.gci").read_bytes(), b"melee")
        self.assertFalse((self.card / "JAP").exists())


if __name__ == "__main__":
    unittest.main()

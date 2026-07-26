"""ROM path validation: the boundary between RomM's input and the exec line."""

import os
import tempfile
import unittest
from pathlib import Path

from support import broker


class RomPathValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "library")
        self.root.mkdir()
        self.outside = Path(self.tmp.name, "secrets")
        self.outside.mkdir()
        self._saved_root = broker.ROM_ROOT
        broker.ROM_ROOT = self.root.resolve()

    def tearDown(self):
        broker.ROM_ROOT = self._saved_root
        self.tmp.cleanup()

    def test_accepts_path_inside_root(self):
        rom = self.root / "gc" / "game.iso"
        rom.parent.mkdir()
        rom.touch()
        self.assertEqual(broker._validate_rom_path(str(rom)), rom.resolve())

    def test_rejects_dotdot_traversal(self):
        self.assertIsNone(
            broker._validate_rom_path(str(self.root / ".." / "secrets" / "key"))
        )

    def test_rejects_absolute_path_outside_root(self):
        self.assertIsNone(broker._validate_rom_path("/etc/passwd"))

    def test_rejects_symlink_escaping_root(self):
        link = self.root / "escape.iso"
        target = self.outside / "key"
        target.touch()
        os.symlink(target, link)
        self.assertIsNone(broker._validate_rom_path(str(link)))

    def test_rejects_sibling_directory_with_shared_prefix(self):
        sibling = Path(str(self.root) + "-other")
        sibling.mkdir()
        self.assertIsNone(broker._validate_rom_path(str(sibling / "game.iso")))

    def test_accepts_path_that_does_not_exist_yet(self):
        # Existence is checked separately by the handler, which returns 422.
        missing = self.root / "nope.iso"
        self.assertEqual(broker._validate_rom_path(str(missing)), missing)

    def test_rejects_embedded_nul(self):
        self.assertIsNone(broker._validate_rom_path(str(self.root / "a\0b.iso")))


class RomFileResolution(unittest.TestCase):
    """RomM addresses a folder-organized game by its folder, so /launch
    receives a directory Dolphin cannot boot.

    `Rom.full_path` is `fs_path/fs_name`, and for a multi-file ROM `fs_name` is
    the directory rather than the disc image inside it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "library").resolve()
        self.root.mkdir()
        self._saved_root = broker.ROM_ROOT
        broker.ROM_ROOT = self.root

    def tearDown(self):
        broker.ROM_ROOT = self._saved_root
        self.tmp.cleanup()

    def _rom(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"disc")
        return p

    def test_plain_file_passes_through(self):
        iso = self._rom("gc/game.iso")
        self.assertEqual(broker._resolve_rom_file(iso), iso)

    def test_game_folder_resolves_to_the_disc_image_inside(self):
        iso = self._rom("gc/Metroid Prime/Metroid Prime.iso")
        self.assertEqual(
            broker._resolve_rom_file(self.root / "gc" / "Metroid Prime"), iso
        )

    def test_folder_without_a_bootable_file_returns_none(self):
        self._rom("gc/Metroid Prime/cover.png")
        self._rom("gc/Metroid Prime/notes.txt")
        self.assertIsNone(
            broker._resolve_rom_file(self.root / "gc" / "Metroid Prime")
        )

    def test_multi_disc_folder_picks_disc_one(self):
        disc1 = self._rom("gc/RE4/RE4 (Disc 1).iso")
        self._rom("gc/RE4/RE4 (Disc 2).iso")
        self.assertEqual(broker._resolve_rom_file(self.root / "gc" / "RE4"), disc1)

    def test_better_format_wins_over_a_plain_iso(self):
        rvz = self._rom("wii/Game/Game.rvz")
        self._rom("wii/Game/Game.iso")
        self.assertEqual(broker._resolve_rom_file(self.root / "wii" / "Game"), rvz)

    def test_disc_image_wins_over_a_homebrew_executable(self):
        iso = self._rom("gc/Game/Game.iso")
        self._rom("gc/Game/boot.dol")
        self.assertEqual(broker._resolve_rom_file(self.root / "gc" / "Game"), iso)

    def test_playlist_is_not_chosen_over_its_discs(self):
        disc1 = self._rom("gc/RE4/RE4 (Disc 1).iso")
        self._rom("gc/RE4/RE4.m3u")
        self.assertEqual(broker._resolve_rom_file(self.root / "gc" / "RE4"), disc1)

    def test_finds_image_in_a_per_disc_subfolder(self):
        disc1 = self._rom("gc/RE4/Disc 1/RE4.iso")
        self._rom("gc/RE4/Disc 2/RE4.iso")
        self.assertEqual(broker._resolve_rom_file(self.root / "gc" / "RE4"), disc1)

    def test_top_level_image_wins_over_a_nested_one(self):
        top = self._rom("gc/Game/Game.iso")
        self._rom("gc/Game/extras/bonus.iso")
        self.assertEqual(broker._resolve_rom_file(self.root / "gc" / "Game"), top)

    def test_does_not_descend_past_the_second_level(self):
        self._rom("gc/Game/a/b/deep.iso")
        self.assertIsNone(broker._resolve_rom_file(self.root / "gc" / "Game"))

    def test_hidden_files_are_ignored(self):
        self._rom("gc/Game/._Game.iso")
        self.assertIsNone(broker._resolve_rom_file(self.root / "gc" / "Game"))

    def test_symlink_escaping_rom_root_is_refused(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        target = outside / "escape.iso"
        target.write_bytes(b"disc")
        folder = self.root / "gc" / "Game"
        folder.mkdir(parents=True)
        os.symlink(target, folder / "link.iso")
        self.assertIsNone(broker._resolve_rom_file(folder))

    def test_missing_path_returns_none(self):
        self.assertIsNone(broker._resolve_rom_file(self.root / "gc" / "nope"))


if __name__ == "__main__":
    unittest.main()

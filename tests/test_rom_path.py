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


if __name__ == "__main__":
    unittest.main()

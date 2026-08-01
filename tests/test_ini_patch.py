"""Dolphin.ini patching.

A key written to the wrong section, or a duplicated section header, silently
disables hotkeys and controller input. The failure looks like a broken
emulator, not a broken config, so it is worth pinning hard.
"""

import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support import broker


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Parse into {section: {key: value}}, last value per key wins."""
    out: dict[str, dict[str, str]] = {}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            out.setdefault(section, {})
        elif "=" in s and section is not None:
            k, _, v = s.partition("=")
            out[section][k.strip()] = v.strip()
    return out


def section_headers(text: str) -> list[str]:
    return re.findall(r"^\[(.+)\]$", text, re.MULTILINE)


class IniPatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmp.name, "data")
        self.data_root.mkdir()
        self.ini = Path(self.tmp.name, "config", "dolphin-emu", "Dolphin.ini")
        self._saved_ini = broker.INI_PATH
        broker.INI_PATH = self.ini
        self.env = unittest.mock.patch.dict(
            "os.environ", {"SAVE_DATA_ROOT": str(self.data_root)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        broker.INI_PATH = self._saved_ini
        self.tmp.cleanup()

    def read(self) -> str:
        return self.ini.read_text()

    # ── creation ──────────────────────────────────────────────────────────

    def test_creates_ini_when_absent(self):
        broker._patch_ini()
        self.assertTrue(self.ini.exists())

    def test_created_ini_matches_the_patch_target(self):
        """The from-scratch write and the patch target must not drift apart."""
        broker._patch_ini(fullscreen=True)
        created = parse_ini(self.read())
        broker._patch_ini(fullscreen=True)
        self.assertEqual(created, parse_ini(self.read()))

    # ── section placement ─────────────────────────────────────────────────

    def test_background_input_lands_in_input_section(self):
        # Dolphin reads MAIN_INPUT_BACKGROUND_INPUT from [Input]. It lived in
        # [General] until 2026-07-20, where Dolphin never saw it.
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["Input"]["BackgroundInput"], "True")
        self.assertNotIn("BackgroundInput", cfg.get("General", {}))

    def test_hotkeys_require_focus_lands_in_general_section(self):
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["General"]["HotkeysRequireFocus"], "False")

    def test_writes_the_key_to_the_right_section_when_it_exists_in_a_wrong_one(self):
        # The stale [General] copy is left alone; Dolphin does not read it.
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[General]\nBackgroundInput = False\n")
        broker._patch_ini()
        self.assertEqual(parse_ini(self.read())["Input"]["BackgroundInput"], "True")

    # ── patching an existing file ─────────────────────────────────────────

    def test_overwrites_existing_wrong_values(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text(
            "[Core]\nCPUThread = True\n"
            "[Interface]\nConfirmStop = True\n"
        )
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["Core"]["CPUThread"], "False")
        self.assertEqual(cfg["Interface"]["ConfirmStop"], "False")

    # ── the backend is seeded once, then owned by the player ──────────────

    def test_a_fresh_config_is_seeded_with_opengl(self):
        # OpenGL is the backend known safe across GPUs, so a fresh config boots
        # on it rather than falling through to Dolphin's own default.
        broker._patch_ini()
        self.assertEqual(parse_ini(self.read())["Core"]["GFXBackend"], "OpenGL")

    def test_a_backend_the_player_picked_is_not_reset(self):
        # The whole point of the change: a value chosen in Dolphin's Graphics
        # settings must survive the next launch's ini patch.
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Core]\nGFXBackend = Vulkan\nCPUThread = True\n")
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["Core"]["GFXBackend"], "Vulkan")
        self.assertEqual(cfg["Core"]["CPUThread"], "False")  # still forced

    def test_a_pre_existing_config_without_a_backend_is_seeded(self):
        # An older Dolphin.ini with no GFXBackend must not fall through to
        # Dolphin's own default, which may be Vulkan.
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Core]\nCPUThread = True\n")
        broker._patch_ini()
        self.assertEqual(parse_ini(self.read())["Core"]["GFXBackend"], "OpenGL")

    def test_the_backend_is_never_duplicated_across_launches(self):
        broker._patch_ini()
        for _ in range(3):
            broker._patch_ini()
        core_backends = [
            ln for ln in self.read().splitlines() if ln.strip().startswith("GFXBackend")
        ]
        self.assertEqual(len(core_backends), 1, core_backends)

    def test_preserves_unrelated_keys(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Core]\nGFXBackend = Vulkan\nSkipIPL = True\n")
        broker._patch_ini()
        self.assertEqual(parse_ini(self.read())["Core"]["SkipIPL"], "True")

    def test_handles_keys_written_without_spaces(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Interface]\nConfirmStop=True\n")
        broker._patch_ini()
        self.assertEqual(parse_ini(self.read())["Interface"]["ConfirmStop"], "False")

    def test_inserts_missing_key_under_the_existing_header(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Interface]\nThemeName = Clean\n")
        broker._patch_ini()
        self.assertEqual(section_headers(self.read()).count("Interface"), 1)
        self.assertEqual(parse_ini(self.read())["Interface"]["ConfirmStop"], "False")

    def test_never_duplicates_a_section_header(self):
        broker._patch_ini()
        for _ in range(3):
            broker._patch_ini()
        headers = section_headers(self.read())
        self.assertEqual(len(headers), len(set(headers)), headers)

    def test_is_idempotent(self):
        broker._patch_ini(fullscreen=True)
        once = self.read()
        broker._patch_ini(fullscreen=True)
        self.assertEqual(once, self.read())

    def test_ends_with_a_newline(self):
        broker._patch_ini()
        self.assertTrue(self.read().endswith("\n"))
        self.assertFalse(self.read().endswith("\n\n\n"))

    def test_leaves_no_temp_file_behind(self):
        broker._patch_ini()
        broker._patch_ini()
        leftovers = [p.name for p in self.ini.parent.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    # ── fullscreen and memory card ────────────────────────────────────────

    def test_fullscreen_flag_drives_the_display_section(self):
        broker._patch_ini(fullscreen=True)
        self.assertEqual(parse_ini(self.read())["Display"]["Fullscreen"], "True")
        broker._patch_ini(fullscreen=False)
        self.assertEqual(parse_ini(self.read())["Display"]["Fullscreen"], "False")

    def test_slot_a_is_pinned_to_the_gci_folder_override(self):
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["Core"]["SlotA"], str(broker.EXI_MEMORY_CARD_FOLDER))
        self.assertEqual(
            cfg["Core"]["GCIFolderAPathOverride"], str(broker._gci_card_path())
        )

    def test_analytics_stay_disabled(self):
        broker._patch_ini()
        cfg = parse_ini(self.read())
        self.assertEqual(cfg["Analytics"]["Enabled"], "False")
        self.assertEqual(cfg["Analytics"]["PermissionAsked"], "True")

    # ── failure handling ──────────────────────────────────────────────────

    def test_unreadable_ini_leaves_the_original_intact(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Core]\nGFXBackend = Vulkan\n")
        with unittest.mock.patch.object(
            Path, "read_text", side_effect=OSError("boom")
        ):
            broker._patch_ini()
        self.assertIn("Vulkan", self.read())


if __name__ == "__main__":
    unittest.main()

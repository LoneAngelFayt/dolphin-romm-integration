"""Container wiring and cross-file drift.

None of this is exercised by the unit tests above: it is the layer where the
mod is a set of files copied into someone else's image. A broken s6 dependency
or a sudoers rule that no longer covers what the broker shells out to fails at
runtime, in a container, with no stack trace.
"""

import re
import unittest
from pathlib import Path

from support import BROKER_PATH, REPO_ROOT

S6 = REPO_ROOT / "root/etc/s6-overlay/s6-rc.d"
INIT_SH = S6 / "init-dolphin-config/init.sh"
BROKER_SRC = BROKER_PATH.read_text()
INIT_SRC = INIT_SH.read_text()


def parse_ini_text(text: str) -> dict[str, dict[str, str]]:
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


class DockerMod(unittest.TestCase):
    def test_the_image_is_just_the_root_overlay(self):
        text = (REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("FROM scratch", text)
        self.assertIn("COPY root/", text)

    def test_compiled_python_is_not_shipped(self):
        ignored = (REPO_ROOT / ".gitignore").read_text()
        self.assertIn("__pycache__", ignored)
        self.assertIn("*.pyc", ignored)


class S6Services(unittest.TestCase):
    def test_the_broker_is_a_longrun_and_the_config_step_is_a_oneshot(self):
        self.assertEqual((S6 / "svc-broker/type").read_text().strip(), "longrun")
        self.assertEqual(
            (S6 / "init-dolphin-config/type").read_text().strip(), "oneshot"
        )

    def test_both_services_are_enabled_in_the_user_bundle(self):
        enabled = {p.name for p in (S6 / "user/contents.d").iterdir()}
        self.assertEqual(enabled, {"svc-broker", "init-dolphin-config"})

    def test_the_broker_waits_for_the_config_step(self):
        deps = {p.name for p in (S6 / "svc-broker/dependencies.d").iterdir()}
        self.assertIn("init-dolphin-config", deps)

    def test_the_oneshot_up_file_points_at_the_script_that_exists(self):
        up = (S6 / "init-dolphin-config/up").read_text().strip()
        self.assertTrue(Path(REPO_ROOT / "root" / up.lstrip("/")).is_file(), up)

    def test_the_run_script_execs_the_broker_that_ships(self):
        run = (S6 / "svc-broker/run").read_text()
        self.assertIn("/root/broker.py", run)
        self.assertTrue(BROKER_PATH.is_file())

    def test_the_broker_runs_unbuffered_so_logs_reach_s6(self):
        self.assertIn("python3 -u", (S6 / "svc-broker/run").read_text())


class Sudoers(unittest.TestCase):
    RULE = (REPO_ROOT / "root/etc/sudoers.d/broker").read_text()

    def test_the_rule_targets_the_abc_account(self):
        self.assertRegex(self.RULE, r"^root ALL=\(abc\) NOPASSWD:", )

    def test_every_binary_the_broker_sudos_to_is_covered(self):
        invoked = set(re.findall(r'"sudo", "-u", "abc", "(\w+)"', BROKER_SRC))
        self.assertTrue(invoked, "no sudo invocations found, did the pattern change?")
        for binary in invoked:
            with self.subTest(binary=binary):
                self.assertIn(f"/{binary}", self.RULE)

    def test_the_rule_is_made_readable_by_sudo_at_init(self):
        # sudo silently ignores a sudoers.d file that is not mode 0440.
        self.assertIn("chmod 0440 /etc/sudoers.d/broker", INIT_SRC)


class SeededConfigMatchesTheBroker(unittest.TestCase):
    """init.sh pre-seeds Dolphin.ini and broker.py patches it.

    Two copies of the same defaults drift: BackgroundInput sat in the wrong
    section in both until 2026-07-20, and only one of them was fixed.
    """

    def setUp(self):
        match = re.search(r"cat > \"\$DOLPHIN_INI\" <<'EOF'\n(.*?)\nEOF", INIT_SRC, re.S)
        if match is None:
            self.skipTest("init.sh no longer seeds Dolphin.ini, broker.py is the only copy")
        self.seeded = parse_ini_text(match.group(1))

    def broker_target(self) -> dict[str, dict[str, str]]:
        """The section/key pairs broker.py enforces, read from its own source."""
        body = re.search(r"\n    target = \{\n(.*?)\n    \}\n", BROKER_SRC, re.S)
        self.assertIsNotNone(body, "broker.py target dict not found")
        out: dict[str, dict[str, str]] = {}
        for section_name, keys in re.findall(
            r'"(\w+)": \{([^}]*)\}', body.group(1), re.S
        ):
            out[section_name] = {
                k: v for k, v in re.findall(r'"(\w+)": ["\w.]*?"?(\w+)"', keys)
            }
        return {s: set(k) for s, k in out.items()}

    def test_no_seeded_key_sits_in_a_section_the_broker_disagrees_with(self):
        target = self.broker_target()
        placement = {k: s for s, keys in target.items() for k in keys}
        for section, keys in self.seeded.items():
            for key in keys:
                with self.subTest(key=key):
                    expected = placement.get(key)
                    if expected is None:
                        continue
                    self.assertEqual(
                        section,
                        expected,
                        f"init.sh puts {key} in [{section}], broker.py in [{expected}]",
                    )

    def test_seeded_values_match_what_the_broker_enforces(self):
        # A seeded value the broker immediately overwrites is dead weight and a
        # misleading record of what the mod wants.
        for section, keys in self.seeded.items():
            for key, value in keys.items():
                with self.subTest(key=key):
                    match = re.search(
                        rf'"{key}":\s*("(\w+)"|str\(|f?"?\{{)', BROKER_SRC
                    )
                    if match is None or match.group(2) is None:
                        continue
                    self.assertEqual(value, match.group(2))


class BrokerConstants(unittest.TestCase):
    def test_the_save_slot_is_within_the_hotkey_range(self):
        from support import broker

        self.assertLessEqual(broker.MAX_SLOT, 8, "Dolphin only maps F1-F8")
        self.assertTrue(1 <= broker.SAVE_SLOT <= broker.MAX_SLOT)

    def test_the_state_and_save_roots_share_a_parent(self):
        """Screenshots, cards and states must resolve under the same data dir.

        _sstate_dir and _save_data_root probe independently; if their candidate
        lists ever stop lining up, a state can land in one tree and its
        thumbnail in the other.
        """
        from support import broker

        state_parents = [c.parent for c in broker._SSTATE_DIR_CANDIDATES]
        self.assertEqual(list(broker._SAVE_DATA_ROOTS), state_parents)

    def test_the_declared_save_subtrees_include_the_pinned_card(self):
        from support import broker

        self.assertIn("romm/Card A", broker.SAVE_SYNC_SUBTREES)
        self.assertTrue(str(broker._gci_card_path()).endswith("romm/Card A"))


if __name__ == "__main__":
    unittest.main()

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
    def test_the_broker_is_a_longrun_and_the_init_steps_are_oneshots(self):
        self.assertEqual((S6 / "svc-broker/type").read_text().strip(), "longrun")
        for unit in ("init-dolphin-config", "init-dolphin-deps"):
            with self.subTest(unit=unit):
                self.assertEqual((S6 / unit / "type").read_text().strip(), "oneshot")

    def test_every_service_is_enabled_in_the_user_bundle(self):
        enabled = {p.name for p in (S6 / "user/contents.d").iterdir()}
        self.assertEqual(
            enabled, {"svc-broker", "init-dolphin-config", "init-dolphin-deps"}
        )

    def test_the_broker_waits_for_both_init_steps(self):
        deps = {p.name for p in (S6 / "svc-broker/dependencies.d").iterdir()}
        self.assertIn("init-dolphin-config", deps)
        self.assertIn("init-dolphin-deps", deps)

    def test_the_service_stack_waits_for_the_config_step(self):
        """Without this edge every patch in init-dolphin-config is dead.

        nginx, xorg and selkies read their config once at start. s6 otherwise
        runs them in parallel with the oneshot, they win by seconds, and the
        stream gate is simply absent from the nginx that is actually serving.
        """
        self.assertTrue(
            (S6 / "init-services/dependencies.d/init-dolphin-config").exists()
        )

    def test_the_slow_work_is_not_in_the_step_the_stack_waits_on(self):
        """apt-get belongs in init-dolphin-deps, which only the broker waits for.

        In init-dolphin-config it would park the whole desktop behind a package
        download on every cold start.
        """
        self.assertNotIn("apt-get", INIT_SRC)
        self.assertIn("apt-get", (S6 / "init-dolphin-deps/init.sh").read_text())

    def test_each_oneshot_up_file_points_at_the_script_that_exists(self):
        for unit in ("init-dolphin-config", "init-dolphin-deps"):
            with self.subTest(unit=unit):
                up = (S6 / unit / "up").read_text().strip()
                self.assertTrue(Path(REPO_ROOT / "root" / up.lstrip("/")).is_file(), up)

    def test_the_run_script_execs_the_broker_that_ships(self):
        run = (S6 / "svc-broker/run").read_text()
        self.assertIn("/root/broker.py", run)
        self.assertTrue(BROKER_PATH.is_file())

    def test_the_broker_runs_unbuffered_so_logs_reach_s6(self):
        self.assertIn("python3 -u", (S6 / "svc-broker/run").read_text())


class NginxStreamGate(unittest.TestCase):
    """The gate is split across init.sh and broker.py and must agree.

    nginx names the internal subrequest and the broker answers it; a rename on
    either side leaves a gate that authorises nothing, and nginx will not
    complain because auth_request off is a perfectly valid config.
    """

    def test_the_gate_calls_the_route_the_broker_serves(self):
        self.assertIn("/verify", INIT_SRC)
        self.assertIn('"/verify"', BROKER_SRC)

    def test_the_gate_targets_the_port_the_broker_listens_on(self):
        self.assertIn('bport="${BROKER_PORT:-8000}"', INIT_SRC)
        self.assertIn('BROKER_PORT", "8000"', BROKER_SRC)

    def test_the_set_cookie_header_is_carried_back_out(self):
        # auth_request drops the subrequest's response headers unless they are
        # captured; without this the stream_sid cookie never reaches the browser
        # and every asset after the first re-presents the query token.
        self.assertIn(
            "auth_request_set $stream_set_cookie $upstream_http_set_cookie", INIT_SRC
        )
        self.assertIn("add_header Set-Cookie $stream_set_cookie", INIT_SRC)

    def test_the_error_page_is_exempt_from_the_gate(self):
        # /50x.html is the error_page target. Gated, a broker outage makes each
        # request bounce between the 500 and its own gated error page until
        # nginx trips its internal-redirect limit.
        self.assertIn("/50x\\.html", INIT_SRC)
        self.assertIn('print "    auth_request off;"', INIT_SRC)

    def test_the_gate_is_anchored_on_every_server_block(self):
        # The base image ships two identical vhosts (3000 plain, 3001 TLS).
        # Anchoring on 'server {' rather than a port gates both, and the count
        # check in init.sh warns when fewer than two matched.
        self.assertIn("server[[:space:]]*\\{", INIT_SRC)
        self.assertIn("only $gated vhost gated", INIT_SRC)

    def test_verify_is_reachable_without_the_shared_secret(self):
        # nginx cannot forward BROKER_SECRET on a subrequest, so /verify has to
        # be routed before _check_secret runs. The stream token is its credential.
        get_body = re.search(r"def do_GET\(self\).*?(?=\n    def )", BROKER_SRC, re.S)
        self.assertIsNotNone(get_body, "do_GET not found, did it get renamed?")
        verify = get_body.group(0).index("/verify")
        secret = get_body.group(0).index("_check_secret")
        self.assertLess(verify, secret, "/verify is routed after the secret check")


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

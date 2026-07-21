"""The HTTP contract RomM's backend depends on.

Runs a real ThreadingHTTPServer on a loopback port with every emulator-touching
helper stubbed, so this covers routing, auth, validation and status codes.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from support import broker, make_zip, reset_session, write, zip_names


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)


class ApiTestCase(unittest.TestCase):
    """Base: live server, stubbed emulator, isolated dirs."""

    SECRET = "test-secret"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmp.name, "dolphin-emu")
        self.state_dir = self.data_root / "StateSaves"
        self.rom_root = Path(self.tmp.name, "library")
        for d in (self.data_root, self.state_dir, self.rom_root):
            d.mkdir(parents=True)

        self.env = unittest.mock.patch.dict(
            "os.environ",
            {
                "SAVE_DATA_ROOT": str(self.data_root),
                "SSTATE_DIR": str(self.state_dir),
                "GCI_CARD_DIR": str(self.data_root / "romm" / "Card A"),
            },
        )
        self.env.start()

        self.patches = [
            unittest.mock.patch.object(broker, "SECRET", self.SECRET),
            unittest.mock.patch.object(broker, "ROM_ROOT", self.rom_root.resolve()),
            unittest.mock.patch.object(broker, "_launch_dolphin", self._fake_launch),
            unittest.mock.patch.object(broker, "_xdotool_save_state", lambda slot: True),
            unittest.mock.patch.object(broker, "_xdotool_load_state", lambda slot: True),
            unittest.mock.patch.object(broker, "_save_and_exit", lambda slot: True),
            unittest.mock.patch.object(broker, "_restart_selkies", lambda: None),
            unittest.mock.patch.object(broker, "_pactl", self._fake_pactl),
        ]
        for p in self.patches:
            p.start()

        self.launches = []
        self.pactl_calls = []
        self.pactl_returncode = 0

        reset_session()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), broker.BrokerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        reset_session()

    # ── stubs ─────────────────────────────────────────────────────────────

    def _fake_launch(self, rom_path, state_path=None):
        self.launches.append((rom_path, state_path))

    def _fake_pactl(self, *args):
        import subprocess

        self.pactl_calls.append(args)
        return subprocess.CompletedProcess(
            list(args), self.pactl_returncode, "Mute: no\n", "pactl said no"
        )

    # ── request helper ────────────────────────────────────────────────────

    def request(self, method, path, body=None, secret=SECRET, raw=None) -> Response:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = raw if raw is not None else (json.dumps(body).encode() if body else None)
        req = urllib.request.Request(url, data=data, method=method)
        if secret is not None:
            req.add_header("X-Broker-Secret", secret)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return Response(exc.code, dict(exc.headers), exc.read())

    def start_session(self, rom_name="game.iso"):
        rom = write(self.rom_root / rom_name)
        with broker._session_lock:
            broker._session["rom_path"] = str(rom)
            broker._session["rom_name"] = rom.stem
        return rom


class Auth(ApiTestCase):
    def test_health_is_open_for_container_healthchecks(self):
        resp = self.request("GET", "/health", secret=None)
        self.assertEqual(resp.status, 200)

    def test_every_other_get_requires_the_secret(self):
        for path in ("/status", "/state-file", "/save-file", "/memory-card"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, secret=None).status, 403)

    def test_post_requires_the_secret(self):
        resp = self.request("POST", "/launch", {"rom_path": "/x"}, secret=None)
        self.assertEqual(resp.status, 403)

    def test_put_requires_the_secret(self):
        resp = self.request("PUT", "/save-file", raw=b"x", secret=None)
        self.assertEqual(resp.status, 403)

    def test_delete_requires_the_secret(self):
        self.assertEqual(self.request("DELETE", "/launch", secret=None).status, 403)

    def test_a_wrong_secret_is_rejected(self):
        self.assertEqual(self.request("GET", "/status", secret="wrong").status, 403)

    def test_a_secret_prefix_is_rejected(self):
        self.assertEqual(
            self.request("GET", "/status", secret=self.SECRET[:-1]).status, 403
        )

    def test_unknown_routes_are_404(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                resp = self.request(method, "/nope", raw=b"")
                self.assertEqual(resp.status, 404)


class Status(ApiTestCase):
    def test_reports_inactive_with_no_game(self):
        body = self.request("GET", "/status").json()
        self.assertFalse(body["active"])
        self.assertIsNone(body["rom_path"])

    def test_publishes_the_slot_the_broker_actually_uses(self):
        body = self.request("GET", "/status").json()
        self.assertEqual(body["save_slot"], broker.SAVE_SLOT)
        self.assertEqual(body["autosave_slot"], broker.AUTOSAVE_SLOT)

    def test_a_dead_process_is_not_reported_as_active(self):
        self.start_session()
        body = self.request("GET", "/status").json()
        self.assertFalse(body["active"])


class Launch(ApiTestCase):
    def test_launches_a_rom_inside_the_library(self):
        rom = write(self.rom_root / "game.iso")
        resp = self.request("POST", "/launch", {"rom_path": str(rom)})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["status"], "launching")

    def test_requires_a_rom_path(self):
        self.assertEqual(self.request("POST", "/launch", {}).status, 400)
        self.assertEqual(
            self.request("POST", "/launch", {"rom_path": "   "}).status, 400
        )

    def test_rejects_a_rom_outside_the_library(self):
        resp = self.request("POST", "/launch", {"rom_path": "/etc/passwd"})
        self.assertEqual(resp.status, 400)
        self.assertIn("rom_root", resp.json())

    def test_reports_a_missing_rom_separately_from_a_rejected_one(self):
        resp = self.request(
            "POST", "/launch", {"rom_path": str(self.rom_root / "absent.iso")}
        )
        self.assertEqual(resp.status, 422)

    def test_rejects_an_out_of_range_load_slot(self):
        rom = write(self.rom_root / "game.iso")
        for slot in (-1, broker.MAX_SLOT + 1, "8", 1.5):
            with self.subTest(slot=slot):
                resp = self.request(
                    "POST", "/launch", {"rom_path": str(rom), "load_slot": slot}
                )
                self.assertEqual(resp.status, 400)

    def test_load_slot_zero_resolves_to_the_default_slot(self):
        # Every other route treats 0 as "the default slot"; /launch used to be
        # the one place it was a 400.
        rom = write(self.rom_root / "game.iso")
        write(self.state_dir / f"GALE01.s{broker.SAVE_SLOT:02d}")
        resp = self.request("POST", "/launch", {"rom_path": str(rom), "load_slot": 0})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["load_slot"], broker.SAVE_SLOT)

    def test_reports_a_resume_slot_with_no_state_instead_of_booting_unresumed(self):
        rom = write(self.rom_root / "game.iso")
        resp = self.request("POST", "/launch", {"rom_path": str(rom), "load_slot": 3})
        self.assertEqual(resp.status, 404)
        self.assertEqual(self.launches, [])

    def test_passes_the_resolved_state_file_to_the_launcher(self):
        rom = write(self.rom_root / "game.iso")
        state = write(self.state_dir / "GALE01.s03")
        resp = self.request("POST", "/launch", {"rom_path": str(rom), "load_slot": 3})
        self.assertEqual(resp.status, 200)
        self.wait_for_launch()
        self.assertEqual(self.launches[-1], (str(rom.resolve()), str(state)))

    def test_launch_is_refused_while_a_save_is_in_flight(self):
        rom = write(self.rom_root / "game.iso")
        with broker._session_lock:
            broker._session["save_in_progress"] = True
        resp = self.request("POST", "/launch", {"rom_path": str(rom)})
        self.assertEqual(resp.status, 409)

    def test_delete_returns_to_the_dashboard(self):
        resp = self.request("DELETE", "/launch")
        self.assertEqual(resp.status, 200)
        self.wait_for_launch()
        self.assertEqual(self.launches[-1], (None, None))

    def wait_for_launch(self, timeout=5.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.launches:
            time.sleep(0.02)
        self.assertTrue(self.launches, "launch thread never ran")


class SaveAndLoadState(ApiTestCase):
    def test_save_state_requires_a_running_game(self):
        self.assertEqual(self.request("POST", "/save-state", {}).status, 409)

    def test_load_state_requires_a_running_game(self):
        self.assertEqual(self.request("POST", "/load-state", {}).status, 409)

    def test_load_state_is_refused_while_a_save_is_running(self):
        # Every other slot route already refuses this; a load that lands
        # mid-save rolls the player back onto the state being overwritten.
        self.start_session()
        with broker._session_lock:
            broker._session["save_in_progress"] = True
        try:
            resp = self.request("POST", "/load-state", {})
        finally:
            with broker._session_lock:
                broker._session["save_in_progress"] = False
        self.assertEqual(resp.status, 409)

    def test_save_state_defaults_to_the_configured_slot(self):
        self.start_session()
        body = self.request("POST", "/save-state", {}).json()
        self.assertEqual(body["slot"], broker.SAVE_SLOT)

    def test_slot_zero_means_the_default_slot(self):
        self.start_session()
        body = self.request("POST", "/save-state", {"slot": 0}).json()
        self.assertEqual(body["slot"], broker.SAVE_SLOT)

    def test_rejects_out_of_range_slots(self):
        self.start_session()
        for path in ("/save-state", "/load-state", "/save-and-exit"):
            for slot in (-1, broker.MAX_SLOT + 1, "8", None, 1.5):
                with self.subTest(path=path, slot=slot):
                    resp = self.request("POST", path, {"slot": slot})
                    self.assertEqual(resp.status, 400)

    def test_a_rejected_slot_does_not_leave_the_save_flag_stuck(self):
        self.start_session()
        self.request("POST", "/save-state", {"slot": 99})
        with broker._session_lock:
            self.assertFalse(broker._session["save_in_progress"])
        self.assertEqual(self.request("POST", "/save-state", {}).status, 200)

    def test_a_second_save_is_refused_while_one_is_in_flight(self):
        self.start_session()
        with broker._session_lock:
            broker._session["save_in_progress"] = True
        self.assertEqual(self.request("POST", "/save-state", {}).status, 409)
        self.assertEqual(self.request("POST", "/save-and-exit", {}).status, 409)

    def test_save_and_exit_reports_the_slot_it_used(self):
        self.start_session()
        body = self.request("POST", "/save-and-exit", {"slot": 2}).json()
        self.assertEqual(body["slot"], 2)
        self.assertTrue(body["saved"])

    def test_save_and_exit_can_be_queued_without_waiting(self):
        self.start_session()
        body = self.request("POST", "/save-and-exit", {"wait": False}).json()
        self.assertEqual(body["status"], "queued")


class Volume(ApiTestCase):
    def test_sets_a_valid_level(self):
        resp = self.request("POST", "/volume", {"level": 40})
        self.assertEqual(resp.status, 200)
        self.assertIn(("set-sink-volume", "@DEFAULT_SINK@", "40%"), self.pactl_calls)

    def test_accepts_the_range_boundaries(self):
        for level in (0, 100):
            with self.subTest(level=level):
                self.assertEqual(
                    self.request("POST", "/volume", {"level": level}).status, 200
                )

    def test_rejects_levels_outside_the_range(self):
        for level in (-1, 101, "50", None, 50.5):
            with self.subTest(level=level):
                self.assertEqual(
                    self.request("POST", "/volume", {"level": level}).status, 400
                )

    def test_a_pactl_failure_is_a_500_not_a_dropped_connection(self):
        self.pactl_returncode = 1
        self.assertEqual(self.request("POST", "/volume", {"level": 50}).status, 500)

    def test_mute_without_a_body_toggles(self):
        self.request("POST", "/mute", {})
        self.assertIn(("set-sink-mute", "@DEFAULT_SINK@", "toggle"), self.pactl_calls)

    def test_mute_with_an_explicit_value_sets_it(self):
        self.request("POST", "/mute", {"mute": True})
        self.assertIn(("set-sink-mute", "@DEFAULT_SINK@", "1"), self.pactl_calls)

    def test_a_pactl_mute_failure_is_a_500(self):
        self.pactl_returncode = 1
        self.assertEqual(self.request("POST", "/mute", {"mute": True}).status, 500)


class StateFileTransfer(ApiTestCase):
    def test_get_reports_a_slot_with_no_state(self):
        self.assertEqual(self.request("GET", "/state-file?slot=1").status, 404)

    def test_get_serves_the_state_and_names_it(self):
        write(self.state_dir / "GALE01.s08", b"statebytes")
        resp = self.request("GET", "/state-file?slot=8")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"statebytes")
        self.assertEqual(resp.headers["X-State-Filename"], "GALE01.s08")

    def test_get_defaults_to_the_configured_slot(self):
        write(self.state_dir / f"GALE01.s{broker.SAVE_SLOT:02d}", b"default slot")
        self.assertEqual(self.request("GET", "/state-file").body, b"default slot")

    def test_get_rejects_a_bad_slot(self):
        for query in ("?slot=abc", "?slot=-1", f"?slot={broker.MAX_SLOT + 1}"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.request("GET", "/state-file" + query).status, 400
                )

    def test_get_refuses_an_oversized_state_without_reading_it(self):
        # The size check used to come after read_bytes, so refusing a 4 GB
        # state still pulled 4 GB into the broker's address space first.
        write(self.state_dir / "GALE01.s08", b"far too long")
        with unittest.mock.patch.object(broker, "STATE_FILE_MAX_BYTES", 4):
            with unittest.mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("read the whole file")
            ):
                resp = self.request("GET", "/state-file?slot=8")
        self.assertEqual(resp.status, 413)

    def test_a_client_that_hangs_up_mid_pull_does_not_break_the_server(self):
        # The streamed write is unguarded no longer: a disconnect used to raise
        # BrokenPipeError out of the handler as an unhandled traceback.
        # Asserted on handle_error, not on the server still answering: the
        # server survives an unhandled exception either way, so a /health probe
        # passed just as well before the guard existed.
        import socket
        import struct

        errors = []
        self.server.handle_error = lambda request, addr: errors.append(
            sys.exc_info()[1]
        )
        # 16 MB so the write is still in progress when the client vanishes, and
        # SO_LINGER 0 so the close sends an RST rather than a polite FIN: a FIN
        # alone lets the kernel keep buffering and the write never fails.
        write(self.state_dir / "GALE01.s08", b"x" * (16 * 1024 * 1024))
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(
            b"GET /state-file?slot=8 HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"X-Broker-Secret: " + self.SECRET.encode() + b"\r\n\r\n"
        )
        sock.recv(64)
        sock.close()
        self.assertEqual(self.request("GET", "/health", secret=None).status, 200)
        # The handler runs on its own thread and hits the dead socket a moment
        # after the close, so give it one before reading the error list.
        time.sleep(0.5)
        self.assertEqual(errors, [])

    def test_put_stores_the_file_under_its_original_name(self):
        resp = self.request(
            "PUT", "/state-file?filename=GALE01.s08", raw=b"restored"
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual((self.state_dir / "GALE01.s08").read_bytes(), b"restored")

    def test_put_confines_a_traversing_filename_to_the_state_dir(self):
        self.request("PUT", "/state-file?filename=../../evil.s08", raw=b"x")
        self.assertFalse(Path(self.tmp.name, "evil.s08").exists())
        self.assertFalse(self.data_root.joinpath("evil.s08").exists())

    def test_put_rejects_filenames_that_are_not_state_files(self):
        for name in ("", "Dolphin.ini", "GALE01.sav", ".s08", "GALE01.sXY", "noext"):
            with self.subTest(name=name):
                resp = self.request("PUT", f"/state-file?filename={name}", raw=b"x")
                self.assertEqual(resp.status, 400)

    def test_put_rejects_a_slot_outside_the_supported_range(self):
        # .sNN parsed as a slot: .s99 is a well-formed name for a slot nothing
        # else reads back, so it must not be writable either.
        for name in ("GALE01.s00", "GALE01.s09", "GALE01.s99"):
            with self.subTest(name=name):
                resp = self.request("PUT", f"/state-file?filename={name}", raw=b"x")
                self.assertEqual(resp.status, 400)
                self.assertNotIn(name, [p.name for p in self.state_dir.iterdir()])

    def test_get_refuses_to_serve_while_a_save_is_still_running(self):
        write(self.state_dir / "GALE01.s08", b"state")
        with broker._session_lock:
            broker._session["save_in_progress"] = True
        try:
            with unittest.mock.patch.object(broker, "STATE_GET_WAIT", 0.3):
                resp = self.request("GET", "/state-file?slot=8")
        finally:
            with broker._session_lock:
                broker._session["save_in_progress"] = False
        self.assertEqual(resp.status, 409)

    def test_put_rejects_an_empty_body(self):
        resp = self.request("PUT", "/state-file?filename=GALE01.s08", raw=b"")
        self.assertEqual(resp.status, 400)

    def test_put_rejects_an_oversized_body(self):
        with unittest.mock.patch.object(broker, "STATE_FILE_MAX_BYTES", 4):
            resp = self.request(
                "PUT", "/state-file?filename=GALE01.s08", raw=b"far too long"
            )
        self.assertEqual(resp.status, 413)

    def test_round_trips_a_state_through_put_then_get(self):
        self.request("PUT", "/state-file?filename=GALE01.s08", raw=b"round trip")
        self.assertEqual(self.request("GET", "/state-file?slot=8").body, b"round trip")


class SaveFileTransfer(ApiTestCase):
    def test_get_reports_that_no_game_has_been_launched(self):
        resp = self.request("GET", "/save-file")
        self.assertEqual(resp.status, 404)
        # Tagged so RomM can tell "nothing to sync" apart from an unmarked 404
        # from an older mod that has no endpoint here at all.
        self.assertEqual(resp.headers.get("X-Save-File"), "absent")

    def test_get_distinguishes_unchanged_saves_from_an_absent_session(self):
        import time

        with broker._session_lock:
            broker._session["save_baseline"] = time.time() + 3600
        resp = self.request("GET", "/save-file")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.headers.get("X-Save-File"), "unchanged")

    def test_get_serves_saves_written_since_the_baseline(self):
        import time

        baseline = time.time()
        with broker._session_lock:
            broker._session["save_baseline"] = baseline
            broker._session["rom_name"] = "Melee"
        write(self.data_root / "GC" / "save.gci", b"gci", mtime=baseline + 1)
        resp = self.request("GET", "/save-file")
        self.assertEqual(resp.status, 200)
        self.assertEqual(zip_names(resp.body), {"GC/save.gci"})
        self.assertEqual(resp.headers["X-Save-Filename"], "Melee.saves.zip")

    def test_the_filename_header_survives_a_non_ascii_rom_name(self):
        import time

        baseline = time.time()
        with broker._session_lock:
            broker._session["save_baseline"] = baseline
            broker._session["rom_name"] = "ポケモン"
        write(self.data_root / "GC" / "save.gci", b"gci", mtime=baseline + 1)
        resp = self.request("GET", "/save-file")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["X-Save-Filename"], "dolphin.saves.zip")

    def test_put_restores_an_archive(self):
        resp = self.request("PUT", "/save-file", raw=make_zip({"GC/a.gci": b"one"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"status": "ok", "written": 1, "skipped": 0})

    def test_put_rejects_an_empty_body(self):
        self.assertEqual(self.request("PUT", "/save-file", raw=b"").status, 400)

    def test_put_rejects_a_hostile_archive(self):
        resp = self.request("PUT", "/save-file", raw=make_zip({"/etc/passwd": b"x"}))
        self.assertEqual(resp.status, 400)

    def test_put_rejects_an_oversized_body(self):
        with unittest.mock.patch.object(broker, "SAVE_FILE_MAX_BYTES", 4):
            resp = self.request("PUT", "/save-file", raw=make_zip({"GC/a.gci": b"x"}))
        self.assertEqual(resp.status, 413)

    def test_put_rejects_a_body_shorter_than_content_length(self):
        # Raw socket: urllib will not send fewer bytes than it declared, and a
        # short read used to be handed to the zip parser as a whole archive.
        import socket

        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(
                b"PUT /save-file HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"X-Broker-Secret: " + self.SECRET.encode() + b"\r\n"
                b"Content-Length: 64\r\n\r\nPK"
            )
            sock.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := sock.recv(4096):
                reply += chunk
        self.assertIn(b"400", reply.split(b"\r\n")[0])
        self.assertIn(b"truncated", reply)


class MemoryCardTransfer(ApiTestCase):
    def test_an_absent_card_is_tagged_so_it_is_not_mistaken_for_a_missing_route(self):
        resp = self.request("GET", "/memory-card")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.headers.get("X-Memory-Card"), "absent")

    def test_get_serves_the_slot_a_card(self):
        write(self.data_root / "romm" / "Card A" / "GALE01.gci", b"melee")
        resp = self.request("GET", "/memory-card")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["X-Memory-Card-Slot"], "A")
        self.assertEqual(zip_names(resp.body), {"GALE01.gci"})

    def test_put_hydrates_the_card(self):
        resp = self.request("PUT", "/memory-card", raw=make_zip({"GALE01.gci": b"m"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["written"], 1)

    def test_put_rejects_an_empty_body(self):
        self.assertEqual(self.request("PUT", "/memory-card", raw=b"").status, 400)

    def test_put_rejects_a_hostile_archive(self):
        resp = self.request("PUT", "/memory-card", raw=make_zip({"../x.gci": b"x"}))
        self.assertEqual(resp.status, 400)

    def test_round_trips_a_card(self):
        write(self.data_root / "romm" / "Card A" / "GALE01.gci", b"melee")
        archive = self.request("GET", "/memory-card").body
        self.request("PUT", "/memory-card", raw=make_zip({"other.gci": b"x"}))
        self.request("PUT", "/memory-card", raw=archive)
        card = self.data_root / "romm" / "Card A"
        self.assertEqual((card / "GALE01.gci").read_bytes(), b"melee")
        self.assertFalse((card / "other.gci").exists())


class StateScreenshot(ApiTestCase):
    def test_reports_a_slot_with_no_thumbnail(self):
        self.assertEqual(self.request("GET", "/state-screenshot?slot=1").status, 404)

    def test_serves_the_thumbnail_as_png(self):
        write(self.data_root / "romm" / "state-shots" / "slot08.png", b"\x89PNG-ish")
        resp = self.request("GET", "/state-screenshot?slot=8")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "image/png")
        self.assertEqual(resp.body, b"\x89PNG-ish")

    def test_defaults_to_the_configured_slot(self):
        name = f"slot{broker.SAVE_SLOT:02d}.png"
        write(self.data_root / "romm" / "state-shots" / name, b"png")
        self.assertEqual(self.request("GET", "/state-screenshot").status, 200)

    def test_rejects_a_bad_slot(self):
        self.assertEqual(self.request("GET", "/state-screenshot?slot=x").status, 400)


class MalformedRequests(ApiTestCase):
    def test_a_body_that_is_not_json_is_rejected_not_defaulted(self):
        # It used to parse as {}, so a typo'd /launch quietly became a
        # dashboard reset instead of an error the caller could see.
        resp = self.request("POST", "/volume", raw=b"{not json")
        self.assertEqual(resp.status, 400)
        self.assertIn("JSON", resp.json()["error"])
        self.assertEqual(self.request("GET", "/health", secret=None).status, 200)

    def test_a_json_body_that_is_not_an_object_is_rejected(self):
        self.assertEqual(self.request("POST", "/volume", raw=b"[1,2]").status, 400)

    def test_bad_json_on_a_save_route_releases_the_in_progress_flag(self):
        self.start_session()
        self.assertEqual(
            self.request("POST", "/save-state", raw=b"{oops").status, 400
        )
        with broker._session_lock:
            self.assertFalse(broker._session["save_in_progress"])
        self.assertEqual(self.request("POST", "/save-state", body={}).status, 200)

    def test_a_non_string_rom_path_is_rejected(self):
        resp = self.request("POST", "/launch", {"rom_path": 42})
        self.assertEqual(resp.status, 400)

    def test_a_missing_body_does_not_crash_the_handler(self):
        self.assertEqual(self.request("POST", "/volume", raw=b"").status, 400)

    def test_the_server_survives_every_endpoint_being_called_bodiless(self):
        paths = ("/cleanup", "/volume", "/mute", "/save-state", "/load-state",
                 "/save-and-exit", "/launch")
        for path in paths:
            with self.subTest(path=path):
                resp = self.request("POST", path, raw=b"")
                self.assertLess(resp.status, 500, path)


if __name__ == "__main__":
    unittest.main()

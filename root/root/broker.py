#!/usr/bin/env python3
"""broker.py — launch Dolphin on demand and expose a small HTTP API."""

import glob
import hmac
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("BROKER_PORT", "8000"))
SECRET     = os.environ.get("BROKER_SECRET", "")
ROM_ROOT   = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
SAVE_SLOT  = int(os.environ.get("SAVE_SLOT", "1"))   # default slot for save-and-exit (1–8)
SSTATE_WAIT = float(os.environ.get("SSTATE_WAIT", "3.0"))  # seconds to wait after save key

ENV = {
    "DISPLAY":            ":0",
    "WAYLAND_DISPLAY":    os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "QT_QPA_PLATFORM":    "xcb",
    "PULSE_RUNTIME_PATH": "/defaults",
    "DRI_NODE":           os.environ.get("DRI_NODE", ""),
    "DRINODE":            os.environ.get("DRINODE", ""),
    "HOME":               "/config",
    "USER":               "abc",
    # The joystick interposer hooks open() on /dev/input/* and redirects to
    # selkies Unix sockets so controller input reaches Dolphin.
    "LD_PRELOAD": "/usr/lib/selkies_joystick_interposer.so",
}

# Dolphin on this image writes all config files directly to
# ~/.config/dolphin-emu/ — there is no Config/ subdirectory.
INI_PATH = Path("/config/.config/dolphin-emu/Dolphin.ini")

logging.basicConfig(
    level=getattr(logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_rom_path(raw: str) -> Path | None:
    """Resolve raw to an absolute path and confirm it lives under ROM_ROOT."""
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


def _patch_ini(fullscreen: bool = False):
    """Patch Dolphin.ini to set required broker defaults.

    fullscreen=True when launching a game (fills stream with game content),
    False for the idle dashboard (windowed, avoids black screen on boot).
    """
    INI_PATH.parent.mkdir(parents=True, exist_ok=True)

    fs_val = "True" if fullscreen else "False"

    if not INI_PATH.exists():
        INI_PATH.write_text(
            "[General]\n"
            "BackgroundInput = True\n"
            "\n"
            "[Core]\n"
            "SIDevice0 = 6\n"
            "SIDevice1 = 0\n"
            "SIDevice2 = 0\n"
            "SIDevice3 = 0\n"
            "GFXBackend = Vulkan\n"
            "CPUThread = False\n"
            "\n"
            "[Interface]\n"
            "ConfirmStop = False\n"
            "\n"
            "[Display]\n"
            "RenderToMain = True\n"
            f"Fullscreen = {fs_val}\n"
            "\n"
            "[Analytics]\n"
            "Enabled = False\n"
            "PermissionAsked = True\n"
        )
        log.info("Created Dolphin.ini with broker defaults (fullscreen=%s)", fullscreen)
        return

    target = {
        "General": {
            "BackgroundInput": "True",
        },
        "Core": {
            "SIDevice0": "6",
            "SIDevice1": "0",
            "SIDevice2": "0",
            "SIDevice3": "0",
            "GFXBackend": "Vulkan",
            "CPUThread": "False",
        },
        "Interface": {"ConfirmStop": "False"},
        "Display": {"RenderToMain": "True", "Fullscreen": fs_val},
        "Analytics": {
            "Enabled": "False",
            "PermissionAsked": "True",
        },
    }

    try:
        lines = INI_PATH.read_text().splitlines()
        current_section: str | None = None
        applied: dict[str, set] = {s: set() for s in target}
        new_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1]
                new_lines.append(line)
                continue

            if current_section in target:
                for key, val in target[current_section].items():
                    if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                        new_lines.append(f"{key} = {val}")
                        applied[current_section].add(key)
                        break
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Append any keys that weren't found in the file.
        for section, keys in target.items():
            missing = {k: v for k, v in keys.items() if k not in applied[section]}
            if missing:
                new_lines.append(f"[{section}]")
                for k, v in missing.items():
                    new_lines.append(f"{k} = {v}")
                    log.warning("Dolphin.ini: [%s] %s not found — appended", section, k)

        tmp = INI_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(INI_PATH)
        log.debug("Dolphin.ini patched (ConfirmStop)")
    except Exception as exc:
        log.error("Failed to patch Dolphin.ini: %s", exc)


def _kill_dolphin():
    """Kill the managed dolphin-emu process group."""
    with _session_lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None

    if proc is None or proc.poll() is not None:
        return

    log.info("Stopping Dolphin (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Dolphin did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            proc.wait()
    except ProcessLookupError:
        pass  # already gone


def _launch_dolphin_internal(rom_path):
    """Launch dolphin-emu as abc via sudo+env."""
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "/usr/games/dolphin-emu",
    ]
    if rom_path:
        # Use --exec=path (assignment form) so the path is captured as the
        # flag's value. Dolphin does not support '--' as a POSIX end-of-options
        # marker and would treat it as a literal filename.
        # --batch is intentionally omitted: it suppresses Dolphin's Qt event
        # loop, which is where SDL joystick polling happens.  Without the event
        # loop, controller input never reaches the running game.  Without
        # --batch, when emulation stops Dolphin returns to its main menu rather
        # than exiting — this is the desired idle behaviour.
        cmd.append(f"--exec={rom_path}")

    log.info("Launching Dolphin (rom=%s)", rom_path or "dashboard")
    log.debug("Launching: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("Failed to launch Dolphin: %s", exc)
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("Dolphin launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()
    Thread(target=_log_dolphin_output, args=(proc,), daemon=True).start()
    Thread(target=_diag_window, args=(proc.pid,), daemon=True).start()
    if rom_path:
        Thread(target=_raise_game_window, daemon=True).start()


def _raise_game_window():
    """Minimize the main Dolphin Qt window once the game render window appears.

    With QT_QPA_PLATFORM=xcb + Vulkan, Dolphin creates a separate top-level
    render window (title: "Dolphin ... | JIT64 ... | <game>") that sits below
    the main Qt menu window in the X11 stacking order.  labwc re-raises the
    focused window, so windowraise doesn't stick.  The reliable fix is to
    minimize (iconify) the main menu window so only the render window is visible.
    """
    xdo_base = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            ids = subprocess.check_output(
                xdo_base + ["search", "--classname", "dolphin-emu"],
                text=True, timeout=5,
            ).strip().split()
        except Exception:
            continue

        game_wid = None
        menu_wid = None
        for wid in ids:
            try:
                title = subprocess.check_output(
                    xdo_base + ["getwindowname", wid],
                    text=True, timeout=3,
                ).strip()
            except Exception:
                continue
            if " | " in title:
                game_wid = wid
                log.info("[raise] Game render window %s: %s", wid, title)
            elif title.startswith("Dolphin"):
                menu_wid = wid

        if game_wid and menu_wid:
            try:
                subprocess.run(xdo_base + ["windowminimize", menu_wid], timeout=5)
                log.info("[raise] Minimized main menu window %s", menu_wid)
            except Exception as exc:
                log.warning("[raise] Could not minimize menu window %s: %s", menu_wid, exc)
            try:
                subprocess.run(
                    xdo_base + ["windowstate", "--add", "FULLSCREEN", game_wid],
                    timeout=5,
                )
                log.info("[raise] Set game window %s to fullscreen", game_wid)
            except Exception as exc:
                log.warning("[raise] Could not fullscreen game window %s: %s", game_wid, exc)
            return

    log.warning("[raise] Game render window not found within 20 seconds")


def _log_dolphin_output(proc):
    """Log Dolphin stdout/stderr to the broker log for crash diagnosis."""
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.info("[dolphin] %s", line)
    except Exception:
        pass


def _diag_window(pid: int):
    """After a short delay, check whether Dolphin has an X11 window via xdotool.

    Uses classname search rather than --pid because proc.pid is the sudo wrapper,
    not the actual dolphin-emu process, so --pid never finds the window.
    """
    time.sleep(4)
    try:
        out = subprocess.check_output(
            ["sudo", "-u", "abc", "env",
             f"DISPLAY={ENV['DISPLAY']}", f"HOME={ENV['HOME']}",
             "xdotool", "search", "--classname", "dolphin-emu"],
            text=True, timeout=10,
        ).strip()
        if out:
            log.info("[diag] Dolphin has X11 window(s): %s", out)
        else:
            log.warning("[diag] Dolphin has NO X11 windows (sudo PID %d) — check Xwayland/rendering", pid)
    except subprocess.CalledProcessError:
        log.warning("[diag] xdotool found no X11 windows for Dolphin (sudo PID %d)", pid)
    except Exception as exc:
        log.warning("[diag] xdotool check failed: %s", exc)


def _monitor_process(proc, start_time):
    """On unexpected exit, relaunch the dashboard if the session is still managed."""
    proc.wait()
    duration = time.monotonic() - start_time

    with _session_lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        return

    wait_time = 5 if duration < 5 else 1
    log.info("Dolphin exited after %.1fs — relaunching dashboard in %ds", duration, wait_time)
    time.sleep(wait_time)

    with _session_lock:
        if not _session["is_managed"]:
            return

    _launch_dolphin(None)


def _cleanup_stale_sockets():
    """Remove only stale/unreachable selkies socket files.

    Does NOT send EOF — sending EOF disconnects the browser gamepad client,
    which breaks input for the new Dolphin instance. The interposer will
    reconnect to existing sockets automatically; we only clean up sockets
    that have become orphaned (no listener on the other end).
    """
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.debug("Socket cleanup: no gamepad sockets found.")
        return

    removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
            log.debug("Socket cleanup: %s is alive, leaving it.", path)
        except OSError:
            try:
                os.unlink(path)
                removed += 1
                log.debug("Socket cleanup: removed stale socket %s", path)
            except OSError:
                pass

    if removed:
        log.debug(
            "Socket cleanup: removed %d stale socket(s) (of %d total).",
            removed,
            len(paths),
        )


def _launch_dolphin(rom_path):
    _kill_dolphin()
    _patch_ini(fullscreen=bool(rom_path))
    time.sleep(2)
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _launch_dolphin_internal(rom_path)


# ── xdotool helpers ───────────────────────────────────────────────────────────

_XDOTOOL_ENV = {
    "DISPLAY":         ":0",
    "HOME":            "/config",
    "USER":            "abc",
    "XDG_RUNTIME_DIR": ENV["XDG_RUNTIME_DIR"],
}


def _xdotool_find_window() -> str | None:
    """Return the X11 window ID for dolphin-emu, or None if not found."""
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", "dolphin-emu"], text=True
        ).split()
    except subprocess.CalledProcessError:
        log.error("xdotool: dolphin-emu process not found")
        return None

    xdo_base = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )

    for pid in pids:
        try:
            out = subprocess.check_output(
                xdo_base + ["search", "--onlyvisible", "--pid", pid],
                text=True, timeout=5,
            )
            ids = out.strip().split()
            if ids:
                wid = ids[-1]
                log.debug("xdotool: found window %s for PID %s", wid, pid)
                return wid
        except Exception as exc:
            log.debug("xdotool: window search failed for PID %s: %s", pid, exc)

    # Fallback: search by class name
    try:
        out = subprocess.check_output(
            xdo_base + ["search", "--onlyvisible", "--classname", "dolphin-emu"],
            text=True, timeout=5,
        )
        ids = out.strip().split()
        if ids:
            wid = ids[-1]
            log.debug("xdotool: found window %s by classname fallback", wid)
            return wid
    except Exception as exc:
        log.debug("xdotool: classname search failed: %s", exc)

    log.error("xdotool: Dolphin window not found")
    return None


def _xdotool_key(wid: str, key: str) -> bool:
    """Send a single key to the Dolphin window. Returns False on error."""
    xdo_cmd = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool", "key", "--window", wid, key]
    )
    try:
        subprocess.run(xdo_cmd, timeout=5, check=True)
        return True
    except Exception as exc:
        log.error("xdotool: key %r failed: %s", key, exc)
        return False


def _xdotool_save_state(slot: int) -> bool:
    """Save emulator state to slot (1–8) via Shift+F{slot}.

    Dolphin maps Shift+F1–Shift+F8 directly to save slots 1–8, so no slot
    cycling is needed. Sends the key then waits SSTATE_WAIT seconds for the
    write to complete before returning.
    """
    wid = _xdotool_find_window()
    if wid is None:
        return False
    if not _xdotool_key(wid, f"shift+F{slot}"):
        return False
    log.info("xdotool: shift+F%d sent to window %s — waiting %.1fs for write", slot, wid, SSTATE_WAIT)
    time.sleep(SSTATE_WAIT)
    return True


def _xdotool_load_state(slot: int) -> bool:
    """Load emulator state from slot (1–8) via F{slot}.

    Dolphin maps F1–F8 directly to load slots 1–8.
    """
    wid = _xdotool_find_window()
    if wid is None:
        return False
    if not _xdotool_key(wid, f"F{slot}"):
        return False
    log.info("xdotool: F%d sent to window %s", slot, wid)
    return True


def _save_and_exit(slot: int) -> bool:
    """Save emulator state then kill Dolphin. Returns True if save key was sent."""
    ok = _xdotool_save_state(slot)
    _kill_dolphin()
    return ok


# ── PulseAudio helpers ────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance."""
    return subprocess.run(
        _PACTL_CMD + ["pactl"] + list(args),
        capture_output=True, text=True, timeout=5,
    )


def _pactl_get_mute() -> bool | None:
    """Return current mute state as bool, or None on error."""
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        return None
    return result.stdout.strip().endswith("yes")


def _cleanup_sockets():
    """Restart selkies to flush all stale gamepad connections."""
    log.info("Socket cleanup: restarting selkies...")
    result = subprocess.run(["pkill", "-15", "-f", "selkies"], capture_output=True)
    if result.returncode == 0:
        log.info("Socket cleanup: selkies stopped, s6 will restart it shortly.")
    else:
        log.warning("Socket cleanup: selkies not found or already stopped.")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Broker-Secret", ""),
            SECRET,
        )

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                snap = dict(_session) if active else {}
            self._send_json(200, {
                "active":     active,
                "rom_path":   snap.get("rom_path")   if active else None,
                "rom_name":   snap.get("rom_name")   if active else None,
                "started_at": snap.get("started_at") if active else None,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/cleanup":
            Thread(target=_cleanup_sockets, daemon=True).start()
            self._send_json(200, {"status": "cleanup started"})
            return

        if self.path == "/save-and-exit":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", SAVE_SLOT)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–8"})
                return
            wait = body.get("wait", True)
            if wait:
                try:
                    ok = _save_and_exit(slot)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-and-exit: save key failed (slot %d) — killed anyway", slot)
                self._send_json(200, {"status": "ok", "saved": ok, "slot": slot})
                Thread(target=_launch_dolphin, args=(None,), daemon=True).start()
            else:
                def _bg(s):
                    try:
                        ok = _save_and_exit(s)
                    finally:
                        with _session_lock:
                            _session["save_in_progress"] = False
                    if not ok:
                        log.warning("save-and-exit: save key failed (slot %d) — killed anyway", s)
                    _launch_dolphin(None)
                Thread(target=_bg, args=(slot,), daemon=True).start()
                self._send_json(200, {"status": "queued", "slot": slot})
            return

        if self.path == "/volume":
            body = self._read_body()
            level = body.get("level")
            if not isinstance(level, int) or not (0 <= level <= 100):
                self._send_json(400, {"error": "level must be an integer 0–100"})
                return
            result = _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            log.info("Volume set to %d%%", level)
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            if "mute" in body:
                mute_arg = "1" if body["mute"] else "0"
            else:
                mute_arg = "toggle"
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            mute_state = _pactl_get_mute()
            log.info("Mute %s", "on" if mute_state else "off")
            self._send_json(200, {"status": "ok", "mute": mute_state})
            return

        if self.path == "/save-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–8"})
                return

            def _bg_save(s):
                try:
                    ok = _xdotool_save_state(s)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-state: key delivery failed for slot %d", s)

            Thread(target=_bg_save, args=(slot,), daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/load-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                self._send_json(400, {"error": "slot must be 1–8"})
                return
            ok = _xdotool_load_state(slot)
            self._send_json(200 if ok else 500, {"status": "ok" if ok else "error", "loaded": ok, "slot": slot})
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        with _session_lock:
            if _session["save_in_progress"]:
                self._send_json(409, {"error": "save in progress"})
                return

        body = self._read_body()
        raw_path = body.get("rom_path", "").strip()

        if not raw_path:
            self._send_json(400, {"error": "rom_path is required"})
            return

        rom_path = _validate_rom_path(raw_path)
        if rom_path is None:
            self._send_json(400, {
                "error": "rom_path must be within ROM_ROOT",
                "rom_root": str(ROM_ROOT),
            })
            return
        if not rom_path.exists():
            self._send_json(422, {"error": "rom_path does not exist", "path": str(rom_path)})
            return

        Thread(target=_launch_dolphin, args=(str(rom_path),), daemon=True).start()
        self._send_json(200, {"status": "launching", "rom_path": str(rom_path)})

    def do_DELETE(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        Thread(target=_launch_dolphin, args=(None,), daemon=True).start()
        log.info("Soft reset: returning to dashboard")
        self._send_json(200, {"status": "resetting"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Broker starting — waiting 5s for desktop...")
    if not SECRET:
        log.warning("BROKER_SECRET is not set — all POST/DELETE endpoints are unauthenticated")
    time.sleep(5)

    # Kill any stale Dolphin instance left from a previous broker run.
    result = subprocess.run(["pkill", "-9", "-f", "/usr/games/dolphin-emu"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale dolphin-emu instance(s) on startup.")
        time.sleep(2)

    _patch_ini()

    # Clean up any leftover selkies socket files from a previous container run.
    # Only done once at startup — not on game launches, where webrtc_input is
    # already running and manages its own socket lifecycle.
    _cleanup_stale_sockets()

    # Launch Dolphin into its main menu so the stream shows something useful
    # whenever no game is playing.  Game launches kill this instance first.
    Thread(target=_launch_dolphin, args=(None,), daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""broker.py — launch Dolphin on demand and expose a small HTTP API."""

import glob
import hmac
import io
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread, Lock
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("BROKER_PORT", "8000"))
SECRET     = os.environ.get("BROKER_SECRET", "")
ROM_ROOT   = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
SAVE_SLOT     = int(os.environ.get("SAVE_SLOT", "1"))  # default slot for manual save-and-exit (1–7)
AUTOSAVE_SLOT = 8                                       # slot 8 is reserved exclusively for auto-saves
if not (1 <= SAVE_SLOT <= 7):
    raise SystemExit(f"SAVE_SLOT must be 1–7, got {SAVE_SLOT}")
# Max seconds to poll for the savestate write to complete after the save key.
# Returns as soon as the file size is stable, so raising this only affects the
# failure path. Falls back to a fixed 3 s sleep if the StateSaves dir is absent.
SSTATE_WAIT   = float(os.environ.get("SSTATE_WAIT", "10.0"))
# Resume-from-state (launch with load_slot): how long to wait for the game
# window to appear, and how long to let the game settle before the deferred
# slot load. Dolphin has no IPC status probe, so the settle is generous.
RESUME_LOAD_WAIT   = float(os.environ.get("RESUME_LOAD_WAIT",   "90.0"))
RESUME_LOAD_SETTLE = float(os.environ.get("RESUME_LOAD_SETTLE", "8.0"))

# Dolphin writes savestates to StateSaves under its data dir; the location
# varies by build (XDG vs. non-XDG layout), so both are probed at save time.
_SSTATE_DIR_CANDIDATES = (
    Path("/config/.local/share/dolphin-emu/StateSaves"),
    Path("/config/.config/dolphin-emu/StateSaves"),
)

ENV = {
    "DISPLAY":            ":0",
    # WAYLAND_DISPLAY intentionally omitted: if set, Dolphin's Vulkan backend
    # creates a VK_KHR_wayland_surface and renders directly to the Wayland
    # compositor, leaving the X11 window black.  Without it, Vulkan uses
    # VK_KHR_xcb_surface and renders into the X11 window that Xwayland
    # composites into labwc — which is what selkies captures.
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "QT_QPA_PLATFORM":    "xcb",
    "PULSE_RUNTIME_PATH": "/defaults",
    "DRI_NODE":           os.environ.get("DRI_NODE", ""),
    "DRINODE":            os.environ.get("DRINODE", ""),
    "HOME":               "/config",
    "USER":               "abc",
    # The joystick interposer hooks open() on /dev/input/* and redirects to
    # selkies Unix sockets so controller input reaches Dolphin. The fake
    # libudev is required alongside it: /dev/input is empty in the container,
    # so SDL's udev enumeration finds no devices without it and Dolphin never
    # opens the interposer's virtual pads.
    "LD_PRELOAD": os.environ.get("LD_PRELOAD")
    or "/usr/lib/selkies_joystick_interposer.so:/opt/lib/libudev.so.1.0.0-fake",
}

# Dolphin on this image writes all config files directly to
# ~/.config/dolphin-emu/ — there is no Config/ subdirectory.
INI_PATH = Path("/config/.config/dolphin-emu/Dolphin.ini")

# Dolphin's stdout/stderr is redirected at fd level to this file. A Python
# reader thread on a PIPE is not used: if the reader dies, Dolphin blocks
# forever once the 64 KB pipe buffer fills.
DOLPHIN_LOG_PATH = Path(os.environ.get("DOLPHIN_LOG_PATH", "/config/dolphin.log"))
_LOG_UID = int(os.environ.get("PUID", "1000"))
_LOG_GID = int(os.environ.get("PGID", "1000"))

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
    # True from POST /launch until _launch_dolphin has swapped instances —
    # gates the deferred resume load so it never fires at the old window.
    "launch_in_progress": False,
    # Wall-clock stamp of the last GAME launch (not dashboard relaunches).
    # GET /save-file only ships files modified at or after this point; it
    # survives game exit so RomM can still pull after the session ends.
    "save_baseline": None,
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
    try:
        INI_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("Could not create %s: %s — skipping ini patch",
                  INI_PATH.parent, exc)
        return

    fs_val = "True" if fullscreen else "False"

    if not INI_PATH.exists():
        try:
            INI_PATH.write_text(
                "[General]\n"
                "BackgroundInput = True\n"
                "\n"
                "[Core]\n"
                "SIDevice0 = 6\n"
                "SIDevice1 = 0\n"
                "SIDevice2 = 0\n"
                "SIDevice3 = 0\n"
                "GFXBackend = OpenGL\n"
                "CPUThread = False\n"
                "\n"
                "[Interface]\n"
                "ConfirmStop = False\n"
                "\n"
                "[Display]\n"
                "RenderToMain = False\n"
                f"Fullscreen = {fs_val}\n"
                "\n"
                "[Analytics]\n"
                "Enabled = False\n"
                "PermissionAsked = True\n"
            )
        except OSError as exc:
            log.error("Could not create %s: %s — broker defaults not applied",
                      INI_PATH, exc)
            return
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
            "GFXBackend": "OpenGL",
            "CPUThread": "False",
        },
        "Interface": {"ConfirmStop": "False"},
        "Display": {"RenderToMain": "False", "Fullscreen": fs_val},
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

        # Insert missing keys under their existing section header; only create
        # the section if the file doesn't have it at all. Blindly appending a
        # second [section] block at EOF accumulates duplicate headers.
        for section, keys in target.items():
            missing = {k: v for k, v in keys.items() if k not in applied[section]}
            if not missing:
                continue
            header_idx = next(
                (i for i, l in enumerate(new_lines) if l.strip() == f"[{section}]"),
                None,
            )
            add_lines = [f"{k} = {v}" for k, v in missing.items()]
            if header_idx is None:
                new_lines.append(f"[{section}]")
                new_lines.extend(add_lines)
            else:
                new_lines[header_idx + 1:header_idx + 1] = add_lines
            for k in missing:
                log.warning("Dolphin.ini: [%s] %s not found — inserted", section, k)

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
        _session["rom_path"] = None
        _session["rom_name"] = None
        _session["started_at"] = None

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
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("Dolphin did not exit after SIGKILL — giving up")
    except ProcessLookupError:
        pass  # already gone


def _wait_dolphin_gone(timeout: float = 5.0) -> None:
    """Poll until no dolphin-emu process remains, so the new instance doesn't
    race the old one for the X display and gamepad sockets."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-x", "dolphin-emu"], capture_output=True).returncode != 0:
            return
        time.sleep(0.2)
    log.warning("dolphin-emu still running after %.1fs — continuing anyway", timeout)


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

    # Append mode keeps history across launches. Failure to open is non-fatal:
    # Dolphin still launches, just without captured output.
    log_fh = None
    try:
        log_fh = open(DOLPHIN_LOG_PATH, "ab", buffering=0)
        try:
            os.chown(DOLPHIN_LOG_PATH, _LOG_UID, _LOG_GID)
        except (OSError, PermissionError):
            pass
        log_fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch (rom={rom_path or 'dashboard'}) ===\n".encode())
        log_fh.flush()
    except OSError as exc:
        log.warning("Cannot open %s for Dolphin output capture (%s); continuing without capture.", DOLPHIN_LOG_PATH, exc)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("Failed to launch Dolphin: %s", exc)
        if log_fh:
            log_fh.close()
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return
    finally:
        # Popen dup'd the fd; close our handle so it isn't leaked.
        if log_fh:
            log_fh.close()

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("Dolphin launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()


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
    try:
        _launch_dolphin_inner(rom_path)
    finally:
        with _session_lock:
            _session["launch_in_progress"] = False


def _launch_dolphin_inner(rom_path):
    # Auto-save before killing the current game (covers both navigate-away and
    # game-switching).  Skipped when no game is running, when the emulator
    # already died (crash-relaunch path — a keystroke to a dead process would
    # just poll for a savestate write that never comes), or when a save is
    # already in progress (e.g. called from /save-and-exit which saves first
    # then kills).  Read and set save_in_progress atomically to avoid a TOCTOU
    # race with a concurrent /save-and-exit request.
    with _session_lock:
        old_game = _session["rom_path"]
        emulator_alive = (
            _session["process"] is not None
            and _session["process"].poll() is None
        )
        if old_game is not None and emulator_alive and not _session["save_in_progress"]:
            _session["save_in_progress"] = True
            do_autosave = True
        else:
            do_autosave = False

    if do_autosave:
        try:
            ok = _xdotool_save_state(AUTOSAVE_SLOT)
            if ok:
                log.info("auto-save: saved to slot %d before leaving %s",
                         AUTOSAVE_SLOT, Path(old_game).name)
            else:
                log.warning("auto-save: xdotool failed for slot %d — continuing anyway", AUTOSAVE_SLOT)
        finally:
            with _session_lock:
                _session["save_in_progress"] = False

    _kill_dolphin()
    _patch_ini(fullscreen=bool(rom_path))
    _wait_dolphin_gone()
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        # started_at marks when a *game* session began; the idle dashboard has
        # no session start.
        _session["started_at"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if rom_path else None
        )
        # Dashboard relaunches keep the previous baseline so an end-of-session
        # save pull still sees the files the last game wrote.
        if rom_path:
            _session["save_baseline"] = time.time()
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


def _sstate_dir() -> Path | None:
    """Return Dolphin's StateSaves dir, honouring an SSTATE_DIR override."""
    env = os.environ.get("SSTATE_DIR")
    if env:
        return Path(env)
    for c in _SSTATE_DIR_CANDIDATES:
        if c.is_dir():
            return c
    return None


def _sstate_snapshot(state_dir: Path) -> dict:
    """Return {Path: (size, mtime)} for every file currently in state_dir."""
    snap = {}
    try:
        for p in state_dir.iterdir():
            try:
                st = p.stat()
                snap[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return snap


def _wait_for_sstate_write(state_dir: Path, before: dict, deadline: float) -> bool:
    """Poll state_dir until a savestate write completes or deadline passes.

    Detects new files and overwrites (mtime change), then waits for the size
    to be stable for 0.5 s. Killing Dolphin on a fixed timer instead of write
    confirmation truncates large states mid-flush.
    """
    STABLE_SECS = 0.5
    POLL_SECS   = 0.1
    target = last_size = stable_since = None

    while time.monotonic() < deadline:
        after = _sstate_snapshot(state_dir)
        if target is None:
            for p, (size, mtime) in after.items():
                prev = before.get(p)
                if prev is None or prev[1] != mtime:
                    target, last_size, stable_since = p, size, time.monotonic()
                    log.debug("Save: write detected — %s (%d bytes)", p.name, size)
                    break
        else:
            cur = after.get(target)
            if cur is None:
                target = None
            elif cur[0] != last_size:
                last_size, stable_since = cur[0], time.monotonic()
            elif time.monotonic() - stable_since >= STABLE_SECS:
                log.info("Save: state write complete — %s (%d bytes)", target.name, last_size)
                return True
        time.sleep(POLL_SECS)
    return False


# ── State file transfer ───────────────────────────────────────────────────────
# RomM's backend syncs save states through these helpers: after a save it
# GETs the freshly written slot file, and on session claim it PUTs the user's
# stored states back into StateSaves. GET blocks while a save is in flight so
# the response always carries the completed write.

STATE_FILE_MAX_BYTES = 256 * 1024 * 1024
STATE_GET_WAIT = float(os.environ.get("STATE_GET_WAIT", "30.0"))


def _wait_for_save_idle(deadline: float) -> None:
    """Block until no save is in flight, or the deadline passes."""
    while time.monotonic() < deadline:
        with _session_lock:
            if not _session["save_in_progress"]:
                return
        time.sleep(0.2)


def _newest_state_for_slot(slot: int) -> Path | None:
    """Newest state file for the slot. Dolphin names states `<GameID>.s{slot:02d}`."""
    state_dir = _sstate_dir()
    if state_dir is None or not state_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in state_dir.glob(f"*.s{slot:02d}"):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates)[1]


def _xdotool_save_state(slot: int) -> bool:
    """Save emulator state to slot (1–8) via Shift+F{slot}.

    Dolphin maps Shift+F1–Shift+F8 directly to save slots 1–8, so no slot
    cycling is needed. Sends the key then polls StateSaves for the write to
    complete (up to SSTATE_WAIT seconds) so a follow-up kill cannot land
    mid-flush. Falls back to a fixed 3 s sleep if StateSaves can't be found.
    """
    wid = _xdotool_find_window()
    if wid is None:
        return False

    state_dir = _sstate_dir()
    before = _sstate_snapshot(state_dir) if state_dir else {}

    if not _xdotool_key(wid, f"shift+F{slot}"):
        return False

    if state_dir is None:
        log.warning("Save: StateSaves dir not found — falling back to fixed 3.0s wait")
        time.sleep(3.0)
        return True

    log.info("xdotool: shift+F%d sent to window %s — waiting for write (max %.1fs)", slot, wid, SSTATE_WAIT)
    if not _wait_for_sstate_write(state_dir, before, time.monotonic() + SSTATE_WAIT):
        log.warning("Save: state write not confirmed within %.1fs (key was sent)", SSTATE_WAIT)
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


def _deferred_load_state(slot: int) -> None:
    """Resume-from-state: send the load key once the launched game is up.

    Waits for launch_in_progress to clear first — the old instance is dead and
    gone by then, so any window found afterwards belongs to the new game. The
    settle delay lets the game boot far enough to accept the hotkey.
    """
    deadline = time.monotonic() + RESUME_LOAD_WAIT
    while time.monotonic() < deadline:
        with _session_lock:
            launching = _session["launch_in_progress"]
            running = _session["rom_path"] is not None
        if not launching and running and _xdotool_find_window() is not None:
            time.sleep(RESUME_LOAD_SETTLE)
            ok = _xdotool_load_state(slot)
            log.info("resume: deferred load of slot %d %s", slot, "delivered" if ok else "failed")
            return
        time.sleep(1.0)
    log.warning("resume: game window never appeared — slot %d not loaded", slot)


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
    """Run pactl as abc so it connects to abc's PulseAudio instance.

    A hung or missing pactl is reported as a non-zero CompletedProcess (rather
    than raising) so the /volume and /mute handlers return a 500 instead of
    dropping the connection with an unhandled exception."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        log.error("pactl timed out: %s", " ".join(args))
        return subprocess.CompletedProcess(cmd, 124, "", "pactl timed out")
    except OSError as exc:
        log.error("pactl failed to run: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


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


# ── In-game save sync ─────────────────────────────────────────────────────────
# Alongside the savestate sync above, RomM syncs Dolphin's own in-game saves:
# GC memory-card folders (GCI) and Wii NAND title saves. GET /save-file zips
# every save file modified since the last game launch; PUT /save-file restores
# a pulled archive before launch. Same guarded, mtime-last-write-wins mechanics
# as Eden's implementation.
_SAVE_DATA_ROOTS = (
    Path("/config/.local/share/dolphin-emu"),
    Path("/config/.config/dolphin-emu"),
)
# Archive members must live under one of these root-relative subtrees; PUT
# rejects anything else so a crafted zip cannot reach configs, keys or states.
SAVE_SYNC_SUBTREES = ("GC", "Wii/title")
SAVE_FILE_MAX_BYTES = 256 * 1024 * 1024
# Zip stores mtimes at 2 s DOS resolution; the slack keeps the newer-file
# guard from skipping files over rounding alone.
_SAVE_MTIME_SLACK = 2.0


def _save_data_root() -> Path | None:
    """Return Dolphin's data dir, honouring a SAVE_DATA_ROOT override."""
    env = os.environ.get("SAVE_DATA_ROOT")
    if env:
        return Path(env)
    for c in _SAVE_DATA_ROOTS:
        if c.is_dir():
            return c
    return None


def _iter_save_files(root: Path) -> list[Path]:
    """Every regular file under the allowed save subtrees, sorted for a
    deterministic archive (identical content zips to identical bytes)."""
    files: list[Path] = []
    for sub in SAVE_SYNC_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(
            p for p in sorted(base.rglob("*")) if p.is_file() and not p.is_symlink()
        )
    return files


def _build_save_archive(baseline: float) -> bytes | None:
    """Zip every save file modified since the last game launch.

    Returns None when there is nothing to sync — no data dir yet, or no file
    changed since `baseline`. Member paths are relative to the data dir so a
    later PUT restores them regardless of which candidate dir is live."""
    root = _save_data_root()
    if root is None:
        log.debug("save-file: no Dolphin data dir found")
        return None
    changed: list[Path] = []
    total = 0
    for p in _iter_save_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= baseline:
            changed.append(p)
            total += st.st_size
    if not changed:
        return None
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("save-file: changed saves exceed size limit (%d bytes)", total)
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            try:
                zf.write(p, p.relative_to(root).as_posix())
            except OSError as exc:
                log.warning("save-file: could not read %s — %s", p, exc)
    return buf.getvalue()


def _mkdirs_owned(path: Path) -> None:
    """mkdir -p with abc ownership on every directory this call creates, so
    Dolphin (running as abc) can keep writing saves inside them later."""
    missing: list[Path] = []
    cur = path
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in reversed(missing):
        try:
            os.chown(d, _LOG_UID, _LOG_GID)
        except OSError:
            pass


def _extract_save_archive(content: bytes) -> tuple[int, int] | str:
    """Restore a pulled save archive into the data dir.

    Returns (written, skipped) on success, or an error string for a bad
    archive. Existing files newer than their archive member are skipped so a
    restore can never roll back saves made since the archive was taken."""
    root = _save_data_root() or _SAVE_DATA_ROOTS[0]
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                return f"archive member escapes save dir: {info.filename}"
            if not any(
                member.as_posix().startswith(sub + "/") for sub in SAVE_SYNC_SUBTREES
            ):
                return f"archive member outside save subtrees: {info.filename}"

        written = skipped = 0
        for info in infos:
            target = root / PurePosixPath(info.filename)
            mtime = time.mktime(info.date_time + (0, 0, -1))
            try:
                if (
                    target.exists()
                    and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK
                ):
                    skipped += 1
                    continue
                _mkdirs_owned(target.parent)
                tmp = target.parent / f".{target.name}.tmp"
                tmp.write_bytes(zf.read(info))
                os.chown(tmp, _LOG_UID, _LOG_GID)
                os.replace(tmp, target)
                os.utime(target, (mtime, mtime))
            except OSError as exc:
                return f"could not write {info.filename}: {exc}"
            written += 1
    return (written, skipped)


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
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", 0)), 64 * 1024))
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _get_state_file(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            slot = int(query.get("slot", ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "slot must be an integer"})
            return
        if slot == 0:
            slot = SAVE_SLOT
        if not (1 <= slot <= 8):
            self._send_json(400, {"error": "slot must be 0–8"})
            return

        # Block while a save is being written so the caller never receives a
        # half-written or stale file right after triggering a save.
        _wait_for_save_idle(time.monotonic() + STATE_GET_WAIT)

        state_path = _newest_state_for_slot(slot)
        if state_path is None:
            self._send_json(404, {"error": "no state file for slot", "slot": slot})
            return
        try:
            content = state_path.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"could not read state file: {exc}"})
            return
        if len(content) > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-State-Filename", state_path.name)
        self.end_headers()
        self.wfile.write(content)
        log.info("state-file: served %s (%d bytes)", state_path.name, len(content))

    def _get_save_file(self):
        with _session_lock:
            baseline = _session["save_baseline"]
            rom_name = _session["rom_name"]
        if baseline is None:
            self._send_json(404, {"error": "no game has been launched"})
            return
        archive = _build_save_archive(baseline)
        if archive is None:
            self._send_json(404, {"error": "no save changes since last launch"})
            return
        # Header values must be latin-1; ROM stems can be anything.
        safe_name = "".join(
            c for c in (rom_name or "dolphin") if c.isascii() and c.isprintable()
        ).strip() or "dolphin"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(archive)))
        self.send_header("X-Save-Filename", f"{safe_name}.saves.zip")
        self.end_headers()
        self.wfile.write(archive)
        log.info("save-file: served archive (%d bytes)", len(archive))

    def _put_save_file(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or empty body"})
            return
        if length > SAVE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "archive too large"})
            return
        content = self.rfile.read(length)
        result = _extract_save_archive(content)
        if isinstance(result, str):
            self._send_json(400, {"error": result})
            return
        written, skipped = result
        log.info("save-file: restored archive — %d written, %d skipped", written, skipped)
        self._send_json(200, {"status": "ok", "written": written, "skipped": skipped})

    def do_PUT(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/save-file":
            self._put_save_file()
            return
        if parsed.path != "/state-file":
            self._send_json(404, {"error": "not found"})
            return

        # Basename only — the filename came from a previous GET and is written
        # back verbatim so Dolphin recognises the slot; path parts are rejected.
        raw_name = parse_qs(parsed.query).get("filename", [""])[0]
        filename = Path(raw_name).name
        stem, _, ext = filename.rpartition(".")
        if (
            not stem
            or stem.startswith(".")
            or len(ext) != 3
            or ext[0] != "s"
            or not ext[1:].isdigit()
        ):
            self._send_json(400, {"error": "filename must be a <GameID>.sNN basename"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or invalid Content-Length"})
            return
        if length > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._send_json(400, {"error": "truncated request body"})
            return

        state_dir = _sstate_dir() or _SSTATE_DIR_CANDIDATES[0]
        tmp = state_dir / f".{filename}.tmp"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(content)
            # Dolphin runs as abc and must be able to overwrite the slot later.
            os.chown(tmp, _LOG_UID, _LOG_GID)
            os.replace(tmp, state_dir / filename)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            self._send_json(500, {"error": f"could not write state file: {exc}"})
            return
        log.info("state-file: stored %s (%d bytes)", filename, length)
        self._send_json(200, {"status": "ok", "filename": filename})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif not self._check_secret():
            # /health stays open for container healthchecks; all other GETs
            # require the shared secret, matching POST/DELETE.
            self._send_json(403, {"error": "forbidden"})
        elif urlparse(self.path).path == "/state-file":
            self._get_state_file()
        elif urlparse(self.path).path == "/save-file":
            self._get_save_file()
        elif self.path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                snap = dict(_session) if active else {}
            self._send_json(200, {
                "active":        active,
                "rom_path":      snap.get("rom_path")   if active else None,
                "rom_name":      snap.get("rom_name")   if active else None,
                "started_at":    snap.get("started_at") if active else None,
                "autosave_slot": AUTOSAVE_SLOT,
                "save_slot":     SAVE_SLOT,
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
            slot = body.get("slot", AUTOSAVE_SLOT)
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
            if not isinstance(slot, int) or not (1 <= slot <= 7):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–7"})
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

        # Resume-from-state: load this slot once the game window is up.
        load_slot = body.get("load_slot")
        if load_slot is not None and (
            not isinstance(load_slot, int) or not (1 <= load_slot <= 8)
        ):
            self._send_json(400, {"error": "load_slot must be 1–8"})
            return

        with _session_lock:
            _session["launch_in_progress"] = True
        Thread(target=_launch_dolphin, args=(str(rom_path),), daemon=True).start()
        if load_slot is not None:
            Thread(
                target=_deferred_load_state, args=(load_slot,), daemon=True
            ).start()
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


# ── Main ──────────────────────────────────────────────────────────────────────

def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """Stop the HTTP listener, let any in-flight save finish, then kill Dolphin.
    Triggered on SIGTERM/SIGINT — serve_forever()'s KeyboardInterrupt path does
    not cover SIGTERM from s6/systemd, which previously hard-killed Dolphin
    mid-savestate on container stop."""
    log.info("Received signal %d — beginning graceful shutdown", signum)
    Thread(target=server.shutdown, daemon=True).start()

    deadline = time.monotonic() + max(SSTATE_WAIT, 5.0)
    while time.monotonic() < deadline:
        with _session_lock:
            if not _session["save_in_progress"]:
                break
        time.sleep(0.2)
    else:
        log.warning("Shutdown: in-flight save did not complete within %.1fs — killing Dolphin anyway", SSTATE_WAIT)

    _kill_dolphin()
    log.info("Shutdown complete")


def main():
    log.info("Broker starting — waiting 5s for desktop...")
    if not SECRET:
        log.warning("BROKER_SECRET is not set — all POST/DELETE endpoints are unauthenticated")
    time.sleep(5)

    # Kill any stale Dolphin instance left from a previous broker run.
    # -x matches the process name exactly; -f with a path substring would also
    # match unrelated processes whose command line mentions the binary.
    result = subprocess.run(["pkill", "-9", "-x", "dolphin-emu"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale dolphin-emu instance(s) on startup.")
        _wait_dolphin_gone()

    _patch_ini()

    # Clean up any leftover selkies socket files from a previous container run.
    # Only done once at startup — not on game launches, where webrtc_input is
    # already running and manages its own socket lifecycle.
    _cleanup_stale_sockets()

    # Launch Dolphin into its main menu so the stream shows something useful
    # whenever no game is playing.  Game launches kill this instance first.
    Thread(target=_launch_dolphin, args=(None,), daemon=True).start()

    # ThreadingHTTPServer: /save-and-exit with wait=true runs xdotool plus the
    # savestate write poll inline; a single-threaded server would stall /health
    # and /status for the duration. Session state is lock-protected.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    def _handle(signum, _frame):
        _graceful_shutdown(server, signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

# dolphin-romm-integration-mod

A [linuxserver Docker mod](https://www.linuxserver.io/blog/2019-09-14-customizing-our-containers) for [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) that adds [RomM](https://github.com/rommapp/romm) streaming support.

An HTTP broker runs inside the container and manages the Dolphin lifecycle: game launches, save/load state, volume control, and returning to dashboard when a session ends. The display streams back to the RomM player via the container's built-in WebRTC/selkies setup.

---

## How It Works

An S6 service (`svc-broker`) runs `broker.py` as root inside the container. The broker:

1. Kills any stale Dolphin process on startup, then launches Dolphin in dashboard mode.
2. Accepts HTTP requests from the RomM backend to launch ROMs, save/load state, set volume, and stop sessions.
3. Auto-saves to the reserved auto-save slot whenever a game is exited or switched.
4. Monitors the Dolphin process and relaunches it into dashboard mode if it exits unexpectedly.

---

## Quick Start

```yaml
services:
  dolphin:
    image: lscr.io/linuxserver/dolphin:latest
    environment:
      - PUID=1000
      - PGID=1000
      - DOCKER_MODS=ghcr.io/loneangelfayt/dolphin-romm-integration-mod:latest
      - ROM_ROOT=/romm/library
      - BROKER_PORT=8000          # optional, default 8000
      - BROKER_SECRET=            # optional shared secret
      - SSTATE_WAIT=3.0           # optional, seconds to wait after save key
      - BROKER_LOG_LEVEL=INFO     # DEBUG for verbose output
    ports:
      - 3000:3000   # WebRTC stream
      - 3001:3001   # HTTPS stream
      - 8000:8000   # Broker API
    volumes:
      - /path/to/romm/library:/romm/library:ro
      - dolphin-config:/config
```

---

## RomM Configuration

In your RomM `config.yml`, enable streaming for GameCube and/or Wii:

```yaml
streaming:
  enabled: true
  containers:
    - platform: ngc
      host: http://<dolphin-host>:3001
      label: Dolphin
      memory_card_sync: true
    - platform: wii
      host: http://<dolphin-host>:3001
      label: Dolphin
```

---

## Broker API

All `POST`/`DELETE` endpoints require `X-Broker-Secret` header if `BROKER_SECRET` is set.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `GET` | `/status` | Current session info including slot config |
| `POST` | `/launch` | Launch a ROM (`{"rom_path": "..."}`). Optional `"load_slot": N` (1–8) resumes from that state slot: once the new game window is up (up to `RESUME_LOAD_WAIT`, plus a `RESUME_LOAD_SETTLE` grace) the broker sends the load hotkey. Push the state file via `PUT /state-file` before launching. |
| `DELETE` | `/launch` | Return to Dolphin dashboard |
| `POST` | `/save-and-exit` | Save to auto-save slot then stop game (`{"slot": 8, "wait": true}`) |
| `POST` | `/save-state` | Save to a user slot in background (`{"slot": 1}`) |
| `POST` | `/load-state` | Load from a slot (`{"slot": 1}`) |
| `GET` | `/state-file?slot=N` | Newest state file for slot N as raw bytes; the filename is echoed in the `X-State-Filename` header. Blocks while a save is in flight (up to `STATE_GET_WAIT`) so a GET after `/save-state` carries the finished write. `slot=0` resolves to `SAVE_SLOT`; returns `404` if the slot has no state. |
| `PUT` | `/state-file?filename=NAME` | Write raw body into StateSaves as `NAME` (used by RomM to hydrate a claimed container). `NAME` must be a bare `<GameID>.sNN` basename; write is atomic and chowned to `abc`. Max 256 MB. |
| `GET` | `/state-screenshot?slot=N` | PNG frame captured at the moment slot N was saved, used by RomM as the state's thumbnail. `404` if the slot has no capture. |
| `GET` | `/save-file` | Zip of every in-game save (GC cards, Wii NAND titles) modified since the last game launch; `404` when nothing changed. |
| `PUT` | `/save-file` | Restore a pulled save archive. Files newer in the container than in the archive are skipped. Max 256 MB. |
| `GET` | `/memory-card` | Zip of the whole Slot-A GCI folder card, member paths relative to the card root. `404` with `X-Memory-Card: absent` when no card exists yet. |
| `PUT` | `/memory-card` | Wipe Slot A and lay down the card in the body. Staged then swapped, so a failure never leaves a half-wiped card. Max 256 MB. |
| `POST` | `/volume` | Set PulseAudio volume (`{"level": 80}`) |
| `POST` | `/mute` | Mute/unmute (`{"mute": true}` or `{}` to toggle) |
| `POST` | `/cleanup` | Restart selkies to flush stale gamepad connections |

### Memory Cards

Slot A is pinned to a GCI **folder** card at a fixed path (`GCIFolderAPathOverride`
in `Dolphin.ini`), which the broker sets on every launch. Dolphin's default GCI
folder sits under a region directory it picks from the booted game
(`GC/USA/Card A`), which the broker cannot know before launch; the override is
used verbatim, with no region or card-name parts appended, so there is one
stable card directory per container.

That is what lets RomM treat the card as a single per-user image: `GET
/memory-card` evacuates it at release, `PUT /memory-card` lays the next user's
card down at claim. Slot B is never touched. Enable it per platform in RomM
with `memory_card_sync: true` on the `ngc` container entry (Wii saves live in
NAND, not on a card, so the `wii` entry keeps the `/save-file` path).

### State Screenshots

Dolphin savestates carry no embedded frame, so the broker takes one: the
screenshot hotkey (`F9`) fires just before the save key and the PNG Dolphin
writes to `ScreenShots` is filed under the slot for `GET /state-screenshot`.
Firing before the save keeps the "Saved State to Slot N" banner out of the
frame. It is best effort throughout: a failed capture never fails the save.

### Save State Slots

Dolphin supports 8 save state slots. **Slot 8 is reserved exclusively for auto-saves** and is not shown in the RomM slot selector.

| Action | User slots | Auto-save slot | Hotkey |
|--------|-----------|---------------|--------|
| Save | 1–7 | 8 (auto only) | `Shift+F1` – `Shift+F8` |
| Load | 1–7 | 8 (load autosave button) | `F1` – `F8` |

**Auto-save behaviour:** slot 8 is written automatically whenever you navigate away from a game (switch titles or click save-and-exit). The "load autosave" button in the RomM player always loads slot 8.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROKER_PORT` | `8000` | HTTP port the broker listens on |
| `BROKER_SECRET` | _(empty)_ | Shared secret for request auth (`X-Broker-Secret` header) |
| `ROM_ROOT` | `/romm/library` | ROM files must be within this path |
| `SSTATE_WAIT` | `3.0` | Seconds to wait after save key before killing Dolphin |
| `STATE_GET_WAIT` | `30.0` | Max seconds `GET /state-file` blocks waiting for an in-flight save to finish |
| `RESUME_LOAD_WAIT` | `90.0` | Max seconds a `load_slot` launch waits for the new game window before giving up on the deferred state load |
| `RESUME_LOAD_SETTLE` | `8.0` | Seconds to let the game boot after its window appears, before the deferred `load_slot` hotkey fires |
| `SCREENSHOT_WAIT` | `5.0` | Max seconds to wait for the screenshot hotkey to produce a PNG before giving up on the state thumbnail |
| `GCI_CARD_DIR` | _(derived)_ | Slot-A GCI folder card path; defaults to `romm/Card A` under Dolphin's data dir |
| `BROKER_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

---

## Controller Setup

The mod uses the [selkies joystick interposer](https://github.com/selkies-project/selkies-gstreamer) to forward browser gamepad input into the container as virtual Xbox 360 pads (`SDL/0–3/Microsoft X-Box 360 pad`).

### Default mapping

All four GCPad ports are pre-mapped to the selkies virtual device on first launch. The mapping is seeded from `/defaults/GCPadNew.ini` only if no existing config is present, so your customisations are never overwritten.

### Calibration

The selkies virtual controller uses a **circular gate** (values come from the browser Gamepad API which clamps to a unit circle). Dolphin's default calibration assumes a square gate and sets diagonal range to `141.42`, which causes the stick to appear short on NW/NE/SW/SE axes.

**Fix:** all 8 calibration points should be `100.00`. Edit `GCPadNew.ini` directly on the host volume:

```
Main Stick/Calibration = 100.00 100.00 100.00 100.00 100.00 100.00 100.00 100.00
C-Stick/Calibration   = 100.00 100.00 100.00 100.00 100.00 100.00 100.00 100.00
```

The file is at `<your-config-volume>/.config/dolphin-emu/GCPadNew.ini`. Restart the container after editing.

### Persisting controller config

Controller mapping and calibration are stored in the `/config` volume and survive game switches. Dolphin does **not** auto-save controller settings on exit — you must click the **Close** button (not just OK) in Dolphin's controller settings dialog to write changes to disk.

---

## Display

The mod forces the following rendering configuration for correct selkies capture:

| Setting | Value | Reason |
|---------|-------|--------|
| `GFXBackend` | `OpenGL` | Vulkan activates Wayland WSI when `WAYLAND_DISPLAY` is set, bypassing X11 |
| `RenderToMain` | `False` | `True` creates an unmapped render window in this Dolphin build |
| `QT_QPA_PLATFORM` | `xcb` | Forces Qt to use XCB; without this Qt falls back to a broken Wayland path |
| `WAYLAND_DISPLAY` | _(unset)_ | Must not be set; causes Dolphin to render directly to Wayland, leaving X11 black |
| `Fullscreen` | `True` (game) / `False` (dashboard) | Prevents black screen on idle boot |

These are applied by the broker on every Dolphin launch and cannot be overridden via Dolphin's GUI.

---

## Troubleshooting

**Game launches but screen is black**
Dolphin may take a few seconds to initialise. If black screen persists, check that `WAYLAND_DISPLAY` is not set in your container environment and that no other mod is injecting a fake libudev.

**Save state doesn't work**
The broker sends xdotool keystrokes to the Dolphin window. If save/load appears to do nothing, check the broker logs for xdotool errors (`docker logs <container> | grep xdotool`). The game must be fully loaded before state operations work.

**Stick doesn't reach full range on diagonals**
See the [Calibration](#calibration) section above. The default `141.42` diagonal values must be changed to `100.00`.

**Volume controls have no effect**
The broker controls PulseAudio sink volume for the `abc` user. Verify PulseAudio is running in the container (`docker exec <container> pactl info`).

**Controller input stops working after game switch**
This is prevented by `BackgroundInput = True` in `Dolphin.ini` (set automatically by the broker). If input drops, check the broker log for socket cleanup warnings.

**Nothing reaches Dolphin — neither broker hotkeys nor browser gamepad**
Both transports die together when Dolphin's global input gate shuts, so treat
simultaneous failure as one bug, not two. The gate is fed by two settings whose
Dolphin sections differ and whose defaults both demand render-window focus —
focus the window never gets, since Dolphin is an Xwayland client under labwc:

| Setting | Section | Default | Broker sets |
|---|---|---|---|
| `HotkeysRequireFocus` | `[General]` | `True` | `False` |
| `BackgroundInput` | `[Input]` | `False` | `True` |

Note `BackgroundInput` lives under `[Input]`, **not** `[General]`. Placed in the
wrong section it is silently ignored and the gate stays shut. Verify with
`grep -A1 '\[Input\]' Dolphin.ini`; the emulator must be restarted to reload it.

# dolphin-romm-integration-mod

A [linuxserver Docker mod](https://www.linuxserver.io/blog/2019-09-14-customizing-our-containers) for [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) that adds [RomM](https://github.com/rommapp/romm) streaming support.

An HTTP broker runs inside the container and manages the Dolphin lifecycle: game launches, save/load state, volume control, and returning to dashboard when a session ends. The display streams back to the RomM player via the container's built-in WebRTC/selkies setup.

---

## How It Works

An S6 service (`svc-broker`) runs `broker.py` as root inside the container. The broker:

1. Kills any stale Dolphin process on startup, then launches Dolphin in dashboard mode.
2. Accepts HTTP requests from the RomM backend to launch ROMs, save/load state, set volume, and stop sessions.
3. Auto-saves to `SAVE_SLOT` whenever a game is exited or switched.
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
| `POST` | `/launch` | Launch a ROM (`{"rom_path": "..."}`, a file or a game folder, see [ROM path resolution](#rom-path-resolution)). Optional `"load_slot": N` (`0` or omitted resolves to `SAVE_SLOT`; `1`-`8` addressable) resumes from that state slot: the broker resolves the slot to its state file and passes it to Dolphin as `--save_state`, so the state is applied during boot, before emulation starts, so the game is never seen running un-resumed. Returns `404` if the slot has no state file. Push the state file via `PUT /state-file` before launching. |
| `DELETE` | `/launch` | Return to Dolphin dashboard |
| `POST` | `/save-and-exit` | Save then stop game (`{"slot": N, "wait": true}`, `0` or omitted = `SAVE_SLOT`) |
| `POST` | `/save-state` | Save state in background (`{"slot": N}`, `0` or omitted = `SAVE_SLOT`) |
| `POST` | `/load-state` | Load a state (`{"slot": N}`, `0` or omitted = `SAVE_SLOT`). `409` while a save is in flight, so a load never races the write it would overwrite. |
| `GET` | `/state-file?slot=N` | Newest state file for slot N as raw bytes; the filename is echoed in the `X-State-Filename` header. Blocks while a save is in flight (up to `STATE_GET_WAIT`) so a GET after `/save-state` carries the finished write. `slot=0` resolves to `SAVE_SLOT`; returns `404` if the slot has no state, and `409` if the save is still running when `STATE_GET_WAIT` expires (the file on disk is mid-flush and must not be stored as the state). |
| `PUT` | `/state-file?filename=NAME` | Write raw body into StateSaves as `NAME` (used by RomM to hydrate a claimed container). `NAME` must be a bare `<GameID>.sNN` basename with `NN` in `01`-`08`; write is atomic and chowned to `abc`. Max 256 MB. |
| `GET` | `/state-screenshot?slot=N` | PNG frame captured at the moment slot N was saved, used by RomM as the state's thumbnail. `404` if the slot has no capture, `409` if a save is still in flight after `STATE_GET_WAIT`. |
| `GET` | `/save-file` | Zip of every in-game save (GC cards, Wii NAND titles) modified since the last game launch. `404` with `X-Save-File: unchanged` when nothing changed, or `X-Save-File: absent` when no game has been launched. An untagged `404` means the endpoint is missing, not that there is nothing to sync. |
| `PUT` | `/save-file` | Restore a pulled save archive. Files newer in the container than in the archive are skipped. Max 256 MB. |
| `GET` | `/memory-card` | Zip of the whole Slot-A GCI folder card, member paths relative to the card root. `404` with `X-Memory-Card: absent` when no card exists yet. |
| `PUT` | `/memory-card` | Wipe Slot A and lay down the card in the body. Staged then swapped, so a failure never leaves a half-wiped card. Max 256 MB. |
| `POST` | `/volume` | Set PulseAudio volume (`{"level": 80}`) |
| `POST` | `/mute` | Mute/unmute (`{"mute": true}` or `{}` to toggle) |
| `POST` | `/cleanup` | Restart selkies to flush stale gamepad connections |

### ROM path resolution

`rom_path` must exist and be under `ROM_ROOT`. It may be either a file or a
**directory**, for libraries laid out one game per folder
(`roms/gc/Metroid Prime/Metroid Prime.iso`). RomM addresses such a game by its
folder, because `Rom.full_path` is `fs_path/fs_name` and for a multi-file ROM
`fs_name` is the directory, so the broker looks inside for the disc image: the
folder itself first, then one level down for the per-disc subfolders some sets
use. Candidates are ranked by format (`.rvz`, `.iso`, `.gcm`, `.wbfs`, `.wia`,
`.gcz`, `.ciso`, `.tgc`, `.wad`, `.dol`, `.elf`) and then by name, so a
multi-disc set boots disc 1 and a real disc image wins over a homebrew `.dol`
sitting beside it. `.m3u` playlists are skipped, because Dolphin boots the disc
itself and every folder shipping a playlist also ships the discs it points at.
Dot-files are skipped, and a symlink pointing outside `ROM_ROOT` is never
chosen. The resolved file is what `/status` and the response body report.

A directory with nothing bootable inside returns `422` with the accepted
extensions in an `extensions` field, which is a different message from the
`422` for a path that does not exist at all.

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

Dolphin supports 8 save state slots. RomM offers no slot selection, so **all state I/O funnels through a single slot, `SAVE_SLOT` (env, default 8)**, the same shape the pcsx2 broker uses. Splitting manual saves and auto-saves across two slots would only split the library's view of a session across two files.

| Action | Slot | Hotkey |
|--------|------|--------|
| Save | `SAVE_SLOT` (`0` or omitted resolves to it; `1`-`8` addressable) | `Shift+F1` - `Shift+F8` |
| Load | `SAVE_SLOT` (`0` or omitted resolves to it; `1`-`8` addressable) | `F1` - `F8` |

`SAVE_SLOT` is written by manual saves, by save-and-exit, and automatically whenever you navigate away from a game (switch titles or click save-and-exit); every read defaults to it. The explicit `1`-`8` range stays addressable for debugging.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROKER_PORT` | `8000` | HTTP port the broker listens on |
| `BROKER_SECRET` | _(empty)_ | Shared secret for request auth (`X-Broker-Secret` header) |
| `ROM_ROOT` | `/romm/library` | ROM files must be within this path |
| `SAVE_SLOT` | `8` | The one slot every state save and load uses (1-8) |
| `SSTATE_WAIT` | `3.0` | Seconds to wait after save key before killing Dolphin |
| `STATE_GET_WAIT` | `30.0` | Max seconds `GET /state-file` blocks waiting for an in-flight save to finish |
| `BROKER_SPOOL_DIR` | `/config/.romm-broker-spool` | Where uploads are spooled to disk; falls back to the system temp dir if it cannot be created |
| `DISPLAY_WAIT` | `30.0` | Max seconds to wait for the X server socket before starting anyway |
| `SCREENSHOT_WAIT` | `5.0` | Max seconds to wait for the screenshot hotkey to produce a PNG before giving up on the state thumbnail |
| `GCI_CARD_DIR` | _(derived)_ | Slot-A GCI folder card path; defaults to `romm/Card A` under Dolphin's data dir |
| `BROKER_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

---

## Controller Setup

The mod uses the [selkies joystick interposer](https://github.com/selkies-project/selkies-gstreamer) to forward browser gamepad input into the container as virtual Xbox 360 pads (`SDL/0-3/Microsoft X-Box 360 pad`).

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

Controller mapping and calibration are stored in the `/config` volume and survive game switches. Dolphin does **not** auto-save controller settings on exit: you must click the **Close** button (not just OK) in Dolphin's controller settings dialog to write changes to disk.

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

**No sound in the stream**
selkies streams one PulseAudio sink: `output`, captured through `output.monitor`.
The base image creates that sink once, from `svc-selkies`, behind a guard that
waits for PulseAudio's pid file — which PulseAudio writes a few milliseconds
before it accepts connections. When `load-module` lands in that gap it dies with
`Connection refused`, `/dev/shm/audio.lock` is touched anyway, and the sink is
gone for the life of the container: `pactl list short sinks` shows only `input`,
and the log repeats `[pcmflux] ERROR: pa_simple_new() failed: No such entity`.
The broker recreates whichever sink is missing at startup and points the default
sink back at `output`. If you hit this on a container running an older mod:

```sh
docker exec <container> s6-setuidgid abc pactl load-module module-null-sink sink_name=output sink_properties=device.description=output
```

then reconnect the browser tab so selkies restarts its audio pipeline. Run every
`pactl` as `abc`; as root it starts a second PulseAudio server and Dolphin's
`cubeb_stream_init` then fails on the next launch.

**Keys from the browser do not reach Dolphin**
The path is `keydown` → `kd,<keysym>` over the data websocket → selkies →
`inject_key` on the Wayland virtual keyboard → labwc → Xwayland `:0` → Dolphin.
It is testable without a browser, which is the fastest way to split client from
server — replay a keysym on the data websocket (**port 8082**, not the
documented 8081) and watch for it with `xev -id <dolphin window id>`. If the key
lands, the fault is client-side.

Client-side, two things silence the keyboard while the gamepad keeps working:

- A URL hash of `#shared` or `#playerN` makes the client a viewer. Viewers never
  call `attach_context()`, the only place the `keydown`/`keyup` listeners are
  registered. The server logs the role on connect: look for `Role: controller`.
- A superseded session. A second client claiming the same display evicts the
  first (`Killing old client for 'primary'`), and the evicted tab keeps
  rendering its last frame while sending into a closed socket. Reload it.

Combinations the browser never delivers to a page (`Ctrl+W`, `Ctrl+T`, `F12`)
cannot be forwarded by anything. Dolphin's own state hotkeys are unaffected by
all of this: the broker sends F1–F8 and Shift+F1–F8 with xdotool inside the
container, so RomM's save/load buttons work even when no browser is attached.

**Nothing reaches Dolphin, neither broker hotkeys nor browser gamepad**
Both transports die together when Dolphin's global input gate shuts, so treat
simultaneous failure as one bug, not two. The gate is fed by two settings whose
Dolphin sections differ and whose defaults both demand render-window focus:

| Setting | Section | Default | Broker sets |
|---|---|---|---|
| `HotkeysRequireFocus` | `[General]` | `True` | `False` |
| `BackgroundInput` | `[Input]` | `False` | `True` |

Note `BackgroundInput` lives under `[Input]`, **not** `[General]`. Placed in the
wrong section it is silently ignored and the gate stays shut. Verify with
`grep -A1 '\[Input\]' Dolphin.ini`; the emulator must be restarted to reload it.

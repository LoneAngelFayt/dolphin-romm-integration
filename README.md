# dolphin-romm-integration

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) that lets [RomM](https://github.com/rommapp/romm) drive [Dolphin](https://dolphin-emu.org/). Pick a GameCube or Wii game in the RomM web UI, and it boots in the Dolphin container and streams back to your browser.

## What the broker actually is

The mod drops a single Python file into the [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) container and runs it as an s6 service (`svc-broker`). It is a small HTTP server, stdlib only, on port 8000. RomM talks to it; it talks to Dolphin.

Dolphin has no remote control API, so something inside the container has to act as the person at the keyboard. That is the broker:

```
browser ──── selkies (WebRTC video) ────┐
                                        │
RomM backend ──── HTTP ──── broker ──── dolphin-emu
                                │
                                ├── xdotool       hotkeys for save and load state
                                ├── Dolphin.ini   patched before every launch
                                ├── --save_state  resume applied during boot
                                └── pactl         volume and mute
```

Five things are worth knowing before the API makes sense:

**Dolphin is always running.** There is no stopped state. With no game loaded the broker keeps Dolphin alive on its own dashboard, so the stream always shows something. If Dolphin dies the broker relaunches it with exponential backoff (5s doubling to 60s) and gives up after five failures rather than respawning a broken renderer forever.

**Everything goes through xdotool.** Unlike PCSX2's PINE socket, Dolphin offers no IPC, so save and load are synthesised keypresses (`Shift+F1`-`Shift+F8` to save, `F1`-`F8` to load) against the Dolphin window. That makes the input gate load-bearing, and it is the single most common thing to break. See [when nothing reaches Dolphin](#nothing-reaches-dolphin).

**Resuming is different from loading.** `POST /load-state` presses a key at a running game. A `load_slot` on `POST /launch` instead passes `--save_state` on Dolphin's command line, so the state is applied during boot and the game is never briefly visible un-resumed. Two mechanisms, two code paths, same slots.

**One slot, not eight.** Dolphin has eight, but RomM exposes no slot picker, so every save and load funnels through `SAVE_SLOT` (default 8). See [Save slots](#save-slots).

**Two kinds of save.** Savestates are whole-emulator snapshots, handled by `/state-file`. In-game saves are GameCube memory card files and Wii NAND titles, handled by `/save-file` and `/memory-card`. RomM syncs them separately, and the memory card needs [a fixed folder card](#memory-cards).

## Quick start

```yaml
services:
  dolphin:
    image: lscr.io/linuxserver/dolphin:latest
    environment:
      - PUID=1000
      - PGID=1000
      - DOCKER_MODS=ghcr.io/loneangelfayt/dolphin-romm-integration-mod:latest
      - ROM_ROOT=/romm/library
      - BROKER_SECRET=your_secret_here
    ports:
      - 3000:3000   # WebRTC stream
      - 3001:3001   # HTTPS stream
      - 8000:8000   # broker API
    volumes:
      - /path/to/romm/library:/romm/library:ro
      - dolphin-config:/config
```

Then enable streaming in RomM's `config.yml`:

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

`memory_card_sync` belongs on the `ngc` entry only. Wii saves live in NAND rather than on a card, so the `wii` entry uses the `/save-file` path instead.

The ROM volume has to be mounted at the same path in both containers. If RomM sees `/romm/library/ngc/game.rvz`, Dolphin must see it there too, or every launch will 422.

## Configuration

`BROKER_SECRET` is the one worth setting. The rest have working defaults.

| Variable | Default | What it does |
|---|---|---|
| `BROKER_SECRET` | *(empty)* | Shared secret, sent as `X-Broker-Secret`. Empty means every request is accepted. |
| `BROKER_PORT` | `8000` | Port the broker listens on. |
| `ROM_ROOT` | `/romm/library` | Where ROMs are mounted. A `rom_path` outside this is rejected. |
| `SAVE_SLOT` | `8` | The one slot every state save and load uses (1-8). |
| `SSTATE_WAIT` | `10.0` | Seconds to wait for a state write to land before killing Dolphin. Falls back to a flat 3s sleep if `StateSaves` doesn't exist yet. |
| `STATE_GET_WAIT` | `30.0` | How long `GET /state-file` waits for an in-flight save before giving up. |
| `SCREENSHOT_WAIT` | `5.0` | How long to wait for the screenshot hotkey to produce a PNG before shipping the state without a thumbnail. |
| `DISPLAY_WAIT` | `30.0` | How long to wait for the X socket at startup before starting anyway. |
| `SSTATE_DIR` | *(probed)* | Savestate directory. Normally found under Dolphin's data dir; override if your build puts it somewhere unusual. |
| `SAVE_DATA_ROOT` | *(probed)* | Dolphin's data dir. Both the XDG (`/config/.local/share/dolphin-emu`) and non-XDG (`/config/.config/dolphin-emu`) layouts are probed, since it varies by build. |
| `GCI_CARD_DIR` | *(derived)* | Slot-A GCI folder card path. Defaults to `romm/Card A` under the data dir. |
| `BROKER_SPOOL_DIR` | `/config/.romm-broker-spool` | Where uploads spool to disk. Falls back to the system temp dir if it can't be created. |
| `DOLPHIN_LOG_PATH` | `/config/dolphin.log` | Captures Dolphin's stdout and stderr. Renderer failures show up here. |
| `BROKER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`. |
| `PUID` / `PGID` | `1000` | Standard LinuxServer UID/GID. Also used to chown files the broker writes for Dolphin, which runs as `abc`. |

## API

Send `X-Broker-Secret` on every request when `BROKER_SECRET` is set. File bodies are capped at 256 MB.

| Endpoint | Does | Notable failures |
|---|---|---|
| `GET /health` | `{"status": "ok"}` if the broker is up | none |
| `GET /status` | Session info plus the configured slot, see [below](#session-state) | none |
| `POST /launch` | Boot a ROM. `{"rom_path": "..."}`, a file or a folder. Optional `load_slot` resumes via `--save_state`, see [below](#launching) | `404` that slot has no state file, `422` path missing or nothing bootable in the folder |
| `DELETE /launch` | Stop the game, back to the dashboard | none |
| `POST /save-and-exit` | Save, then stop. `{"slot": N, "wait": true}` | `409` no game running |
| `POST /save-state` | Save in the background. `{"slot": N}` | `409` no game running or save in progress |
| `POST /load-state` | Load into the running game. `{"slot": N}` | `409` a save is in flight, so a load can't race the write it would overwrite |
| `GET /state-file?slot=N` | Newest state file for the slot, raw bytes. Filename echoed in `X-State-Filename`. Blocks while a save is running, so a GET straight after `/save-state` carries the finished write | `404` no state, `409` still saving after `STATE_GET_WAIT` (the file is mid-flush and must not be stored) |
| `PUT /state-file?filename=NAME` | Write a state file back, atomically, chowned to `abc`. `NAME` must be a bare `<GameID>.sNN` basename with `NN` in `01`-`08` | `400` bad name or truncated body |
| `GET /state-screenshot?slot=N` | PNG captured when the slot was saved, used as RomM's thumbnail. See [below](#state-screenshots) | `404` no capture, `409` still saving |
| `GET /save-file` | Zip of in-game saves (GC cards, Wii NAND titles) changed since the last launch | `404` + `X-Save-File: unchanged` when nothing changed, `X-Save-File: absent` when nothing has launched. An *untagged* `404` means the endpoint is missing, not that there's nothing to sync |
| `PUT /save-file` | Restore a save archive. Files newer in the container are skipped, so a restore can't roll back newer saves | `400` bad archive |
| `GET /memory-card` | The whole Slot-A GCI folder card as one zip, paths relative to the card root | `404` + `X-Memory-Card: absent` when there is no card yet |
| `PUT /memory-card` | Replace Slot A wholesale. Staged then swapped, so a failure never leaves a half-wiped card | `400` bad archive |
| `POST /volume` | `{"level": 80}` | `500` pactl failed |
| `POST /mute` | `{"mute": true}`, or `{}` to toggle | `500` pactl failed |
| `POST /cleanup` | Restart selkies to flush stale gamepad connections. Use it if controllers go dead | none |

Slots are 1-8. `0` or an omitted slot resolves to `SAVE_SLOT` everywhere.

### Session state

```json
{
  "active": true,
  "rom_path": "/romm/library/ngc/game.rvz",
  "rom_name": "game",
  "started_at": "2026-01-01T00:00:00Z",
  "save_slot": 8,
  "autosave_slot": 8
}
```

`active` here means a game is genuinely running: the process is alive *and* a ROM is loaded, so an idle container on the dashboard reports `active: false`. (The PCSX2 broker differs, so don't carry the assumption across.) `rom_path`, `rom_name` and `started_at` are `null` whenever `active` is false. `save_slot` and `autosave_slot` are two names for the same number, kept for older RomM clients.

### Launching

```json
{ "rom_path": "/romm/library/ngc/game.rvz", "load_slot": 8 }
```

`rom_path` may be a **directory**, which matters more than it sounds. RomM addresses a folder-organized game by its folder, because `Rom.full_path` is `fs_path/fs_name` and for a multi-file ROM `fs_name` *is* the directory. So a library laid out one game per folder (`roms/ngc/Metroid Prime/Metroid Prime.iso`) hands the broker a path Dolphin cannot boot. The broker looks inside: the folder itself first, then one level down for the per-disc subfolders some sets use, no deeper. Candidates rank by format (`.rvz`, `.iso`, `.gcm`, `.wbfs`, `.wia`, `.gcz`, `.ciso`, `.tgc`, `.wad`, `.dol`, `.elf`) then by name, so a multi-disc set boots disc 1 and a real disc image beats a homebrew `.dol` sitting beside it. `.m3u` playlists are skipped, since Dolphin boots the disc itself and any folder shipping a playlist also ships the discs it points at. Dot-files are skipped, and a symlink pointing outside `ROM_ROOT` is never chosen.

A folder with nothing bootable inside returns `422` with an `extensions` list, which is a different message from the `422` for a path that doesn't exist. Every broker in this family behaves the same way here.

`load_slot` resumes from a state, and it is not the same mechanism as `/load-state`. The broker resolves the slot to its state file and passes it to Dolphin as `--save_state`, so the state is applied during boot and the game is never seen running un-resumed. Push the state file with `PUT /state-file` *before* launching, or you get a `404`.

### Save slots

Dolphin has eight slots, but RomM offers no slot selection, so **all state I/O funnels through one slot, `SAVE_SLOT` (default 8)**, matching the PCSX2 broker. Splitting manual saves and autosaves across two slots would only split one session's view across two files.

| Action | Hotkey the broker sends |
|---|---|
| Save | `Shift+F1` - `Shift+F8` |
| Load | `F1` - `F8` |

`SAVE_SLOT` is written by manual saves, by save-and-exit, and automatically whenever you navigate away from a game. Every read defaults to it. The explicit `1`-`8` range stays addressable for debugging.

### Memory cards

Slot A is pinned to a GCI **folder** card at a fixed path, via `GCIFolderAPathOverride` in `Dolphin.ini`, which the broker rewrites on every launch. Dolphin's own default puts the GCI folder under a region directory chosen from the booted game (`GC/USA/Card A`), which the broker cannot know *before* launch. The override is used verbatim, with no region or card-name parts appended, so there is exactly one stable card directory per container.

That is what lets RomM treat the card as a single per-user image: `GET /memory-card` evacuates it when a user releases the container, `PUT /memory-card` lays the next user's card down when they claim it. Slot B is never touched.

### State screenshots

Dolphin savestates carry no embedded frame, so the broker takes one: the screenshot hotkey (`F9`) fires just *before* the save key, and the PNG Dolphin drops in `ScreenShots` is filed under the slot for `GET /state-screenshot`. Firing first is deliberate, since it keeps the "Saved State to Slot N" banner out of the picture. The whole path is best effort: a failed capture never fails the save.

### Display settings

The broker forces these on every launch, and they cannot be overridden from Dolphin's GUI. They exist because selkies only captures the X11 display, so anything that could pull Dolphin's output onto a different display path is closed off.

| Setting | Value | Why |
|---|---|---|
| `RenderToMain` | `False` | `True` creates an unmapped render window in this build |
| `QT_QPA_PLATFORM` | `xcb` | Without it Qt takes a broken Wayland path |
| `WAYLAND_DISPLAY` | *(unset)* | Stripped as a precaution so Dolphin stays on the X11 render path selkies captures; the "if set" behavior is unverified |
| `Fullscreen` | `True` in game, `False` on the dashboard | Stops the idle boot going black |

`GFXBackend` is seeded, not forced. The broker writes `OpenGL` into a fresh config, and inserts it if an existing config carries no backend at all, so the first boot always renders on a backend known to be safe across GPUs rather than falling through to whatever default Dolphin picks. After that the choice is yours: pick a backend in Dolphin's Graphics settings and it persists across sessions like any other in-app setting.

Vulkan was tested on the AMD (radeonsi/radv) reference host and renders to the X11 stream fine, identical to OpenGL. It has not been tested on NVIDIA, where GPU selection has its own quirks (see the GLVND note below), so OpenGL stays the seeded default. If a backend you picked ever fails to render, delete the `GFXBackend` line from `[Core]` in `Dolphin.ini` (the broker reseeds `OpenGL`) or set it back to `OpenGL` by hand.

Note that changing `GFXBackend` by editing `Dolphin.ini` while Dolphin is running has no effect: on the next launch the broker quits the live instance cleanly, which flushes its in-memory backend back over your edit before the seed patch runs. Change the backend from Dolphin's own Graphics settings instead, which is what persistence is built around.

### Graphics environment

`sudo` resets the environment before launching Dolphin, so the broker forwards graphics variables through explicitly. Anything the operator sets on the container in the vendor namespaces (`NVIDIA_*`, `VK_*`, `MESA_*`, `LIBGL_*`, `GALLIUM_*`, `RADV_*`, `AMD_*`, `DRI_*`, `LIBVA_*`, `VDPAU_*`, `__GLX_*`, `__NV_*`, `__EGL_*`, `__VK_*`), plus `XDG_DATA_DIRS` and the base image's `DRINODE`, reaches Dolphin. Empty values are dropped rather than forwarded as blanks.

On an NVIDIA host the broker also pins `__GLX_VENDOR_LIBRARY_NAME=nvidia` (and the NVIDIA EGL ICD when its file is present) so GL vendor selection does not depend on udev. The gamepad interposer's fake libudev hides the NVIDIA device from GLVND's udev lookup, which otherwise drops the renderer to Mesa's `llvmpipe`; Mesa-native drivers (AMD/Intel) have a direct render-node fallback and are left alone. A value the operator set themselves always wins.

## Controllers

Browser gamepad input reaches the container through the [selkies joystick interposer](https://github.com/selkies-project/selkies-gstreamer) as virtual Xbox 360 pads (`SDL/0-3/Microsoft X-Box 360 pad`). All four GCPad ports are mapped to it on first launch, seeded from `/defaults/GCPadNew.ini` only when no config exists, so your own mappings are never overwritten.

**Diagonals feel short?** That is a calibration mismatch, not a dead zone. The selkies virtual pad has a *circular* gate, because the browser Gamepad API clamps to a unit circle, while Dolphin's default calibration assumes a square gate and sets the diagonals to `141.42`. Set all eight points to `100.00` in `<config-volume>/.config/dolphin-emu/GCPadNew.ini` and restart the container:

```
Main Stick/Calibration = 100.00 100.00 100.00 100.00 100.00 100.00 100.00 100.00
C-Stick/Calibration    = 100.00 100.00 100.00 100.00 100.00 100.00 100.00 100.00
```

Mapping and calibration live in `/config` and survive game switches. Because the broker quits Dolphin cleanly on teardown, changes you make in its controller dialog are flushed to disk on exit, the same way graphics settings now persist.

## Troubleshooting

### Nothing reaches Dolphin

Neither broker hotkeys nor browser gamepad input works. **Treat simultaneous failure as one bug, not two:** both transports die together when Dolphin's global input gate shuts. The gate is fed by two settings that live in *different* sections and whose defaults both demand render-window focus, which the window never gets, because Dolphin is an Xwayland client under labwc.

| Setting | Section | Dolphin default | Broker sets |
|---|---|---|---|
| `HotkeysRequireFocus` | `[General]` | `True` | `False` |
| `BackgroundInput` | `[Input]` | `False` | `True` |

`BackgroundInput` goes under `[Input]`, **not** `[General]`. In the wrong section it is silently ignored and the gate stays shut. Check with `grep -A1 '\[Input\]' Dolphin.ini`, and restart the emulator to reload it.

### Everything else

**Game launches, screen stays black.** Give Dolphin a few seconds. If it persists, confirm `WAYLAND_DISPLAY` is not set in the container environment and that no other mod is injecting a fake libudev. Check `DOLPHIN_LOG_PATH` for renderer errors.

**Save state does nothing.** The game has to be fully loaded first. Then check for xdotool errors: `docker logs <container> | grep xdotool`. If input is dead generally, read [above](#nothing-reaches-dolphin) first.

**Volume does nothing.** The broker drives the PulseAudio sink for the `abc` user. Confirm PulseAudio is up: `docker exec <container> pactl info`.

**Controller input stops after a game switch.** `BackgroundInput = True` is meant to prevent this and the broker sets it. Check the broker log for socket cleanup warnings, and try `POST /cleanup`.

## Development

The broker is one stdlib Python file, and so is its test suite. Nothing to install, which is the point: the container has no pip, so the broker cannot grow a dependency without breaking the image.

```bash
python3 -m unittest discover -s tests -t tests
```

CI runs the same tests under pytest, plus `ruff check .` against the shared [ruff.toml](ruff.toml). See [tests/README.md](tests/README.md) for what each file covers, from ROM path resolution and ini patching through zip bombs, planted symlinks and interrupted card swaps. Anything needing a real X display or a live `dolphin-emu` process is out of scope on purpose, because it is only provable on a running container.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) and releases are cut automatically on merge to `main`: `fix:` bumps the patch, `feat:` the minor, `feat!:` the major.

## Pinning a version

```yaml
- DOCKER_MODS=ghcr.io/loneangelfayt/dolphin-romm-integration-mod:v1.3.0   # exact
- DOCKER_MODS=ghcr.io/loneangelfayt/dolphin-romm-integration-mod:v1.3     # patches only
- DOCKER_MODS=ghcr.io/loneangelfayt/dolphin-romm-integration-mod:latest   # always newest
```

## Resources

- [Releases and changelog](https://github.com/LoneAngelFayt/dolphin-romm-integration/releases) and the [published images](https://github.com/LoneAngelFayt/dolphin-romm-integration/pkgs/container/dolphin-romm-integration-mod)
- [RomM](https://github.com/rommapp/romm) and its [wiki](https://github.com/rommapp/romm/wiki)
- [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) and [how Docker Mods work](https://docs.linuxserver.io/general/container-customization)
- [Dolphin](https://dolphin-emu.org/) and its [wiki](https://wiki.dolphin-emu.org/index.php?title=Help:Contents)
- [selkies](https://github.com/selkies-project/selkies-gstreamer), which does the WebRTC streaming and the joystick interposer
- Sibling brokers, same shape, different emulators: [PCSX2](https://github.com/LoneAngelFayt/pcsx2-romm-integration), [xemu](https://github.com/LoneAngelFayt/xemu-romm-integration), [RPCS3](https://github.com/LoneAngelFayt/rpcs3-romm-integration), [Eden](https://github.com/LoneAngelFayt/eden-romm-integration)

## License

[GPLv3](LICENSE)

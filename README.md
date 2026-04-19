# dolphin-romm-integration-mod

A [linuxserver Docker mod](https://www.linuxserver.io/blog/2019-09-14-customizing-our-containers) for [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) that integrates [RomM](https://github.com/rommapp/romm) streaming support.

The mod injects a lightweight HTTP broker that manages the Dolphin emulator lifecycle — launching games on demand, saving/loading state, controlling volume, and streaming the display back to the RomM player page via the container's built-in WebRTC stream.

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
    - platform: wii
      host: http://<dolphin-host>:3001
      label: Dolphin
    - platform: wiiu
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
| `POST` | `/launch` | Launch a ROM (`{"rom_path": "..."}`) |
| `DELETE` | `/launch` | Return to Dolphin dashboard |
| `POST` | `/save-and-exit` | Save to auto-save slot then stop game (`{"slot": 8, "wait": true}`) |
| `POST` | `/save-state` | Save to a user slot in background (`{"slot": 1}`) |
| `POST` | `/load-state` | Load from a slot (`{"slot": 1}`) |
| `POST` | `/volume` | Set PulseAudio volume (`{"level": 80}`) |
| `POST` | `/mute` | Mute/unmute (`{"mute": true}` or `{}` to toggle) |
| `POST` | `/cleanup` | Restart selkies to flush stale gamepad connections |

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
| `BROKER_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

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

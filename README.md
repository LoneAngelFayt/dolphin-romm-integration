# dolphin-romm-integration-mod

A [linuxserver Docker mod](https://www.linuxserver.io/blog/2019-09-14-customizing-our-containers) for [linuxserver/dolphin](https://docs.linuxserver.io/images/docker-dolphin/) that integrates [RomM](https://github.com/rommapp/romm) streaming support.

The mod injects a lightweight HTTP broker that manages the Dolphin emulator lifecycle — launching games on demand, saving/loading state, controlling volume, and streaming the display back to the RomM player page via the container's built-in WebRTC stream.

---

## How It Works

An S6 service (`svc-broker`) runs `broker.py` as root inside the container. The broker:

1. Kills any stale Dolphin process on startup, then launches Dolphin in dashboard mode.
2. Accepts HTTP requests from the RomM backend to launch ROMs, save/load state, set volume, and stop sessions.
3. Monitors the Dolphin process and relaunches it into dashboard mode if it exits unexpectedly.

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
      - SAVE_SLOT=1               # default slot for save-and-exit (1–8)
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
    - platform: gc
      host: http://<dolphin-host>:8000
      label: Dolphin
    - platform: wii
      host: http://<dolphin-host>:8000
      label: Dolphin
```

---

## Broker API

All `POST`/`DELETE` endpoints require `X-Broker-Secret` header if `BROKER_SECRET` is set.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `GET` | `/status` | Current session info |
| `POST` | `/launch` | Launch a ROM (`{"rom_path": "..."}`) |
| `DELETE` | `/launch` | Return to Dolphin dashboard |
| `POST` | `/save-and-exit` | Save state then stop game (`{"slot": 1, "wait": true}`) |
| `POST` | `/save-state` | Save to slot in background (`{"slot": 1}`) |
| `POST` | `/load-state` | Load from slot (`{"slot": 1}`) |
| `POST` | `/volume` | Set PulseAudio volume (`{"level": 80}`) |
| `POST` | `/mute` | Mute/unmute (`{"mute": true}` or `{}` to toggle) |
| `POST` | `/cleanup` | Restart selkies to flush stale gamepad connections |

### Save State Slots

Dolphin supports **8 save state slots** (1–8). Each slot maps directly to a hotkey:

| Action | Slots | Hotkey |
|--------|-------|--------|
| Save | 1–8 | `Shift+F1` – `Shift+F8` |
| Load | 1–8 | `F1` – `F8` |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROKER_PORT` | `8000` | HTTP port the broker listens on |
| `BROKER_SECRET` | _(empty)_ | Shared secret for request auth |
| `ROM_ROOT` | `/romm/library` | ROM files must be within this path |
| `SAVE_SLOT` | `1` | Default slot used by `/save-and-exit` |
| `SSTATE_WAIT` | `3.0` | Seconds to wait after save key before killing Dolphin |
| `BROKER_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

---

## Troubleshooting

**Game launches but screen is black**
Dolphin may take a few seconds to initialise. The WebRTC stream continues once the game renders its first frame.

**Save state doesn't load**
Verify the slot number matches the one used when saving. Slots are per-game in Dolphin — a save from one game cannot be loaded in another.

**Volume controls have no effect**
The broker controls PulseAudio sink volume for the `abc` user. Verify PulseAudio is running in the container (`pactl info`).

**xdotool key delivery fails**
The broker targets the Dolphin window by PID. If Dolphin is still initialising when a keypress is sent, the window may not yet be visible. Wait until the game is fully loaded before using save/load state.

#!/usr/bin/with-contenv bash

XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"

# Ensure python3 is available for the broker service.
if ! command -v python3 &>/dev/null; then
    echo "[broker-mod] Installing python3..."
    apt-get update -qq && apt-get install -y -qq python3 \
        || echo "[broker-mod] ERROR: failed to install python3"
fi

# Lock down the sudoers rule so sudo accepts it (requires mode 0440).
chmod 0440 /etc/sudoers.d/broker
echo "[broker-mod] sudoers rule set."

# Disable the labwc autostart so dolphin-emu isn't launched a second time by
# the desktop session — the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by dolphin-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# Dolphin on this image stores all config files directly in
# ~/.config/dolphin-emu/ — there is no Config/ subdirectory.
DOLPHIN_CFG_DIR="/config/.config/dolphin-emu"
mkdir -p "$DOLPHIN_CFG_DIR"

# Pre-seed Dolphin.ini so the broker's INI patch has something to work with
# on the very first launch, before Dolphin has written its own copy.
DOLPHIN_INI="$DOLPHIN_CFG_DIR/Dolphin.ini"
if [ ! -f "$DOLPHIN_INI" ]; then
    cat > "$DOLPHIN_INI" <<'EOF'
[Core]
SIDevice0 = 6
SIDevice1 = 0
SIDevice2 = 0
SIDevice3 = 0
BackgroundInput = True

[Interface]
ConfirmStop = False

[Analytics]
Enabled = False
PermissionAsked = True
EOF
    echo "[broker-mod] Created default Dolphin.ini."
fi

# Copy default controller profile if not already present.  The container ships
# a ready-made GCPadNew.ini in /defaults/ that maps all 4 GCPad ports to
# SDL "Microsoft X-Box 360 pad" — exactly what the selkies joystick interposer
# presents.  Without this file Dolphin has no controller mappings configured.
GCPAD_INI="$DOLPHIN_CFG_DIR/GCPadNew.ini"
if [ ! -f "$GCPAD_INI" ] && [ -f "/defaults/GCPadNew.ini" ]; then
    cp /defaults/GCPadNew.ini "$GCPAD_INI"
    echo "[broker-mod] Copied default GCPadNew.ini (controller mappings)."
fi

# Log kernel input device names so we can verify GCPadNew.ini uses the right
# SDL device name.  Without libudev.so.1.0.0-fake, SDL falls back to sysfs for
# device names — these are the names it will see.
if [ "${BROKER_LOG_LEVEL,,}" = "debug" ]; then
    echo "[broker-mod] Input device names (for GCPadNew.ini SDL mapping):"
    for node in js0 js1 js2 js3; do
        name_file="/sys/class/input/${node}/device/name"
        if [ -f "$name_file" ]; then
            echo "[broker-mod]   /dev/input/${node}: $(cat "$name_file")"
        else
            echo "[broker-mod]   /dev/input/${node}: sysfs name not found"
        fi
    done
fi

# Patch the selkies input_handler.py keep-alive loop to check reader.at_eof().
# Without this, idle gamepad sockets never detect client disconnection because
# asyncio buffers the EOF but writer.is_closing() never flips on Unix sockets.
# Locate selkies input_handler.py — glob over the python version so the patch
# survives base image upgrades that bump e.g. python3.12 → python3.13.
INPUT_HANDLER=$(compgen -G "/lsiopy/lib/python3.*/site-packages/selkies/input_handler.py" | head -1)
INPUT_HANDLER="${INPUT_HANDLER:-/lsiopy/lib/python3.13/site-packages/selkies/input_handler.py}"
if [ -f "$INPUT_HANDLER" ]; then
    # Apply EOF detection patch if not already applied.
    if ! grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
        sed -i \
            's/while self\.running and not writer\.is_closing():/while self.running and not writer.is_closing() and not reader.at_eof():/' \
            "$INPUT_HANDLER" \
            || echo "[broker-mod] ERROR: sed patch failed on input_handler.py"
        echo "[broker-mod] Patched selkies input_handler.py EOF detection."
    fi

    # Silence the selkies_gamepad logger — it emits ~80 INFO lines per launch cycle.
    # Uses python3 for the insertion because sed \n behaviour is not portable across
    # GNU/BSD sed variants and can silently produce a literal '\n' in the file.
    if ! grep -q "setLevel(logging.WARNING)" "$INPUT_HANDLER"; then
        if python3 - "$INPUT_HANDLER" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
old = 'logger_selkies_gamepad = logging.getLogger("selkies_gamepad")'
new = old + '\nlogger_selkies_gamepad.setLevel(logging.WARNING)'
text = p.read_text()
if old in text:
    p.write_text(text.replace(old, new, 1))
    sys.exit(0)
sys.exit(1)
PYEOF
        then
            echo "[broker-mod] Patched selkies_gamepad log level to WARNING."
        else
            echo "[broker-mod] ERROR: python patch failed setting selkies_gamepad log level"
        fi
    fi

else
    echo "[broker-mod] WARNING: selkies input_handler.py not found at $INPUT_HANDLER"
fi

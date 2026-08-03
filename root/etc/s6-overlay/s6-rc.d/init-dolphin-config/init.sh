#!/usr/bin/with-contenv bash

# Every patch below rewrites a file that a base-image service reads once, at
# import or config-load time: selkies' input_handler.py, nginx's site config,
# labwc's autostart. So init-services depends on this oneshot (see
# init-services/dependencies.d/init-dolphin-config), which puts the whole
# service stack behind it. Without that edge s6 starts the services in parallel
# with this script, they win the race by several seconds, and every patch here
# lands on disk having no effect at all on the processes already running: the
# stream gate is absent from the live nginx and the selkies fixes never load.
# Keep this script fast and offline. Slow or networked work belongs in
# init-dolphin-deps, which only the broker waits for.

XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"

# Lock down the sudoers rule so sudo accepts it (requires mode 0440).
chmod 0440 /etc/sudoers.d/broker
echo "[broker-mod] sudoers rule set."

# Disable the labwc autostart so dolphin-emu isn't launched a second time by
# the desktop session: the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by dolphin-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# Dolphin on this image stores all config files directly in
# ~/.config/dolphin-emu/. There is no Config/ subdirectory. Dolphin.ini itself
# is not seeded here: broker.py writes it on startup, before Dolphin launches,
# and a second copy of those defaults drifts out of sync with the broker's.
DOLPHIN_CFG_DIR="/config/.config/dolphin-emu"
mkdir -p "$DOLPHIN_CFG_DIR"

# Copy default controller profile if not already present.  The container ships
# a ready-made GCPadNew.ini in /defaults/ that maps all 4 GCPad ports to
# SDL "Microsoft X-Box 360 pad", exactly what the selkies joystick interposer
# presents.  Without this file Dolphin has no controller mappings configured.
GCPAD_INI="$DOLPHIN_CFG_DIR/GCPadNew.ini"
if [ ! -f "$GCPAD_INI" ] && [ -f "/defaults/GCPadNew.ini" ]; then
    cp /defaults/GCPadNew.ini "$GCPAD_INI"
    echo "[broker-mod] Copied default GCPadNew.ini (controller mappings)."
fi

# Seed the emulated Wiimote profile for Wii titles.  The base image ships no
# WiimoteNew.ini, so Dolphin auto-generates a mouse/keyboard profile that is
# useless over a stream.  This mod's /defaults/WiimoteNew.ini maps an emulated
# Wiimote+Nunchuk to the SDL pad: Nunchuk stick on left stick, IR pointer on
# right stick, Wiimote shake on B, Nunchuk shake on RB.  Replace the existing
# file only while it has never been pointed at an SDL device, so a hand-tuned
# profile survives restarts.
WIIMOTE_INI="$DOLPHIN_CFG_DIR/WiimoteNew.ini"
if [ -f "/defaults/WiimoteNew.ini" ]; then
    if [ ! -f "$WIIMOTE_INI" ] || ! grep -q "Device = SDL/" "$WIIMOTE_INI"; then
        cp /defaults/WiimoteNew.ini "$WIIMOTE_INI"
        echo "[broker-mod] Seeded WiimoteNew.ini (emulated Wiimote+Nunchuk on SDL pad)."
    fi
fi

# Log kernel input device names so we can verify GCPadNew.ini uses the right
# SDL device name.  Without libudev.so.1.0.0-fake, SDL falls back to sysfs for
# device names. These are the names it will see.
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
# Locate selkies input_handler.py, globbing over the python version so the patch
# survives base image upgrades that bump e.g. python3.12 → python3.13.
INPUT_HANDLER=$(compgen -G "/lsiopy/lib/python3.*/site-packages/selkies/input_handler.py" | head -1)
INPUT_HANDLER="${INPUT_HANDLER:-/lsiopy/lib/python3.13/site-packages/selkies/input_handler.py}"
if [ -f "$INPUT_HANDLER" ]; then
    # Apply EOF detection patch if not already applied.
    if ! grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
        sed -i \
            's/while self\.running and not writer\.is_closing():/while self.running and not writer.is_closing() and not reader.at_eof():/' \
            "$INPUT_HANDLER"
        # Verify the substitution actually took: sed exits 0 even when the
        # pattern never matched, so grep is the only honest success signal.
        if grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
            echo "[broker-mod] Patched selkies input_handler.py EOF detection."
        else
            echo "[broker-mod] ERROR: input_handler.py EOF pattern not found, patch NOT applied (upstream may have changed)"
        fi
    fi

    # Silence the selkies_gamepad logger: it emits ~80 INFO lines per launch cycle.
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

# Gate the browser-facing stream with nginx auth_request. Without this, anyone
# who learns the address gets an interactive desktop with the ROM library
# mounted, since RomM's auth never sits on this socket. auth_request sends every
# request to the broker's /verify, which checks the session-bound stream token
# RomM appends to the iframe URL and, on the first (query-token) hit, hands back
# a stream_sid cookie that carries every later asset and the WebSocket upgrade.
# The broker exempts /verify from its shared secret because nginx cannot forward
# that secret and the stream token is the credential.
#
# EVERY server block is gated, not just the 3001 SSL vhost RomM points at. The
# base image ships a second, identical plain-HTTP vhost on 3000 (same /websocket
# proxy to selkies, same /files alias of /config/Desktop), and the README tells
# you to publish it, so gating only one leaves a complete bypass of the gate one
# port number away. Note that stream_sid is a Secure cookie: reaching 3000
# directly over plain HTTP admits the query token but the cookie will not stick,
# so 3000 only works behind a proxy that terminates TLS. That is the supported
# shape; unproxied plain-HTTP 3000 is not, and now fails closed instead of
# serving the desktop.
#
# /50x.html is exempted because it is the error_page target: left gated, a
# broker outage makes each request bounce between the 500 and its own gated
# error page until nginx's internal-redirect limit trips.
NGINX_SITE="/etc/nginx/sites-available/default"
if [ -f "$NGINX_SITE" ]; then
    if grep -q "RomM stream gate" "$NGINX_SITE"; then
        echo "[broker-mod] nginx stream gate already applied."
    else
        awk -v bport="${BROKER_PORT:-8000}" '
          /^[[:space:]]*server[[:space:]]*\{/ {
            print
            print "  # ── RomM stream gate (dolphin-broker-mod) ──"
            print "  auth_request /_stream_auth;"
            print "  auth_request_set $stream_set_cookie $upstream_http_set_cookie;"
            print "  add_header Set-Cookie $stream_set_cookie;"
            print "  add_header Referrer-Policy \"same-origin\" always;"
            print "  location = /_stream_auth {"
            print "    internal;"
            print "    auth_request off;"
            print "    proxy_pass http://127.0.0.1:" bport "/verify;"
            print "    proxy_pass_request_body off;"
            print "    proxy_set_header Content-Length \"\";"
            print "    proxy_set_header X-Original-URI $request_uri;"
            print "  }"
            next
          }
          /^[[:space:]]*location[[:space:]]*=[[:space:]]*\/50x\.html[[:space:]]*\{/ {
            print
            print "    auth_request off;"
            next
          }
          { print }
        ' "$NGINX_SITE" > "$NGINX_SITE.tmp"
        gated=$(grep -c "RomM stream gate" "$NGINX_SITE.tmp" 2>/dev/null)
        [ -n "$gated" ] || gated=0
        if [ "$gated" -gt 0 ]; then
            mv "$NGINX_SITE.tmp" "$NGINX_SITE"
            echo "[broker-mod] Applied nginx stream gate to $gated vhost(s)."
            if [ "$gated" -lt 2 ]; then
                echo "[broker-mod] WARNING: only $gated vhost gated; the base image ships two (3000 and 3001). Any other listener is serving the desktop ungated."
            fi
            if ! nginx -t 2>/dev/null; then
                echo "[broker-mod] WARNING: nginx -t failed now (ssl cert may not exist yet at init); nginx revalidates at start."
            fi
        else
            rm -f "$NGINX_SITE.tmp"
            echo "[broker-mod] ERROR: nginx stream gate not applied (no 'server {' block matched; base image may have changed)."
        fi
    fi
else
    echo "[broker-mod] WARNING: nginx site config not found at $NGINX_SITE"
fi

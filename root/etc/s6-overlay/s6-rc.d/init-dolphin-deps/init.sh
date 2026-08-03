#!/usr/bin/with-contenv bash

# Package fetching, split out of init-dolphin-config because it is slow and
# networked. init-dolphin-config now gates the whole service stack (nginx, xorg
# and selkies all wait on it, so that its nginx and selkies patches land before
# those services read them), and anything that can spend a minute on apt-get
# has to live somewhere that only the broker waits for.

# Ensure python3 is available for the broker service.
if ! command -v python3 &>/dev/null; then
    echo "[broker-mod] Installing python3..."
    apt-get update -qq && apt-get install -y -qq python3 \
        || echo "[broker-mod] ERROR: failed to install python3"
fi

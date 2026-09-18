#!/usr/bin/env bash
# ==============================================================================
# Jetson Chromium Kiosk Launcher for Multi-Stream Surveillance Dashboard
# ==============================================================================
# Launches Chromium in fullscreen kiosk mode on DISPLAY=:0 to measure realistic
# client-side decoding, JavaScript execution, and UI rendering overhead.
# ==============================================================================

set -e

PORT="${1:-8000}"
DASHBOARD_URL="http://localhost:${PORT}"
export DISPLAY="${DISPLAY:-:0}"

echo "=============================================================================="
echo " Starting Jetson Surveillance Frontend Kiosk on $DISPLAY"
echo " Dashboard URL: $DASHBOARD_URL"
echo "=============================================================================="

# Disable screen blanking and power management if X11 xset is available
if command -v xset &> /dev/null; then
    xset s noblank 2>/dev/null || true
    xset s off 2>/dev/null || true
    xset -dpms 2>/dev/null || true
fi

# Detect browser binary
BROWSER_BIN=""
for b in "chromium-browser" "chromium" "google-chrome" "google-chrome-stable" "epiphany-browser" "epiphany" "firefox"; do
    if command -v "$b" &> /dev/null; then
        BROWSER_BIN="$b"
        break
    fi
done

if [ -z "$BROWSER_BIN" ]; then
    echo "[-] No supported browser (Chromium/Epiphany/Firefox) found on this system."
    exit 1
fi

echo "[*] Using browser: $BROWSER_BIN"
echo "[*] Waiting for dashboard server at $DASHBOARD_URL to respond..."

# Wait up to 15 seconds for web server to start responding
for i in {1..15}; do
    if curl -s -o /dev/null -w "%{http_code}" "$DASHBOARD_URL" | grep -q "200"; then
        echo "[✓] Dashboard server is ready."
        break
    fi
    sleep 1
done

# Launch browser with appropriate flags
if [[ "$BROWSER_BIN" =~ epiphany ]]; then
    exec $BROWSER_BIN "$DASHBOARD_URL"
elif [[ "$BROWSER_BIN" =~ firefox ]]; then
    exec $BROWSER_BIN --new-window "$DASHBOARD_URL"
else
    # Launch Chromium with hardware-accelerated rendering and kiosk flags
    exec $BROWSER_BIN \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        --disable-translate \
        --check-for-update-interval=31536000 \
        --autoplay-policy=no-user-gesture-required \
        --enable-features=VaapiVideoDecoder \
        --ignore-gpu-blocklist \
        --enable-gpu-rasterization \
        --enable-zero-copy \
        --window-position=0,0 \
        --window-size=1920,1080 \
        "$DASHBOARD_URL"
fi

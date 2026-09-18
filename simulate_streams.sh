#!/usr/bin/env bash
set -e

PIDS=()

cleanup() {
    echo ""
    echo "[!] Stopping simulated streams and MediaMTX server..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    pkill -f "mediamtx" 2>/dev/null || true
    pkill -f "ffmpeg" 2>/dev/null || true
    echo "[✓] Cleanup complete."
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

MEDIAMTX_BIN="./mediamtx"
if command -v mediamtx &> /dev/null; then
    MEDIAMTX_CMD="mediamtx"
else
    MEDIAMTX_CMD="$MEDIAMTX_BIN"
fi

echo "[*] Starting MediaMTX..."
$MEDIAMTX_CMD > /dev/null 2>&1 &
MEDIAMTX_PID=$!
PIDS+=($MEDIAMTX_PID)
sleep 2

echo "[*] Starting 10 simulated RTSP streams (cam_01 to cam_10)..."

for i in $(seq -w 1 10); do
    # Creates cam_01, cam_02 ... cam_10
    CAM_ID="cam_${i}"
    RTSP_URL="rtsp://localhost:8554/${CAM_ID}"

    # Lightweight 720p 15fps streams to keep CPU free for your benchmark pipeline
    ffmpeg -re -f lavfi -i "testsrc=size=1280x720:rate=15" \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v 800k -pix_fmt yuv420p \
        -f rtsp -rtsp_transport tcp "$RTSP_URL" > /dev/null 2>&1 &

    PIDS+=($!)
    echo "  -> Published: $RTSP_URL"
done

echo "[✓] All 10 streams are active. Keep this terminal open!"
wait

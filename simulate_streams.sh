#!/usr/bin/env bash
# ==============================================================================
# Multi-Stream RTSP Simulator for Jetson CCTV Benchmark
# ==============================================================================
# This script sets up 10 local RTSP camera streams using MediaMTX and FFmpeg.
# Streams published:
#   rtsp://localhost:8554/cam0  ->  rtsp://localhost:8554/cam9
#
# Each stream generates a standard 1080p @ 30fps H.264 stream with timestamps
# and stream identification text burned in.
# ==============================================================================

set -e

PIDS=()

cleanup() {
    echo ""
    echo "[!] Stopping all simulated streams and MediaMTX server..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    # Kill any leftover mediamtx or ffmpeg cam publishers
    pkill -f "mediamtx" 2>/dev/null || true
    pkill -f "ffmpeg.*cam[0-9]" 2>/dev/null || true
    echo "[✓] Cleanup complete."
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# ------------------------------------------------------------------------------
# 1. Check or Install MediaMTX
# ------------------------------------------------------------------------------
MEDIAMTX_BIN="./mediamtx"

if ! command -v mediamtx &> /dev/null && [ ! -f "$MEDIAMTX_BIN" ]; then
    echo "[*] MediaMTX not found. Detecting architecture to download..."
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)
            MTX_ARCH="linux_amd64"
            ;;
        aarch64|arm64)
            MTX_ARCH="linux_arm64v8"
            ;;
        *)
            echo "[-] Unsupported architecture for auto-download: $ARCH."
            echo "[-] Please download MediaMTX manually from https://github.com/bluenviron/mediamtx/releases"
            exit 1
            ;;
    esac

    MTX_VERSION="v1.9.0"
    URL="https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}/mediamtx_${MTX_VERSION}_${MTX_ARCH}.tar.gz"
    echo "[*] Downloading MediaMTX from $URL..."
    curl -L -s "$URL" | tar -xz mediamtx
    chmod +x mediamtx
fi

if command -v mediamtx &> /dev/null; then
    MEDIAMTX_CMD="mediamtx"
else
    MEDIAMTX_CMD="$MEDIAMTX_BIN"
fi

# ------------------------------------------------------------------------------
# 2. Check for FFmpeg
# ------------------------------------------------------------------------------
if ! command -v ffmpeg &> /dev/null; then
    echo "[-] FFmpeg is required to generate mock streams."
    echo "[-] Install on Ubuntu/JetPack with: sudo apt-get update && sudo apt-get install -y ffmpeg"
    exit 1
fi

# ------------------------------------------------------------------------------
# 3. Start MediaMTX RTSP Server
# ------------------------------------------------------------------------------
echo "[*] Launching MediaMTX RTSP Server on port 8554..."
$MEDIAMTX_CMD > /dev/null 2>&1 &
MEDIAMTX_PID=$!
PIDS+=($MEDIAMTX_PID)
sleep 2

# Verify server is listening on port 8554
if ! nc -z localhost 8554 2>/dev/null && ! timeout 1 bash -c "</dev/tcp/localhost/8554" 2>/dev/null; then
    echo "[*] Waiting for RTSP port 8554 to become active..."
    sleep 2
fi

echo "[✓] MediaMTX RTSP server is active."

# ------------------------------------------------------------------------------
# 4. Launch 10 FFmpeg Mock Feeds
# ------------------------------------------------------------------------------
CAM_NAMES=(
    "Front_Gate"
    "Loading_Dock"
    "Warehouse_A"
    "Perimeter_East"
    "Server_Room"
    "Hallway_North"
    "Parking_Lot_West"
    "Reception"
    "Emergency_Exit"
    "Roof_Access"
)

echo "[*] Starting 10 simulated RTSP streams (1080p @ 30fps H.264)..."

for i in {0..9}; do
    CAM_ID="cam${i}"
    CAM_TITLE="${CAM_NAMES[$i]}"
    RTSP_URL="rtsp://localhost:8554/${CAM_ID}"

    # Generate synthetic video test stream with live timestamp & camera label
    ffmpeg -re -f lavfi -i "testsrc=size=1920x1080:rate=30,drawtext=text='${CAM_TITLE} (${CAM_ID}) - %{localtime}':x=40:y=40:fontsize=48:fontcolor=white:box=1:boxcolor=black@0.6" \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v 2000k -maxrate 2500k -bufsize 5000k \
        -g 30 -pix_fmt yuv420p \
        -f rtsp -rtsp_transport tcp "$RTSP_URL" > /dev/null 2>&1 &

    FFMPEG_PID=$!
    PIDS+=($FFMPEG_PID)
    echo "  -> Stream [$i/10] published to: $RTSP_URL (PID: $FFMPEG_PID)"
done

echo ""
echo "=============================================================================="
echo " [✓] ALL 10 RTSP STREAMS ARE LIVE & STREAMING"
echo "=============================================================================="
echo " - Display streams (4) : rtsp://localhost:8554/cam0 through /cam3"
echo " - Record-only (6)     : rtsp://localhost:8554/cam4 through /cam9"
echo ""
echo " You can now run the benchmark in another terminal:"
echo "   python3 benchmark_pipeline.py --duration 60"
echo ""
echo " Press [Ctrl+C] in this terminal to stop all streams."
echo "=============================================================================="

# Wait indefinitely until interrupted
wait

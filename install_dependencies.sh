#!/usr/bin/env bash
# ==============================================================================
# Jetson Multi-Stream Benchmark - Automated Dependency Installer
# ==============================================================================
set -e

echo "=============================================================================="
echo " [*] Installing System Dependencies & GStreamer Plugins..."
echo "=============================================================================="
sudo apt-get update
sudo apt-get install -y \
    python3-pip \
    python3-gi \
    python3-gst-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    chromium-browser \
    ffmpeg \
    curl \
    netcat-openbsd

echo "=============================================================================="
echo " [*] Installing Python Dependencies..."
echo "=============================================================================="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pip3 install -r "${SCRIPT_DIR}/requirements.txt"

echo "=============================================================================="
echo " [*] Configuring & Restarting jtop (jetson-stats) Service..."
echo "=============================================================================="
sudo systemctl restart jtop || true

echo "=============================================================================="
echo " [*] Setting Execution Permissions on Scripts..."
echo "=============================================================================="
chmod +x "${SCRIPT_DIR}/simulate_streams.sh" "${SCRIPT_DIR}/launch_kiosk.sh" "${SCRIPT_DIR}/benchmark_pipeline.py"

echo "=============================================================================="
echo " [✓] All dependencies installed successfully!"
echo "=============================================================================="

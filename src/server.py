"""
FastAPI Server for Jetson Multi-Stream CCTV Benchmark.
Provides WebSocket telemetry broadcasting, low-latency video streaming endpoints,
interactive start/stop logging controls, and static frontend dashboard hosting.
"""

import asyncio
import io
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("benchmark.server")

app = FastAPI(title="Jetson Multi-Stream Surveillance Benchmark")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reference to monitor and config
_monitor_instance: Optional[Any] = None
_camera_configs: List[Dict[str, Any]] = []
_active_websockets: List[WebSocket] = []
_frame_buffers: Dict[str, bytes] = {}


def init_server(monitor: Any, cameras: List[Dict[str, Any]]) -> None:
    """Initialize server state with monitor and camera list."""
    global _monitor_instance, _camera_configs
    _monitor_instance = monitor
    _camera_configs = cameras


def update_camera_frame(cam_id: str, jpeg_bytes: bytes) -> None:
    """Store latest JPEG frame for a given camera stream."""
    _frame_buffers[cam_id] = jpeg_bytes


@app.get("/api/cameras")
async def get_cameras():
    """Return configured cameras."""
    return JSONResponse(content={"cameras": _camera_configs})


@app.get("/api/summary")
async def get_summary():
    """Return aggregated benchmark telemetry summary."""
    if _monitor_instance is not None:
        return JSONResponse(content=_monitor_instance.get_summary())
    return JSONResponse(content={})


@app.get("/api/status")
async def get_status():
    """Return server and telemetry status."""
    is_jetson = getattr(_monitor_instance, "is_jetson", False) if _monitor_instance else False
    is_logging = getattr(_monitor_instance, "is_logging", False) if _monitor_instance else False
    return JSONResponse(
        content={
            "status": "online",
            "is_jetson_hardware": is_jetson,
            "is_logging": is_logging,
            "connected_ws_clients": len(_active_websockets),
            "camera_count": len(_camera_configs),
        }
    )


@app.get("/api/logging/status")
async def get_logging_status():
    """Return current logging status from hardware monitor."""
    if _monitor_instance is not None:
        return JSONResponse(content=_monitor_instance.get_logging_status())
    return JSONResponse(content={"is_logging": False, "csv_path": "", "logged_samples": 0})


@app.post("/api/logging/start")
async def start_logging(request: Request):
    """Interactively start/resume CSV telemetry recording."""
    if _monitor_instance is None:
        return JSONResponse(status_code=500, content={"error": "Monitor not initialized"})

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    custom_path = body.get("csv_path")
    status = _monitor_instance.start_logging(custom_path=custom_path)
    return JSONResponse(content={"message": "Logging started", "status": status})


@app.post("/api/logging/stop")
async def stop_logging():
    """Interactively stop CSV telemetry recording."""
    if _monitor_instance is None:
        return JSONResponse(status_code=500, content={"error": "Monitor not initialized"})

    status = _monitor_instance.stop_logging()
    return JSONResponse(content={"message": "Logging stopped", "status": status})


def _generate_synthetic_frame(cam_id: str, frame_num: int) -> bytes:
    """
    Generate an in-memory test frame with camera label, live timestamp, and animated sweep.
    Used for mock testing or when hardware appsink is warming up.
    """
    try:
        from PIL import Image, ImageDraw

        width, height = 640, 360
        img = Image.new("RGB", (width, height), color=(12, 18, 32))
        draw = ImageDraw.Draw(img)

        # Draw grid lines
        for x in range(0, width, 40):
            draw.line([(x, 0), (x, height)], fill=(20, 30, 50), width=1)
        for y in range(0, height, 40):
            draw.line([(0, y), (width, y)], fill=(20, 30, 50), width=1)

        # Animated radar sweep / motion box
        t = frame_num * 0.08
        box_x = int((width - 120) * (0.5 + 0.4 * math.sin(t)))
        box_y = int((height - 80) * (0.5 + 0.3 * math.cos(t * 0.7)))
        draw.rectangle([box_x, box_y, box_x + 100, box_y + 60], outline=(118, 185, 0), width=2)
        draw.text((box_x + 5, box_y + 5), "TARGET [MOTION]", fill=(118, 185, 0))

        # Timecode & Camera metadata
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        draw.text((20, 20), f"{cam_id.upper()} - 1080p @ 30FPS [REC ●]", fill=(240, 245, 255))
        draw.text((20, 45), f"TIMECODE: {now_str}", fill=(148, 163, 184))
        draw.text((20, height - 30), "CODEC: H.264 PASS-THROUGH | JETSON NVDEC", fill=(56, 189, 248))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue()
    except ImportError:
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"
            b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f"
            b"\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00"
            b"\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01"
            b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08"
            b"\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        )


@app.get("/api/stream/{cam_id}")
async def video_stream(cam_id: str):
    """
    Multipart JPEG streaming endpoint for low-latency web viewing.
    Provides live video to the Chromium browser grid.
    """
    async def frame_generator():
        frame_idx = 0
        while True:
            frame_bytes = _frame_buffers.get(cam_id)
            if not frame_bytes:
                frame_bytes = _generate_synthetic_frame(cam_id, frame_idx)

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame_bytes)).encode("ascii") + b"\r\n\r\n"
                + frame_bytes + b"\r\n"
            )
            frame_idx += 1
            await asyncio.sleep(0.033)  # ~30 FPS

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    """
    WebSocket endpoint streaming live hardware telemetry samples every 1 second.
    Includes is_logging status and logged samples count.
    """
    await websocket.accept()
    _active_websockets.append(websocket)
    logger.info(f"WebSocket client connected ({len(_active_websockets)} active)")

    try:
        while True:
            sample = {}
            if _monitor_instance is not None:
                with _monitor_instance._lock:
                    if _monitor_instance._samples:
                        sample = dict(_monitor_instance._samples[-1])
                sample["is_jetson_hardware"] = _monitor_instance.is_jetson
                sample["is_logging"] = _monitor_instance.is_logging
                sample["logged_samples"] = _monitor_instance._logged_samples_count
                sample["csv_path"] = getattr(_monitor_instance, "output_csv_path", "")

            await websocket.send_text(json.dumps(sample))
            await asyncio.sleep(getattr(_monitor_instance, "interval", 1.0))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug(f"WebSocket send error: {e}")
    finally:
        if websocket in _active_websockets:
            _active_websockets.remove(websocket)
        logger.info(f"WebSocket client disconnected ({len(_active_websockets)} remaining)")


# Mount static web directory
web_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
if os.path.isdir(web_dir):
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

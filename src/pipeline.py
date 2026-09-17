"""
Modular GStreamer Pipeline Builder for Jetson Multi-Stream CCTV Benchmark.
Constructs hardware-accelerated pass-through recording and 2x2 grid compositor pipelines.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("benchmark.pipeline")

# Attempt GStreamer import
try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    HAS_GST = True
except Exception:
    HAS_GST = False
    Gst = None
    GLib = None


@dataclass
class CameraStreamConfig:
    id: str
    name: str
    url: str
    codec: str = "h264"
    latency_ms: int = 200
    xpos: int = 0
    ypos: int = 0
    width: int = 960
    height: int = 540


class BenchmarkPipelineBuilder:
    """
    Constructs GStreamer pipelines using NVIDIA Jetson hardware plugins:
    - Zero-transcode remuxing (rtspsrc -> rtph264depay -> h264parse -> qtmux -> filesink)
    - Hardware-accelerated decoding & 2x2 grid compositing (nvv4l2decoder -> nvcompositor -> nv3dsink)
    """

    def __init__(
        self,
        config_path: str,
        recordings_dir: str = "recordings",
        use_hardware_accel: bool = True,
        display_sink: str = "nv3dsink",
        headless: bool = False,
        enable_recording: bool = True,
    ):
        self.config_path = config_path
        self.use_hardware_accel = use_hardware_accel
        self.display_sink = display_sink
        self.headless = headless
        self.enable_recording = enable_recording

        self.display_cameras: List[CameraStreamConfig] = []
        self.record_only_cameras: List[CameraStreamConfig] = []
        self.grid_settings: Dict[str, Any] = {
            "output_width": 1920,
            "output_height": 1080,
            "fps": 30
        }

        self._load_config()
        self.recordings_dir = self._setup_recordings_dir(recordings_dir)

    def _setup_recordings_dir(self, dir_path: str) -> str:
        """
        Verify recordings directory exists, is absolute, and is writable.
        If permission is denied (e.g. root-owned folder), falls back gracefully.
        """
        target_dir = os.path.abspath(dir_path)
        if not self.enable_recording:
            return target_dir

        try:
            os.makedirs(target_dir, exist_ok=True)
            try:
                os.chmod(target_dir, 0o777)
            except Exception:
                pass

            # Pre-flight write verification
            test_file = os.path.join(target_dir, f".write_test_{os.getpid()}")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            logger.info(f"Verified write access to recordings folder: {target_dir}")
            return target_dir
        except (PermissionError, OSError) as e:
            fallback_dir = os.path.join(os.path.expanduser("~"), "jetson_recordings")
            try:
                os.makedirs(fallback_dir, exist_ok=True)
                logger.warning(
                    f"Directory '{target_dir}' is not writable ({e}). "
                    f"Falling back to user home directory: '{fallback_dir}'"
                )
                return fallback_dir
            except Exception:
                tmp_dir = "/tmp/jetson_recordings"
                os.makedirs(tmp_dir, exist_ok=True)
                logger.warning(
                    f"Falling back to temporary directory: '{tmp_dir}'"
                )
                return tmp_dir

    def _load_config(self) -> None:
        """Parse cameras.json config file."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "grid" in data:
            self.grid_settings.update(data["grid"])

        for c in data.get("display_cameras", []):
            self.display_cameras.append(
                CameraStreamConfig(
                    id=c.get("id", "disp_cam"),
                    name=c.get("name", "Display Cam"),
                    url=c["url"],
                    codec=c.get("codec", "h264").lower(),
                    latency_ms=c.get("latency_ms", 200),
                    xpos=c.get("xpos", 0),
                    ypos=c.get("ypos", 0),
                    width=c.get("width", 960),
                    height=c.get("height", 540),
                )
            )

        for c in data.get("record_only_cameras", []):
            self.record_only_cameras.append(
                CameraStreamConfig(
                    id=c.get("id", "rec_cam"),
                    name=c.get("name", "Record Cam"),
                    url=c["url"],
                    codec=c.get("codec", "h264").lower(),
                    latency_ms=c.get("latency_ms", 200),
                )
            )

        logger.info(
            f"Loaded {len(self.display_cameras)} display cameras and "
            f"{len(self.record_only_cameras)} record-only cameras."
        )

    def _get_codec_elements(self, codec: str) -> Tuple[str, str, str]:
        """
        Return (rtp_depay, parse_element, hw_decoder) for a given codec.
        """
        if codec in ["h265", "hevc"]:
            depay = "rtph265depay"
            parser = "h265parse"
            decoder = "nvv4l2decoder" if self.use_hardware_accel else "avdec_h265"
        else:
            depay = "rtph264depay"
            parser = "h264parse"
            decoder = "nvv4l2decoder" if self.use_hardware_accel else "avdec_h264"
        return depay, parser, decoder

    def _get_sink_element(self) -> str:
        """Determine appropriate video sink based on configuration."""
        if self.headless:
            return "fakesink sync=false"

        sink = self.display_sink.lower().strip()
        if sink == "nv3dsink" and self.use_hardware_accel:
            return "nv3dsink sync=false"
        elif sink == "xvimagesink":
            return "xvimagesink sync=false"
        elif sink == "fakesink":
            return "fakesink sync=false"
        elif sink == "autovideosink":
            return "autovideosink sync=false"
        else:
            return f"{sink} sync=false"

    def generate_display_pipeline_str(self, session_timestamp: str) -> str:
        """
        Build GStreamer pipeline string for the 4 display cameras:
        Each camera is teed:
        - Branch 1: Pass-through remux to MP4 filesink
        - Branch 2: nvv4l2decoder -> nvvidconv -> nvcompositor (2x2 grid) -> nv3dsink
        """
        compositor_elem = "nvcompositor" if self.use_hardware_accel else "compositor"
        conv_elem = "nvvidconv" if self.use_hardware_accel else "videoconvert"
        sink_elem = self._get_sink_element()

        out_w = self.grid_settings.get("output_width", 1920)
        out_h = self.grid_settings.get("output_height", 1080)

        caps_filter = f"video/x-raw(memory:NVMM), width={out_w}, height={out_h}" if self.use_hardware_accel else f"video/x-raw, width={out_w}, height={out_h}"

        parts = []

        # Compositor definition
        comp_decl = f"{compositor_elem} name=comp\n"
        for i, cam in enumerate(self.display_cameras):
            comp_decl += f"  sink_{i}::xpos={cam.xpos} sink_{i}::ypos={cam.ypos} sink_{i}::width={cam.width} sink_{i}::height={cam.height}\n"
        comp_decl += f"  ! {conv_elem} ! {caps_filter} ! {sink_elem}\n"
        parts.append(comp_decl)

        # Ingestion branches with tee for each display camera
        for i, cam in enumerate(self.display_cameras):
            depay, parser, decoder = self._get_codec_elements(cam.codec)
            rec_filename = os.path.join(self.recordings_dir, f"{cam.id}_{session_timestamp}.mp4").replace("\\", "/")
            tee_name = f"tee_{cam.id}"

            if self.enable_recording:
                branch = (
                    f"rtspsrc location=\"{cam.url}\" latency={cam.latency_ms} drop-on-latency=true protocols=tcp+udp !\n"
                    f"  {depay} ! {parser} ! tee name={tee_name}\n"
                    f"  {tee_name}. ! queue max-size-buffers=120 max-size-time=0 max-size-bytes=0 ! "
                    f"qtmux faststart=true ! filesink location=\"{rec_filename}\"\n"
                    f"  {tee_name}. ! queue max-size-buffers=60 max-size-time=0 max-size-bytes=0 ! "
                    f"{decoder} ! {conv_elem} ! comp.sink_{i}\n"
                )
            else:
                branch = (
                    f"rtspsrc location=\"{cam.url}\" latency={cam.latency_ms} drop-on-latency=true protocols=tcp+udp !\n"
                    f"  {depay} ! {parser} ! queue max-size-buffers=60 max-size-time=0 max-size-bytes=0 ! "
                    f"{decoder} ! {conv_elem} ! comp.sink_{i}\n"
                )
            parts.append(branch)

        return "\n".join(parts)

    def generate_record_pipeline_str(self, cam: CameraStreamConfig, session_timestamp: str) -> str:
        """
        Build headless zero-transcode pass-through pipeline string for a single camera:
        rtspsrc -> rtp<codec>depay -> <codec>parse -> qtmux -> filesink (or fakesink if recording disabled)
        """
        depay, parser, _ = self._get_codec_elements(cam.codec)
        rec_filename = os.path.join(self.recordings_dir, f"{cam.id}_{session_timestamp}.mp4").replace("\\", "/")

        if self.enable_recording:
            sink_stage = f"qtmux faststart=true ! filesink location=\"{rec_filename}\""
        else:
            sink_stage = "fakesink sync=false"

        pipeline_str = (
            f"rtspsrc location=\"{cam.url}\" latency={cam.latency_ms} drop-on-latency=true protocols=tcp+udp !\n"
            f"  {depay} ! {parser} ! queue max-size-buffers=120 max-size-time=0 max-size-bytes=0 !\n"
            f"  {sink_stage}"
        )
        return pipeline_str

    def generate_unified_pipeline_str(self, session_timestamp: str) -> str:
        """
        Build a single unified GStreamer pipeline string containing all 10 cameras:
        - 4 display & record streams (teed into nvcompositor + MP4 filesink)
        - 6 record-only streams (direct pass-through into MP4 filesink)
        """
        display_part = self.generate_display_pipeline_str(session_timestamp)
        record_parts = [
            self.generate_record_pipeline_str(cam, session_timestamp)
            for cam in self.record_only_cameras
        ]
        return display_part + "\n\n" + "\n\n".join(record_parts)

    def build_pipelines(self, session_timestamp: Optional[str] = None) -> Dict[str, Any]:
        """
        Compile GStreamer pipeline objects.
        Returns a dictionary containing 'display' pipeline and 'record_only' list.
        """
        if not HAS_GST:
            raise RuntimeError(
                "GStreamer Python bindings (gi.repository.Gst) are not available. "
                "Ensure python3-gst-1.0 and python3-gi are installed."
            )

        ts = session_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        pipelines = {}

        # Build Display Grid Pipeline (incorporating 4 recordings + 2x2 display)
        disp_str = self.generate_display_pipeline_str(ts)
        logger.debug(f"Display Pipeline Launch String:\n{disp_str}")
        try:
            disp_pipe = Gst.parse_launch(disp_str)
            pipelines["display"] = disp_pipe
        except Exception as e:
            logger.error(f"Failed to parse display pipeline: {e}")
            raise

        # Build Record-Only Pipelines (6 independent pipelines)
        pipelines["record_only"] = []
        for cam in self.record_only_cameras:
            rec_str = self.generate_record_pipeline_str(cam, ts)
            logger.debug(f"Record Pipeline [{cam.id}] Launch String:\n{rec_str}")
            try:
                rec_pipe = Gst.parse_launch(rec_str)
                pipelines["record_only"].append({"id": cam.id, "pipeline": rec_pipe})
            except Exception as e:
                logger.error(f"Failed to parse record pipeline for {cam.id}: {e}")
                raise

        return pipelines

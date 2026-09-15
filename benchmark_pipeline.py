#!/usr/bin/env python3
"""
NVIDIA Jetson Multi-Stream CCTV Benchmark Runner with Live Frontend Dashboard
Workload:
  1. Ingests 10 concurrent RTSP camera streams.
  2. Saves all 10 streams directly to disk (.mp4) via zero-transcode pass-through remuxing.
  3. Streams 4 active feeds to a local web frontend (FastAPI + WebSocket).
  4. Renders a 2x2 video grid & real-time hardware telemetry dashboard via Chromium kiosk on DISPLAY=:0.
  5. Continuously captures hardware metrics via jtop to a timestamped CSV.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# Local module imports
from src.monitor import HardwareMonitor, HAS_JTOP
from src.pipeline import BenchmarkPipelineBuilder, HAS_GST
from src.server import app, init_server

# Try importing tabulate for formatted report
try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

# Try importing uvicorn
try:
    import uvicorn
    HAS_UVICORN = True
except ImportError:
    HAS_UVICORN = False


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("benchmark.main")


class JetsonBenchmarkRunner:
    """
    Coordinates GStreamer multi-stream ingestion, recording, local web server,
    Chromium kiosk display, and concurrent hardware telemetry logging.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_csv_path = os.path.join(
            self.args.log_dir, f"benchmark_{self.session_ts}.csv"
        )
        self.builder = BenchmarkPipelineBuilder(
            config_path=self.args.config,
            recordings_dir=self.args.recordings_dir,
            use_hardware_accel=not self.args.software_decode,
            display_sink=self.args.sink,
            headless=self.args.headless,
        )
        self.monitor = HardwareMonitor(
            output_csv_path=self.log_csv_path,
            interval=self.args.interval,
            enable_console_log=self.args.console_metrics,
        )
        self.pipelines: Dict[str, Any] = {}
        self.loop = None
        self._shutdown_initiated = False
        self._uvicorn_server: Optional[Any] = None
        self._server_thread: Optional[threading.Thread] = None
        self._browser_process: Optional[subprocess.Popen] = None

    def _start_web_server(self) -> None:
        """Start FastAPI/Uvicorn server in a dedicated background thread."""
        if not HAS_UVICORN:
            logger.warning("Uvicorn is not installed. Web frontend dashboard will not be served.")
            return

        camera_data = [
            {"id": c.id, "name": c.name, "url": c.url, "codec": c.codec, "type": "display"}
            for c in self.builder.display_cameras
        ] + [
            {"id": c.id, "name": c.name, "url": c.url, "codec": c.codec, "type": "record_only"}
            for c in self.builder.record_only_cameras
        ]
        init_server(self.monitor, camera_data)

        config = uvicorn.Config(
            app=app,
            host=self.args.host,
            port=self.args.port,
            log_level="warning",
            access_log=False
        )
        self._uvicorn_server = uvicorn.Server(config)

        def _run_server():
            logger.info(f"Starting web dashboard server at http://{self.args.host}:{self.args.port}")
            self._uvicorn_server.run()

        self._server_thread = threading.Thread(target=_run_server, name="UvicornServerThread", daemon=True)
        self._server_thread.start()
        time.sleep(0.5)

    def _stop_web_server(self) -> None:
        """Stop background web server."""
        if self._uvicorn_server is not None:
            logger.info("Stopping web dashboard server...")
            self._uvicorn_server.should_exit = True
            if self._server_thread is not None:
                self._server_thread.join(timeout=2.0)
            self._uvicorn_server = None
            self._server_thread = None

    def _launch_browser_kiosk(self) -> None:
        """Spawn Chromium in kiosk mode targeting DISPLAY=:0."""
        target_url = f"http://localhost:{self.args.port}"
        env = os.environ.copy()
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"

        # Check if helper script exists and is executable
        script_path = os.path.join(os.path.dirname(__file__), "launch_kiosk.sh")
        if os.name != "nt" and os.path.exists(script_path):
            try:
                logger.info(f"Launching kiosk browser on {env['DISPLAY']} using launch_kiosk.sh...")
                self._browser_process = subprocess.Popen(
                    ["bash", script_path, str(self.args.port)],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                return
            except Exception as e:
                logger.warning(f"Failed to launch kiosk via script: {e}")

        # Fallback direct browser invocation
        browser_bins = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]
        if os.name == "nt":
            browser_bins = ["chrome", "msedge"]

        for b in browser_bins:
            try:
                cmd = [
                    b,
                    f"--app={target_url}",
                    "--noerrdialogs",
                    "--disable-infobars",
                    "--autoplay-policy=no-user-gesture-required",
                ]
                if self.args.kiosk:
                    cmd.append("--kiosk")
                self._browser_process = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"Spawned browser kiosk ({b}) pointing to {target_url} (PID: {self._browser_process.pid})")
                return
            except FileNotFoundError:
                continue

        logger.warning("No supported browser (Chromium/Chrome) found to launch kiosk automatically.")

    def _stop_browser_kiosk(self) -> None:
        """Terminate spawned browser kiosk process."""
        if self._browser_process is not None:
            logger.info("Closing browser kiosk...")
            try:
                self._browser_process.terminate()
                self._browser_process.wait(timeout=2.0)
            except Exception:
                try:
                    self._browser_process.kill()
                except Exception:
                    pass
            self._browser_process = None

    def print_dry_run_info(self) -> None:
        """Display configuration and generated pipeline strings without starting pipelines."""
        print("=" * 80)
        print(" NVIDIA JETSON MULTI-STREAM CCTV BENCHMARK - DRY RUN MODE")
        print("=" * 80)
        print(f"Config File       : {self.args.config}")
        print(f"Hardware Accel    : {not self.args.software_decode}")
        print(f"Display Sink      : {self.args.sink} (Headless: {self.args.headless})")
        print(f"Web Dashboard     : http://{self.args.host}:{self.args.port} (Enabled: {not self.args.no_frontend})")
        print(f"Kiosk Browser     : {self.args.kiosk}")
        print(f"Benchmark Duration: {self.args.duration or 'Indefinite (until Ctrl+C)'}")
        print(f"Output CSV Log    : {self.log_csv_path}")
        print(f"Recordings Dir    : {self.args.recordings_dir}")
        print(f"GStreamer Avail   : {HAS_GST}")
        print(f"jtop Avail        : {HAS_JTOP}")
        print(f"Uvicorn Avail     : {HAS_UVICORN}")
        print("-" * 80)

        print("\n[1] 4-STREAM DISPLAY & RECORD PIPELINE (2x2 GRID + TEE PASS-THROUGH REMUX):")
        print("-" * 80)
        print(self.builder.generate_display_pipeline_str(self.session_ts))

        print("\n[2] 6 RECORD-ONLY PASS-THROUGH PIPELINES (ZERO-TRANSCODE REMUXING):")
        print("-" * 80)
        for cam in self.builder.record_only_cameras:
            print(f"--- Pipeline [{cam.id} - {cam.name}] ---")
            print(self.builder.generate_record_pipeline_str(cam, self.session_ts))
            print()

        print("=" * 80)
        print("Dry run completed successfully. All pipeline specifications and web configs are valid.")
        print("=" * 80)

    def _bus_call(self, bus: Any, message: Any, pipe_name: str) -> bool:
        """Handle GStreamer bus messages."""
        if not HAS_GST:
            return True

        from gi.repository import Gst
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info(f"[{pipe_name}] End of stream (EOS) received.")
            if pipe_name == "display":
                self.request_stop()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"[{pipe_name}] GStreamer Error: {err.message}")
            if debug:
                logger.debug(f"[{pipe_name}] Debug details: {debug}")
            self.request_stop()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"[{pipe_name}] GStreamer Warning: {warn.message}")
        return True

    def request_stop(self) -> None:
        """Initiate graceful shutdown sequence."""
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        logger.info("Shutdown requested. Finalizing recordings and stopping services...")

        if self.loop is not None and HAS_GST:
            self.loop.quit()

    def _send_eos_and_null(self) -> None:
        """Send EOS down all active pipelines so qtmux finalizes the MP4 headers."""
        if not HAS_GST or not self.pipelines:
            return

        from gi.repository import Gst
        logger.info("Sending EOS to GStreamer pipelines to commit MP4 file headers...")

        # Send EOS to display pipeline
        if "display" in self.pipelines and self.pipelines["display"]:
            try:
                self.pipelines["display"].send_event(Gst.Event.new_eos())
            except Exception as e:
                logger.warning(f"Failed to send EOS to display pipeline: {e}")

        # Send EOS to record-only pipelines
        for rec in self.pipelines.get("record_only", []):
            try:
                rec["pipeline"].send_event(Gst.Event.new_eos())
            except Exception as e:
                logger.warning(f"Failed to send EOS to record pipeline {rec.get('id')}: {e}")

        # Brief pause to allow muxers to flush moov atom to disk
        time.sleep(1.0)

        # Set pipelines to NULL state
        if "display" in self.pipelines and self.pipelines["display"]:
            self.pipelines["display"].set_state(Gst.State.NULL)

        for rec in self.pipelines.get("record_only", []):
            rec["pipeline"].set_state(Gst.State.NULL)

        logger.info("All GStreamer pipelines transitioned to NULL state.")

    def run(self) -> None:
        """Execute the benchmark session."""
        if self.args.dry_run:
            self.print_dry_run_info()
            return

        # Start Web Server if enabled
        if not self.args.no_frontend:
            self._start_web_server()

        # Start Browser Kiosk if requested
        if self.args.kiosk or self.args.open_browser:
            self._launch_browser_kiosk()

        # Start Hardware Monitor
        self.monitor.start()

        # Register OS signal handlers
        def _signal_handler(sig, frame):
            logger.info(f"Received signal {sig}. Stopping benchmark...")
            self.request_stop()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        if not HAS_GST:
            logger.warning(
                "GStreamer Python bindings (gi.repository.Gst) are not available. "
                "Running in simulated streaming / telemetry benchmark mode."
            )
            # Run without GStreamer loop (timer-based or Ctrl+C)
            start_time = time.time()
            try:
                while not self._shutdown_initiated:
                    if self.args.duration and (time.time() - start_time) >= self.args.duration:
                        logger.info(f"Duration limit ({self.args.duration}s) reached.")
                        break
                    time.sleep(0.5)
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt caught.")
            finally:
                self._cleanup()
            return

        from gi.repository import GLib, Gst

        # Build pipelines
        logger.info("Constructing GStreamer pipelines...")
        try:
            self.pipelines = self.builder.build_pipelines(session_timestamp=self.session_ts)
        except Exception as e:
            logger.critical(f"Failed to build pipelines: {e}")
            self._cleanup()
            sys.exit(1)

        # Setup bus watchers
        disp_bus = self.pipelines["display"].get_bus()
        disp_bus.add_signal_watch()
        disp_bus.connect("message", self._bus_call, "display")

        for rec in self.pipelines["record_only"]:
            bus = rec["pipeline"].get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._bus_call, f"rec_{rec['id']}")

        # Start GStreamer pipelines
        logger.info("Starting GStreamer display and recording pipelines...")
        self.pipelines["display"].set_state(Gst.State.PLAYING)
        for rec in self.pipelines["record_only"]:
            rec["pipeline"].set_state(Gst.State.PLAYING)

        logger.info("=" * 80)
        logger.info(" BENCHMARK RUNNING: 10 RTSP INGESTIONS (4 Display Grid + 10 MP4 Records)")
        logger.info(f" Live Frontend Dashboard: http://{self.args.host}:{self.args.port}")
        if self.args.duration:
            logger.info(f" Auto-terminating after {self.args.duration} seconds.")
        else:
            logger.info(" Running continuously. Press Ctrl+C to terminate.")
        logger.info("=" * 80)

        # Set up GLib main loop and timer if duration specified
        self.loop = GLib.MainLoop()

        if self.args.duration:
            def _timer_callback():
                logger.info(f"Benchmark duration ({self.args.duration}s) reached.")
                self.request_stop()
                return False  # Do not repeat timer

            GLib.timeout_add_seconds(self.args.duration, _timer_callback)

        try:
            self.loop.run()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt caught.")
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """Perform orderly cleanup of browser, pipelines, monitor, and web server."""
        self._stop_browser_kiosk()
        self._send_eos_and_null()
        self.monitor.stop()
        self._stop_web_server()
        self.print_summary_report()

    def print_summary_report(self) -> None:
        """Print benchmark statistics summary table."""
        summary = self.monitor.get_summary()
        if not summary:
            logger.warning("No performance metrics were captured.")
            return

        metrics = summary.get("metrics", {})

        table_data = [
            ["CPU Average (%)", metrics.get("cpu_avg_pct", {}).get("avg", "N/A"), metrics.get("cpu_avg_pct", {}).get("max", "N/A"), metrics.get("cpu_avg_pct", {}).get("min", "N/A")],
            ["GPU GR3D (%)", metrics.get("gpu_gr3d_pct", {}).get("avg", "N/A"), metrics.get("gpu_gr3d_pct", {}).get("max", "N/A"), metrics.get("gpu_gr3d_pct", {}).get("min", "N/A")],
            ["NVDEC Hardware Decoder (%)", metrics.get("nvdec_pct", {}).get("avg", "N/A"), metrics.get("nvdec_pct", {}).get("max", "N/A"), metrics.get("nvdec_pct", {}).get("min", "N/A")],
            ["NVENC Hardware Encoder (%)", metrics.get("nvenc_pct", {}).get("avg", "N/A"), metrics.get("nvenc_pct", {}).get("max", "N/A"), metrics.get("nvenc_pct", {}).get("min", "N/A")],
            ["RAM Used (MB)", metrics.get("ram_used_mb", {}).get("avg", "N/A"), metrics.get("ram_used_mb", {}).get("max", "N/A"), metrics.get("ram_used_mb", {}).get("min", "N/A")],
            ["RAM Utilization (%)", metrics.get("ram_pct", {}).get("avg", "N/A"), metrics.get("ram_pct", {}).get("max", "N/A"), metrics.get("ram_pct", {}).get("min", "N/A")],
            ["System Power Draw (mW)", metrics.get("power_mw", {}).get("avg", "N/A"), metrics.get("power_mw", {}).get("max", "N/A"), metrics.get("power_mw", {}).get("min", "N/A")],
            ["CPU Temperature (°C)", metrics.get("temp_cpu_c", {}).get("avg", "N/A"), metrics.get("temp_cpu_c", {}).get("max", "N/A"), metrics.get("temp_cpu_c", {}).get("min", "N/A")],
            ["GPU Temperature (°C)", metrics.get("temp_gpu_c", {}).get("avg", "N/A"), metrics.get("temp_gpu_c", {}).get("max", "N/A"), metrics.get("temp_gpu_c", {}).get("min", "N/A")],
        ]

        print("\n" + "=" * 80)
        print("                  JETSON MULTI-STREAM BENCHMARK SUMMARY")
        print("=" * 80)
        print(f"Samples Collected : {summary.get('sample_count', 0)}")
        print(f"Elapsed Duration  : {summary.get('duration_sec', 0.0):.1f} s")
        print(f"Hardware Mode     : {'NVIDIA JetPack (jtop)' if summary.get('is_jetson_hardware') else 'Standard System (Fallback)'}")
        print(f"CSV Metrics File  : {self.log_csv_path}")
        print(f"Recordings Folder : {self.args.recordings_dir}")
        print("-" * 80)

        if HAS_TABULATE:
            headers = ["Metric", "Average", "Maximum", "Minimum"]
            print(tabulate(table_data, headers=headers, tablefmt="fancy_grid"))
        else:
            header_fmt = "{:<32} | {:<12} | {:<12} | {:<12}"
            row_fmt = "{:<32} | {:<12} | {:<12} | {:<12}"
            print(header_fmt.format("Metric", "Average", "Maximum", "Minimum"))
            print("-" * 75)
            for row in table_data:
                print(row_fmt.format(str(row[0]), str(row[1]), str(row[2]), str(row[3])))

        print("=" * 80)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NVIDIA Jetson Multi-Stream CCTV Benchmark with Live Frontend Dashboard"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=os.path.join("config", "cameras.json"),
        help="Path to camera configuration JSON (default: config/cameras.json)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Benchmark duration in seconds (optional; if not set, runs until Ctrl+C)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Telemetry logging interval in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default="logs",
        help="Output directory for CSV metrics and runtime logs (default: logs)"
    )
    parser.add_argument(
        "--recordings-dir",
        type=str,
        default="recordings",
        help="Output directory for camera MP4 recordings (default: recordings)"
    )
    parser.add_argument(
        "--sink",
        type=str,
        default="nv3dsink",
        help="Video sink plugin (nv3dsink, xvimagesink, autovideosink, fakesink)"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without displaying X11 video window (uses fakesink for compositing)"
    )
    parser.add_argument(
        "--software-decode",
        action="store_true",
        help="Use software decoding/compositing (compositor, avdec_h264) instead of Tegra plugins"
    )
    parser.add_argument(
        "--console-metrics",
        action="store_true",
        help="Print real-time hardware telemetry samples to console"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Web server bind host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web server bind port (default: 8000)"
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Disable web dashboard server and run in CLI-only mode"
    )
    parser.add_argument(
        "--kiosk",
        action="store_true",
        help="Launch fullscreen kiosk browser on DISPLAY=:0"
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open dashboard in standard windowed browser"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate camera configs and print pipeline launch commands without executing GStreamer"
    )
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_arguments()
    runner = JetsonBenchmarkRunner(cli_args)
    runner.run()

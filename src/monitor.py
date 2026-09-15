"""
Hardware Performance Monitor for NVIDIA Jetson and Multi-Stream Benchmark.
Captures CPU, GPU (GR3D), NVDEC, NVENC, RAM, Power, and Temperature metrics
using the `jtop` (jetson-stats) Python SDK with graceful psutil fallback.
Supports interactive Start / Stop CSV logging sessions.
"""

import csv
import logging
import os
import platform
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("benchmark.monitor")

# Attempt importing jtop
try:
    from jtop import jtop, JtopException
    HAS_JTOP = True
except ImportError:
    HAS_JTOP = False
    jtop = None
    JtopException = Exception

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class HardwareMonitor:
    """
    Background worker thread logging Jetson hardware metrics to CSV.
    Allows toggling CSV recording on and off interactively while maintaining
    real-time telemetry broadcasting for the web dashboard.
    """

    CSV_HEADERS = [
        "timestamp",
        "cpu_avg_pct",
        "cpu_cores_pct",
        "gpu_gr3d_pct",
        "nvdec_pct",
        "nvenc_pct",
        "ram_used_mb",
        "ram_total_mb",
        "ram_pct",
        "swap_used_mb",
        "power_mw",
        "temp_cpu_c",
        "temp_gpu_c",
        "temp_aux_c"
    ]

    def __init__(
        self,
        output_csv_path: str,
        interval: float = 1.0,
        enable_console_log: bool = False,
        auto_start_logging: bool = True
    ):
        self.output_csv_path = output_csv_path
        self.interval = max(0.1, interval)
        self.enable_console_log = enable_console_log
        self.is_logging = auto_start_logging

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._samples: List[Dict[str, Any]] = []
        self._pending_csv_rows: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.is_jetson = False
        self._logged_samples_count = 0
        self._permission_error_warned = False

        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.output_csv_path)), exist_ok=True)
        if self.is_logging:
            self._init_csv_file(self.output_csv_path)

    def _init_csv_file(self, path: str) -> None:
        """Create or initialize the CSV file with headers."""
        try:
            with open(path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)
            logger.info(f"Initialized telemetry CSV at: {path}")
            self._permission_error_warned = False
        except Exception as e:
            logger.error(f"Failed to initialize CSV at {path}: {e}")

    def start_logging(self, custom_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Interactively start/resume logging telemetry to CSV.
        Can optionally specify a new target CSV path.
        """
        with self._lock:
            if custom_path and custom_path != self.output_csv_path:
                self.output_csv_path = custom_path
                os.makedirs(os.path.dirname(os.path.abspath(self.output_csv_path)), exist_ok=True)
                self._init_csv_file(self.output_csv_path)
            elif not os.path.exists(self.output_csv_path):
                self._init_csv_file(self.output_csv_path)

            self.is_logging = True
            logger.info(f"Logging started. Target CSV: {self.output_csv_path}")

        return self.get_logging_status()

    def stop_logging(self) -> Dict[str, Any]:
        """
        Interactively stop logging telemetry to CSV.
        Flushes any pending rows and closes write locks.
        """
        with self._lock:
            self.is_logging = False
            logger.info(f"Logging stopped. Total samples recorded to CSV: {self._logged_samples_count}")

        return self.get_logging_status()

    def get_logging_status(self) -> Dict[str, Any]:
        """Return current logging state, CSV path, and sample counts."""
        with self._lock:
            return {
                "is_logging": self.is_logging,
                "csv_path": self.output_csv_path,
                "total_samples": len(self._samples),
                "logged_samples": self._logged_samples_count,
            }

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Monitor thread is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="HardwareMonitorThread", daemon=True)
        self._thread.start()
        logger.info(f"Hardware monitor started (interval: {self.interval}s, output: {self.output_csv_path})")

    def stop(self) -> None:
        """Signal the thread to stop and wait for completion."""
        if self._thread is None:
            return

        logger.info("Stopping hardware monitor...")
        self._stop_event.set()
        self._thread.join(timeout=3.0)
        self._thread = None
        logger.info(f"Hardware monitor stopped. Collected {len(self._samples)} sample points.")

    def _run(self) -> None:
        """Worker loop trying jtop first, falling back to psutil."""
        if HAS_JTOP:
            try:
                with jtop(interval=self.interval) as jetson:
                    if jetson.ok():
                        self.is_jetson = True
                        logger.info("Connected to jetson-stats (jtop) service successfully.")
                        self._run_jtop_loop(jetson)
                        return
                    else:
                        logger.warning("jtop service returned not ok. Falling back to system monitor.")
            except JtopException as e:
                logger.warning(f"jtop connection failed ({e}). Falling back to system monitor.")
            except Exception as e:
                logger.warning(f"Unexpected error initializing jtop ({e}). Falling back to system monitor.")

        # Fallback loop
        self.is_jetson = False
        logger.info("Running in fallback system monitor mode (CPU/RAM via psutil).")
        self._run_fallback_loop()

    def _run_jtop_loop(self, jetson: Any) -> None:
        """Collect metrics using the jtop SDK."""
        while not self._stop_event.is_set():
            loop_start = time.time()
            if not jetson.ok():
                logger.warning("jtop communication lost.")
                break

            now_iso = datetime.now().isoformat(timespec="milliseconds")
            stats = jetson.stats
            cpu = jetson.cpu
            mem = jetson.memory
            power = getattr(jetson, "power", {})
            temp = getattr(jetson, "temperature", {})

            # CPU metrics
            cpu_cores = []
            if isinstance(cpu, dict):
                cpu_avg = cpu.get("total", 0.0)
                for key in sorted(cpu.keys()):
                    if key.startswith("CPU") and key[3:].isdigit():
                        cpu_cores.append(f"{cpu[key]:.1f}")
            else:
                cpu_avg = 0.0

            cpu_cores_str = ";".join(cpu_cores) if cpu_cores else "0.0"

            # GPU Engine (GR3D)
            gpu_gr3d = stats.get("GPU", 0.0) if isinstance(stats, dict) else 0.0

            # NVDEC & NVENC
            nvdec = 0.0
            nvenc = 0.0
            if isinstance(stats, dict):
                nvdec = stats.get("NVDEC", stats.get("APE", 0.0))
                nvenc = stats.get("NVENC", 0.0)
            if hasattr(jetson, "nvdec") and isinstance(jetson.nvdec, (int, float)):
                nvdec = float(jetson.nvdec)
            if hasattr(jetson, "nvenc") and isinstance(jetson.nvenc, (int, float)):
                nvenc = float(jetson.nvenc)

            # RAM & Swap (MB)
            ram_dict = mem.get("RAM", {}) if isinstance(mem, dict) else {}
            ram_used = ram_dict.get("used", 0) / 1024.0 if ram_dict.get("used", 0) > 10000 else ram_dict.get("used", 0.0)
            ram_tot = ram_dict.get("tot", 1) / 1024.0 if ram_dict.get("tot", 1) > 10000 else ram_dict.get("tot", 1.0)
            ram_pct = (ram_used / ram_tot * 100.0) if ram_tot > 0 else 0.0

            swap_dict = mem.get("SWAP", {}) if isinstance(mem, dict) else {}
            swap_used = swap_dict.get("used", 0) / 1024.0 if swap_dict.get("used", 0) > 10000 else swap_dict.get("used", 0.0)

            # Power (mW)
            power_mw = 0.0
            if isinstance(power, dict):
                tot_pwr = power.get("tot", {})
                if isinstance(tot_pwr, dict):
                    power_mw = float(tot_pwr.get("power", 0.0))
                elif isinstance(power.get("total"), (int, float)):
                    power_mw = float(power.get("total", 0.0))

            # Temperature (°C)
            temp_cpu = 0.0
            temp_gpu = 0.0
            temp_aux = 0.0
            if isinstance(temp, dict):
                temp_cpu = float(temp.get("CPU", temp.get("cpu", 0.0)))
                temp_gpu = float(temp.get("GPU", temp.get("gpu", 0.0)))
                temp_aux = float(temp.get("AUX", temp.get("thermal", 0.0)))

            row = {
                "timestamp": now_iso,
                "cpu_avg_pct": round(float(cpu_avg), 2),
                "cpu_cores_pct": cpu_cores_str,
                "gpu_gr3d_pct": round(float(gpu_gr3d), 2),
                "nvdec_pct": round(float(nvdec), 2),
                "nvenc_pct": round(float(nvenc), 2),
                "ram_used_mb": round(float(ram_used), 1),
                "ram_total_mb": round(float(ram_tot), 1),
                "ram_pct": round(float(ram_pct), 2),
                "swap_used_mb": round(float(swap_used), 1),
                "power_mw": round(float(power_mw), 1),
                "temp_cpu_c": round(float(temp_cpu), 1),
                "temp_gpu_c": round(float(temp_gpu), 1),
                "temp_aux_c": round(float(temp_aux), 1),
            }

            self._record_sample(row)

            elapsed = time.time() - loop_start
            sleep_time = max(0.01, self.interval - elapsed)
            if self._stop_event.wait(timeout=sleep_time):
                break

    def _run_fallback_loop(self) -> None:
        """Fallback metrics collector using psutil for non-Jetson/simulated environments."""
        while not self._stop_event.is_set():
            loop_start = time.time()
            now_iso = datetime.now().isoformat(timespec="milliseconds")

            cpu_avg = 0.0
            cpu_cores_str = ""
            ram_used_mb = 0.0
            ram_tot_mb = 0.0
            ram_pct = 0.0
            swap_used_mb = 0.0

            if HAS_PSUTIL:
                try:
                    cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
                    cpu_avg = psutil.cpu_percent(interval=None)
                    cpu_cores_str = ";".join(f"{c:.1f}" for c in cpu_cores)
                    vm = psutil.virtual_memory()
                    ram_used_mb = vm.used / (1024.0 * 1024.0)
                    ram_tot_mb = vm.total / (1024.0 * 1024.0)
                    ram_pct = vm.percent
                    swap = psutil.swap_memory()
                    swap_used_mb = swap.used / (1024.0 * 1024.0)
                except Exception as e:
                    logger.debug(f"psutil reading failed: {e}")

            row = {
                "timestamp": now_iso,
                "cpu_avg_pct": round(float(cpu_avg), 2),
                "cpu_cores_pct": cpu_cores_str,
                "gpu_gr3d_pct": 0.0,
                "nvdec_pct": 0.0,
                "nvenc_pct": 0.0,
                "ram_used_mb": round(float(ram_used_mb), 1),
                "ram_total_mb": round(float(ram_tot_mb), 1),
                "ram_pct": round(float(ram_pct), 2),
                "swap_used_mb": round(float(swap_used_mb), 1),
                "power_mw": 0.0,
                "temp_cpu_c": 0.0,
                "temp_gpu_c": 0.0,
                "temp_aux_c": 0.0,
            }

            self._record_sample(row)

            elapsed = time.time() - loop_start
            sleep_time = max(0.01, self.interval - elapsed)
            if self._stop_event.wait(timeout=sleep_time):
                break

    def _record_sample(self, row: Dict[str, Any]) -> None:
        """Write sample row to CSV if logging is enabled, and store in-memory."""
        with self._lock:
            self._samples.append(row)

            if not self.is_logging:
                return

            # If logging is active, append to file
            rows_to_write = self._pending_csv_rows + [row]

            try:
                with open(self.output_csv_path, mode="a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    for r in rows_to_write:
                        writer.writerow([r.get(h, "") for h in self.CSV_HEADERS])
                self._logged_samples_count += len(rows_to_write)
                self._pending_csv_rows.clear()
                self._permission_error_warned = False
            except PermissionError:
                # File is likely open in an external application (e.g. Excel on Windows)
                self._pending_csv_rows.append(row)
                if not self._permission_error_warned:
                    logger.warning(
                        f"Target CSV '{self.output_csv_path}' is currently locked by another application (e.g. Excel). "
                        "Samples are temporarily buffered in RAM and will flush once the file is closed."
                    )
                    self._permission_error_warned = True
            except Exception as e:
                logger.error(f"Failed to append to CSV {self.output_csv_path}: {e}")

        if self.enable_console_log:
            logger.info(
                f"[METRICS] CPU: {row['cpu_avg_pct']:5.1f}% | GPU: {row['gpu_gr3d_pct']:5.1f}% | "
                f"NVDEC: {row['nvdec_pct']:5.1f}% | RAM: {row['ram_used_mb']:.0f}/{row['ram_total_mb']:.0f}MB ({row['ram_pct']}%) | "
                f"Pwr: {row['power_mw']:.0f}mW"
            )

    def get_summary(self) -> Dict[str, Any]:
        """Compute statistical summary (avg, max, min) of collected samples."""
        with self._lock:
            if not self._samples:
                return {}

            keys = [
                "cpu_avg_pct",
                "gpu_gr3d_pct",
                "nvdec_pct",
                "nvenc_pct",
                "ram_used_mb",
                "ram_pct",
                "power_mw",
                "temp_cpu_c",
                "temp_gpu_c",
            ]

            summary: Dict[str, Any] = {
                "sample_count": len(self._samples),
                "logged_samples_count": self._logged_samples_count,
                "is_logging": self.is_logging,
                "duration_sec": len(self._samples) * self.interval,
                "is_jetson_hardware": self.is_jetson,
                "metrics": {}
            }

            for k in keys:
                vals = [float(s[k]) for s in self._samples if k in s and isinstance(s[k], (int, float))]
                if vals:
                    summary["metrics"][k] = {
                        "avg": round(sum(vals) / len(vals), 2),
                        "max": round(max(vals), 2),
                        "min": round(min(vals), 2),
                    }

            return summary

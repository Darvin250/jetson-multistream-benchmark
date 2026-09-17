/**
 * NVIDIA Jetson Surveillance Mission Control - Frontend Client Logic
 * Handles real-time WebSocket telemetry, Canvas multi-series timeline rendering,
 * circular gauge interpolation, camera stream monitoring, and Start/Stop logging.
 */

document.addEventListener("DOMContentLoaded", () => {
  // Header Elements
  const liveClockEl = document.getElementById("live-clock");
  const benchmarkTimerEl = document.getElementById("benchmark-timer");
  const wsStatusEl = document.getElementById("ws-status");
  const wsLabelEl = document.getElementById("ws-label");
  const platformNameEl = document.getElementById("platform-name");

  // Logging Controls
  const btnToggleLogging = document.getElementById("btn-toggle-logging");
  const btnLoggingText = document.getElementById("btn-logging-text");
  const logStatusBadge = document.getElementById("log-status-badge");
  const toastBanner = document.getElementById("toast-banner");
  const toastMsg = document.getElementById("toast-msg");
  const toastIcon = document.getElementById("toast-icon");

  // Telemetry Elements
  const valCpuEl = document.getElementById("val-cpu");
  const valGpuEl = document.getElementById("val-gpu");
  const valNvdecEl = document.getElementById("val-nvdec");
  const valRamEl = document.getElementById("val-ram");
  const subRamMbEl = document.getElementById("sub-ram-mb");
  const subCpuCoresEl = document.getElementById("sub-cpu-cores");

  const gaugeCpuFill = document.getElementById("gauge-cpu-fill");
  const gaugeGpuFill = document.getElementById("gauge-gpu-fill");
  const gaugeNvdecFill = document.getElementById("gauge-nvdec-fill");
  const gaugeRamFill = document.getElementById("gauge-ram-fill");

  const valPowerEl = document.getElementById("val-power");
  const subPowerMwEl = document.getElementById("sub-power-mw");
  const valTempCpuEl = document.getElementById("val-temp-cpu");
  const valTempGpuEl = document.getElementById("val-temp-gpu");
  const valNvencEl = document.getElementById("val-nvenc");
  const valCsvPathEl = document.getElementById("val-csv-path");
  const valSamplesCountEl = document.getElementById("val-samples-count");
  const btnFullscreen = document.getElementById("btn-fullscreen");

  // Constants & State
  const CIRCUMFERENCE = 2 * Math.PI * 40; // 251.327
  let isLoggingActive = true;
  let benchmarkStart = Date.now();
  let sampleCount = 0;
  let toastTimeout = null;

  // Real-time timeline history buffer (max 60 points)
  const MAX_HISTORY = 60;
  const historyData = {
    cpu: [],
    gpu: [],
    nvdec: [],
    ram: []
  };

  // Toast notification helper
  function showToast(message, icon = "ℹ️") {
    if (!toastBanner || !toastMsg) return;
    toastMsg.textContent = message;
    if (toastIcon) toastIcon.textContent = icon;
    toastBanner.classList.add("show");

    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      toastBanner.classList.remove("show");
    }, 3500);
  }

  // Canvas setup
  const canvas = document.getElementById("telemetry-chart");
  const ctx = canvas.getContext("2d");

  function resizeCanvas() {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    renderChart();
  }

  window.addEventListener("resize", resizeCanvas);
  setTimeout(resizeCanvas, 100);

  // Live Clock Tick
  setInterval(() => {
    const now = new Date();
    if (liveClockEl) {
      liveClockEl.textContent = now.toTimeString().split(" ")[0];
    }
    // Update camera timecode watermarks
    const timeStr = now.toISOString().replace("T", " ").substring(0, 19);
    ["cam_01", "cam_02", "cam_03", "cam_04"].forEach(id => {
      const el = document.getElementById(`burnin-${id}`);
      if (el) el.textContent = timeStr;
    });

    // Update benchmark timer
    const elapsedSec = Math.floor((Date.now() - benchmarkStart) / 1000);
    const mins = String(Math.floor(elapsedSec / 60)).padStart(2, "0");
    const secs = String(elapsedSec % 60).padStart(2, "0");
    if (benchmarkTimerEl) {
      benchmarkTimerEl.textContent = `${mins}:${secs}`;
    }
  }, 1000);

  // Gauge Fill Helper
  function setGauge(fillElement, valElement, percentage, defaultColor = "#76b900") {
    const clamped = Math.max(0, Math.min(100, Number(percentage) || 0));
    const offset = CIRCUMFERENCE - (clamped / 100) * CIRCUMFERENCE;
    
    if (fillElement) {
      fillElement.style.strokeDashoffset = offset;
      if (clamped > 90) {
        fillElement.style.stroke = "#ef4444";
      } else if (clamped > 75) {
        fillElement.style.stroke = "#f59e0b";
      } else {
        fillElement.style.stroke = defaultColor;
      }
    }
    if (valElement) {
      valElement.textContent = clamped.toFixed(0);
    }
  }

  // Draw Line Chart on Canvas
  function renderChart() {
    if (!canvas || !ctx) return;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;

    ctx.clearRect(0, 0, w, h);

    // Draw grid lines
    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
    ctx.lineWidth = 1;

    for (let p of [0.25, 0.5, 0.75]) {
      const y = h * p;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    const n = historyData.cpu.length;
    if (n < 2) return;

    const step = w / (MAX_HISTORY - 1);
    const startX = w - (n - 1) * step;

    function plotSeries(data, color, fillGradientStart) {
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = startX + i * step;
        const y = h - (data[i] / 100) * (h - 4);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // Subtle fill
      ctx.lineTo(startX + (n - 1) * step, h);
      ctx.lineTo(startX, h);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, fillGradientStart);
      grad.addColorStop(1, "rgba(0, 0, 0, 0)");
      ctx.fillStyle = grad;
      ctx.fill();
    }

    plotSeries(historyData.ram, "#f59e0b", "rgba(245, 158, 11, 0.12)");
    plotSeries(historyData.nvdec, "#00f2fe", "rgba(0, 242, 254, 0.18)");
    plotSeries(historyData.gpu, "#38bdf8", "rgba(56, 189, 248, 0.15)");
    plotSeries(historyData.cpu, "#76b900", "rgba(118, 185, 0, 0.22)");
  }

  // Update Logging Button UI State
  function updateLoggingUI(active) {
    isLoggingActive = active;
    if (btnToggleLogging && btnLoggingText) {
      if (active) {
        btnToggleLogging.classList.remove("stopped");
        btnLoggingText.textContent = "STOP LOGGING";
      } else {
        btnToggleLogging.classList.add("stopped");
        btnLoggingText.textContent = "START LOGGING";
      }
    }
    if (logStatusBadge) {
      if (active) {
        logStatusBadge.classList.remove("stopped");
        logStatusBadge.textContent = "LOGGING ACTIVE";
      } else {
        logStatusBadge.classList.add("stopped");
        logStatusBadge.textContent = "LOGGING PAUSED";
      }
    }
  }

  // Toggle Logging Button Click Handler
  if (btnToggleLogging) {
    btnToggleLogging.addEventListener("click", async () => {
      const endpoint = isLoggingActive ? "/api/logging/stop" : "/api/logging/start";
      try {
        const resp = await fetch(endpoint, { method: "POST" });
        const res = await resp.json();
        if (isLoggingActive) {
          updateLoggingUI(false);
          showToast("CSV logging stopped. File safely closed for review.", "⏹️");
        } else {
          updateLoggingUI(true);
          showToast("CSV logging started. Writing telemetry samples...", "⏺️");
        }
      } catch (err) {
        console.error("Failed to toggle logging:", err);
        showToast("Error updating logging state", "⚠️");
      }
    });
  }

  // Telemetry Update Dispatcher
  function onTelemetrySample(data) {
    sampleCount++;
    const loggedCount = data.logged_samples !== undefined ? data.logged_samples : sampleCount;
    if (valSamplesCountEl) {
      valSamplesCountEl.textContent = `${loggedCount} SAMPLES LOGGED`;
    }

    if (data.is_logging !== undefined && data.is_logging !== isLoggingActive) {
      updateLoggingUI(data.is_logging);
    }

    const cpuPct = data.cpu_avg_pct || 0;
    const gpuPct = data.gpu_gr3d_pct || 0;
    const nvdecPct = data.nvdec_pct || 0;
    const ramPct = data.ram_pct || 0;

    // Update circular gauges
    setGauge(gaugeCpuFill, valCpuEl, cpuPct, "#76b900");
    setGauge(gaugeGpuFill, valGpuEl, gpuPct, "#38bdf8");
    setGauge(gaugeNvdecFill, valNvdecEl, nvdecPct, "#00f2fe");
    setGauge(gaugeRamFill, valRamEl, ramPct, "#f59e0b");

    // Cores info
    if (data.cpu_cores_pct && subCpuCoresEl) {
      const cores = data.cpu_cores_pct.split(";").length;
      subCpuCoresEl.textContent = `${cores} Cores Active`;
    }

    // RAM info
    if (subRamMbEl && data.ram_used_mb !== undefined) {
      subRamMbEl.textContent = `${Math.round(data.ram_used_mb)} / ${Math.round(data.ram_total_mb || 0)} MB`;
    }

    // Power
    if (valPowerEl && data.power_mw !== undefined) {
      const watts = (data.power_mw / 1000.0).toFixed(1);
      valPowerEl.innerHTML = `${watts} <span class="v-unit">W</span>`;
      if (subPowerMwEl) subPowerMwEl.textContent = `${Math.round(data.power_mw)} mW total`;
    }

    // Temperatures
    if (valTempCpuEl && data.temp_cpu_c !== undefined) {
      valTempCpuEl.innerHTML = `${data.temp_cpu_c.toFixed(1)} <span class="v-unit">°C</span>`;
    }
    if (valTempGpuEl && data.temp_gpu_c !== undefined) {
      valTempGpuEl.innerHTML = `${data.temp_gpu_c.toFixed(1)} <span class="v-unit">°C</span>`;
    }

    // NVENC
    if (valNvencEl && data.nvenc_pct !== undefined) {
      valNvencEl.innerHTML = `${data.nvenc_pct.toFixed(1)} <span class="v-unit">%</span>`;
    }

    // CSV path
    if (valCsvPathEl && data.csv_path) {
      valCsvPathEl.textContent = data.csv_path;
    }

    // Platform name
    if (platformNameEl && data.is_jetson_hardware) {
      platformNameEl.textContent = "NVIDIA Jetson (jtop Active)";
    }

    // Dynamic Camera Status Strip updates
    if (data.camera_status && typeof data.camera_status === "object") {
      const statusList = document.getElementById("cameras-status-list");
      if (statusList && statusList.children.length >= 10) {
        const camKeys = [
          "cam_01", "cam_02", "cam_03", "cam_04", "cam_05",
          "cam_06", "cam_07", "cam_08", "cam_09", "cam_10"
        ];
        camKeys.forEach((key, idx) => {
          const chip = statusList.children[idx];
          if (chip) {
            const st = data.camera_status[key];
            const dot = chip.querySelector(".chip-status-dot");
            if (st === "error" || st === "offline") {
              chip.classList.remove("active");
              chip.classList.add("error");
              if (dot) dot.className = "chip-status-dot offline";
            } else if (st === "online") {
              chip.classList.remove("error");
              chip.classList.add("active");
              if (dot) dot.className = "chip-status-dot rec";
            }
          }
        });
      }
    }

    // Append to history buffer
    historyData.cpu.push(cpuPct);
    historyData.gpu.push(gpuPct);
    historyData.nvdec.push(nvdecPct);
    historyData.ram.push(ramPct);

    if (historyData.cpu.length > MAX_HISTORY) {
      historyData.cpu.shift();
      historyData.gpu.shift();
      historyData.nvdec.shift();
      historyData.ram.shift();
    }

    renderChart();
  }

  // WebSocket Connection
  let ws = null;
  function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      if (wsStatusEl) {
        wsStatusEl.classList.add("connected");
        if (wsLabelEl) wsLabelEl.textContent = "LIVE";
      }
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        onTelemetrySample(payload);
      } catch (err) {
        console.error("Failed to parse telemetry:", err);
      }
    };

    ws.onclose = () => {
      if (wsStatusEl) {
        wsStatusEl.classList.remove("connected");
        if (wsLabelEl) wsLabelEl.textContent = "RECONNECTING";
      }
      setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  connectWebSocket();

  // Fullscreen Matrix Toggle
  if (btnFullscreen) {
    btnFullscreen.addEventListener("click", () => {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(err => {
          console.warn("Fullscreen request failed:", err);
        });
      } else {
        document.exitFullscreen().catch(err => {
          console.warn("Exit fullscreen failed:", err);
        });
      }
    });
  }

  // Stream auto-reconnect fallback
  ["cam_01", "cam_02", "cam_03", "cam_04"].forEach(id => {
    const img = document.getElementById(`stream-${id}`);
    if (img) {
      img.onerror = () => {
        setTimeout(() => {
          img.src = `/api/stream/${id}?retry=${Date.now()}`;
        }, 3000);
      };
    }
  });
});

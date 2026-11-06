#!/usr/bin/env python3
"""
Cardiac Monitor — Python Agent Mode  (the "BP-machine style" strap → wire → laptop flow)

HOW IT WORKS
------------
Tie the sensor strap on your arm / finger, connect the wire (USB serial, e.g. an
Arduino + AD8232 ECG or a pulse sensor), then start this file:

    python agent.py                 # auto-detect the strap; if none → demo simulator
    python agent.py --sim           # force demo simulator (no hardware needed)
    python agent.py --port COM3     # Windows: pick the strap port explicitly
    python agent.py --port /dev/ttyUSB0 --baud 115200
    python agent.py --plot          # also open a local matplotlib line window
    python agent.py --html "cardiac-monitor-dashboard_upgrade.html"

It starts a tiny local web server (default http://localhost:8777) that:
  * serves your dashboard HTML,
  * streams live vitals (HR, SpO2, HRV, signal quality),
  * computes the educational "heart-attack signal %" moment by moment,
  * feeds the LIVE LINE CHART in the "Live sensor agent" tab of the UI.

SERIAL PROTOCOL (what the strap sends, one message per line, 9600 baud default)
  W:512        raw waveform sample (0-1023 ADC, or already -1..1 floats)
  Q:0 / Q:1    signal quality flag (0 = electrode off / noisy, 1 = good)
  HR:72        heart rate in bpm, if your device computes it
  SpO2:98      oxygen saturation, if your device has it
  Also accepted: plain numbers ("72"), CSV ("72,98"), or "bpm:72 spo2:98"

If your device only streams W: samples, the agent detects beats itself and
derives BPM + HRV (RMSSD) from the R-peaks.

NOTES
  * Educational simulation — NOT a medical device and not a diagnosis.
  * A strap alone cannot "detect a heart attack"; the % is a teaching score
    built from rate, rhythm, HRV, SpO2 and signal quality (bands match the UI).
  * On your laptop: pip install -r requirements.txt   (both packages optional —
    without them you still get the web line graph + simulator).
"""

import argparse
import json
import math
import os
import random
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# Optional dependencies (the agent runs fine without them)
# --------------------------------------------------------------------------
try:
    import serial  # type: ignore
    import serial.tools.list_ports  # type: ignore
    HAS_SERIAL = True
except Exception:
    HAS_SERIAL = False

DEFAULT_PORT = 8777
DEFAULT_BAUD = 9600

BANDS = [
    (25, "Low signal", "#4CE0B3"),
    (50, "Moderate signal", "#E8A94C"),
    (75, "High signal", "#FF5C5C"),
    (101, "Emergency pattern", "#FF5C5C"),
]


def band_for(v: float):
    for mx, word, color in BANDS:
        if v < mx:
            return word, color
    return BANDS[-1][1], BANDS[-1][2]


def ema(prev: float, new: float, alpha: float = 0.18) -> float:
    if prev is None:
        return new
    return prev * (1 - alpha) + new * alpha


# --------------------------------------------------------------------------
# Shared live state (written by reader threads, read by HTTP/SSE + plot)
# --------------------------------------------------------------------------
LOCK = threading.Lock()

STATE = {
    "connected": False,          # True when a real strap is streaming
    "source": "simulator",       # "simulator" | "serial"
    "device": None,              # port name when serial
    "running": True,
    "t0": time.time(),
    "samples": 0,
    # live vitals
    "hr": 72.0,
    "spo2": 98.0,                # None when device cannot measure SpO2
    "hrv": 60.0,                 # RMSSD ms
    "quality": 0.9,              # 0..1
    # educational score
    "risk": 8.0,
    "band": "Low signal",
    "reasons": [],
    "emergency": False,
    # buffers
    "wave": [],                  # last ~400 waveform points, -1..1
    "history": [],               # [{t, risk, hr}] for the line chart backfill
}


def snapshot():
    with LOCK:
        word, _ = band_for(STATE["risk"])
        STATE["band"] = word
        return {
            "t": time.time(),
            "uptime": round(time.time() - STATE["t0"], 1),
            "connected": STATE["connected"],
            "source": STATE["source"],
            "device": STATE["device"],
            "samples": STATE["samples"],
            "hr": round(STATE["hr"], 1),
            "spo2": (round(STATE["spo2"], 1) if STATE["spo2"] is not None else None),
            "hrv": round(STATE["hrv"], 1),
            "quality": round(STATE["quality"], 2),
            "risk": round(STATE["risk"], 1),
            "band": word,
            "reasons": list(STATE["reasons"]),
            "emergency": bool(STATE["risk"] >= 75),
            "wave": list(STATE["wave"])[-300:],
        }


# --------------------------------------------------------------------------
# Risk engine — same bands as the HTML UI (0-24 low, 25-49 moderate,
# 50-74 high, 75-100 emergency). Physio signals only; educational.
# --------------------------------------------------------------------------
def compute_risk(hr, spo2, hrv, quality):
    raw = 6.0
    reasons = []

    # Heart rate
    if hr is not None:
        if hr >= 120:
            raw += 34;  reasons.append(f"Resting HR {hr:.0f} bpm (very elevated)")
        elif hr >= 100:
            raw += 24;  reasons.append(f"HR {hr:.0f} bpm (elevated)")
        elif hr >= 90:
            raw += 12;  reasons.append(f"HR {hr:.0f} bpm (borderline)")
        elif hr < 45:
            raw += 22;  reasons.append(f"HR {hr:.0f} bpm (very low)")
        elif hr < 55:
            raw += 8;   reasons.append(f"HR {hr:.0f} bpm (low)")
        else:
            reasons.append(f"HR {hr:.0f} bpm (normal range)")

    # SpO2 (skip silently when the strap cannot measure it)
    if spo2 is not None:
        if spo2 < 90:
            raw += 30;  reasons.append(f"SpO2 {spo2:.0f}% (critically low)")
        elif spo2 < 94:
            raw += 18;  reasons.append(f"SpO2 {spo2:.0f}% (low)")
        elif spo2 < 96:
            raw += 8;   reasons.append(f"SpO2 {spo2:.0f}% (borderline)")
        else:
            reasons.append(f"SpO2 {spo2:.0f}% (normal)")

    # Heart-rate variability — low HRV = strain
    if hrv is not None and hrv > 0:
        if hrv < 15:
            raw += 22;  reasons.append(f"HRV very low ({hrv:.0f} ms)")
        elif hrv < 30:
            raw += 12;  reasons.append(f"HRV low ({hrv:.0f} ms)")
        elif hrv < 50:
            raw += 5;   reasons.append(f"HRV moderate ({hrv:.0f} ms)")
        else:
            reasons.append(f"HRV good ({hrv:.0f} ms)")

    # Poor signal → cap confidence (don't let noise scream "emergency")
    if quality is not None and quality < 0.35:
        raw *= 0.55
        reasons.append("Poor signal quality — score discounted (fix the strap)")
    elif quality is not None and quality < 0.6:
        reasons.append("Signal quality mediocre — treat score cautiously")

    risk = max(0.0, min(100.0, raw))
    return risk, reasons


def push_metrics(hr, spo2, hrv, quality, emergency_flag=False):
    """Update STATE under lock: smooth risk, keep history, trim buffers."""
    risk, reasons = compute_risk(hr, spo2, hrv, quality)
    with LOCK:
        STATE["hr"] = hr if hr is not None else STATE["hr"]
        STATE["spo2"] = spo2
        STATE["hrv"] = hrv if hrv is not None else STATE["hrv"]
        STATE["quality"] = quality if quality is not None else STATE["quality"]
        STATE["risk"] = ema(STATE["risk"], risk, 0.22)
        STATE["reasons"] = reasons
        STATE["emergency"] = STATE["risk"] >= 75   # always derived from the smoothed score
        STATE["history"].append({
            "t": time.time(),
            "risk": round(STATE["risk"], 1),
            "hr": round(STATE["hr"], 1),
        })
        if len(STATE["history"]) > 3000:
            del STATE["history"][:1000]


def push_wave(chunk):
    with LOCK:
        STATE["wave"].extend(chunk)
        if len(STATE["wave"]) > 400:
            del STATE["wave"][: len(STATE["wave"]) - 400]
        STATE["samples"] += len(chunk)


# --------------------------------------------------------------------------
# Beat detection — derives BPM + HRV from a raw waveform window
# --------------------------------------------------------------------------
class BeatDetector:
    def __init__(self):
        self.prev = 0.0
        self.prev2 = 0.0
        self.above = False
        self.last_beat_t = None
        self.ibis = deque(maxlen=24)   # inter-beat intervals (ms)

    def feed(self, samples, dt, t_wall=None):
        """Returns number of beats found in this chunk (mutates ibis)."""
        if t_wall is None:
            t_wall = time.time()
        beats = 0
        # simple adaptive threshold on rising slope crossing
        thr = 0.45
        for v in samples:
            rising = self.prev < thr <= v
            refractory_ok = (self.last_beat_t is None) or ((t_wall - self.last_beat_t) > 0.25)
            if rising and refractory_ok:
                if self.last_beat_t is not None:
                    ibi = (t_wall - self.last_beat_t) * 1000.0
                    if 250 <= ibi <= 3000:
                        self.ibis.append(ibi)
                self.last_beat_t = t_wall
                beats += 1
            self.prev2, self.prev = self.prev, v
        return beats

    def hr_hrv(self):
        if not self.ibis:
            return None, None
        mean_ibi = sum(self.ibis) / len(self.ibis)
        hr = 60000.0 / mean_ibi if mean_ibi > 0 else None
        hrv = None
        if len(self.ibis) >= 4:
            diffs = [self.ibis[i] - self.ibis[i - 1] for i in range(1, len(self.ibis))]
            rmssd = math.sqrt(sum(d * d for d in diffs) / len(diffs))
            hrv = rmssd
        return hr, hrv


# --------------------------------------------------------------------------
# Simulator — the demo "strap" so the line graph moves without hardware
# --------------------------------------------------------------------------
def synth_ecg_phase(phase):
    """One ECG complex in [-1, ~1.2] for phase in [0,1)."""
    def g(c, w, a):
        d = (phase - c) / w
        return a * math.exp(-d * d)
    return (g(0.18, 0.035, 0.12)   # P
            + g(0.40, 0.012, -0.16)  # Q
            + g(0.44, 0.012, 1.0)    # R
            + g(0.48, 0.014, -0.28)  # S
            + g(0.66, 0.05, 0.28))   # T


def run_simulator(stop_evt: threading.Event):
    """75 s scenario loop: calm → ramp → elevated → recovery (nice line chart)."""
    phase = 0.0
    metrics_t = 0.0
    hr, spo2, hrv = 72.0, 98.0, 65.0
    quality = 0.92
    emergency_flag = False
    rng = random.Random(42)

    while not stop_evt.is_set():
        t = time.time()
        cycle = (t % 75.0)
        if cycle < 25:        # calm
            target_hr, target_spo2, target_hrv, q = 72, 98, 65, 0.93
            emergency_flag = False
        elif cycle < 40:      # ramp up
            k = (cycle - 25) / 15.0
            target_hr = 72 + k * 40
            target_spo2 = 98 - k * 4.5
            target_hrv = 65 - k * 48
            q = 0.9 - k * 0.25
            emergency_flag = False
        elif cycle < 60:      # elevated / demo emergency band
            target_hr = 112 + 8 * math.sin(t * 0.7)
            target_spo2 = 93
            target_hrv = 12
            q = 0.62 + 0.08 * math.sin(t * 1.3)
            emergency_flag = (cycle > 47 and cycle < 56)
        else:                 # recovery
            k = (cycle - 60) / 15.0
            target_hr = 112 - k * 40
            target_spo2 = 93 + k * 5
            target_hrv = 12 + k * 53
            q = 0.7 + k * 0.23
            emergency_flag = False

        hr = ema(hr, target_hr + rng.uniform(-1.5, 1.5), 0.25)
        spo2 = ema(spo2, target_spo2 + rng.uniform(-0.4, 0.4), 0.25)
        hrv = max(4.0, ema(hrv, target_hrv + rng.uniform(-2, 2), 0.2))
        quality = max(0.15, min(1.0, ema(quality, q, 0.3)))

        # waveform @ ~50 Hz (2 samples per 40 ms tick)
        rr = 60.0 / max(hr, 30.0)
        chunk = []
        for _ in range(2):
            phase = (phase + (0.04 / 2) / rr) % 1.0
            v = synth_ecg_phase(phase)
            if quality < 0.75:
                v += rng.uniform(-0.25, 0.25) * (1.2 - quality)
            chunk.append(max(-1.2, min(1.2, v)))
        push_wave(chunk)

        metrics_t += 0.04
        if metrics_t >= 0.5:
            metrics_t = 0.0
            push_metrics(hr, spo2, hrv, quality, emergency_flag)

        time.sleep(0.04)


# --------------------------------------------------------------------------
# Serial strap reader
# --------------------------------------------------------------------------
def pick_port():
    if not HAS_SERIAL:
        return None
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None
    # prefer known USB-serial adapters (Arduino, CH340, CP210x, FTDI)
    priority = {0x2341: 0, 0x2A03: 0, 0x1A86: 1, 0x10C4: 1, 0x0403: 1, 0x1B4F: 1}
    ports.sort(key=lambda p: priority.get(p.vid, 9) if p.vid else 5)
    return ports[0].device


def parse_line(line: str):
    """Return ('wave', v) | ('q', 0/1) | ('metrics', dict) | None."""
    s = line.strip()
    if not s:
        return None
    low = s.lower()
    try:
        if low.startswith("w:"):
            v = float(s.split(":", 1)[1])
            if abs(v) > 3.0:            # raw ADC 0..1023 → -1..1
                v = (v - 512.0) / 512.0
            return ("wave", max(-1.5, min(1.5, v)))
        if low.startswith("q:"):
            return ("q", int(float(s.split(":", 1)[1])))
    except ValueError:
        return None

    # key:value style:  HR:72 SpO2:98  /  bpm:72 spo2:98
    metrics = {}
    if ":" in s or "=" in s:
        for part in s.replace(",", " ").split():
            if ":" in part:
                k, v = part.split(":", 1)
            elif "=" in part:
                k, v = part.split("=", 1)
            else:
                continue
            k = k.strip().lower()
            try:
                fv = float(v)
            except ValueError:
                continue
            if k in ("hr", "bpm", "heart", "heart_rate"):
                metrics["hr"] = fv
            elif k in ("spo2", "sao2", "o2", "oxygen"):
                metrics["spo2"] = fv
            elif k in ("ibi", "rr", "ms"):
                if fv > 0:
                    metrics["hr"] = 60000.0 / fv if fv > 200 else metrics.get("hr")
        if metrics:
            return ("metrics", metrics)

    # bare number → HR if plausible, else waveform sample
    if "," in s:
        # CSV "72,98" → hr, spo2
        try:
            parts = [float(x) for x in s.split(",")]
        except ValueError:
            return None
        m = {}
        if parts and 25 <= parts[0] <= 230:
            m["hr"] = parts[0]
        if len(parts) > 1 and 50 <= parts[1] <= 100:
            m["spo2"] = parts[1]
        return ("metrics", m) if m else None
    try:
        v = float(s)
    except ValueError:
        return None
    if 25 <= v <= 230:
        return ("metrics", {"hr": v})
    if abs(v) > 3.0 and 0 <= v <= 1023:
        return ("wave", (v - 512.0) / 512.0)
    if abs(v) <= 1.5:
        return ("wave", v)
    return None


def run_serial(port: str, baud: int, stop_evt: threading.Event):
    """Read the strap; if it only streams waveform, derive HR/HRV ourselves."""
    try:
        ser = serial.Serial(port, baud, timeout=0.4)
    except Exception as e:
        print(f"[agent] could not open {port}: {e} — falling back to simulator",
              file=sys.stderr)
        STATE["source"] = "simulator"
        STATE["connected"] = False
        run_simulator(stop_evt)
        return

    with LOCK:
        STATE["source"] = "serial"
        STATE["device"] = port
        STATE["connected"] = True
    print(f"[agent] strap connected on {port} @ {baud} baud")

    det = BeatDetector()
    device_hr = None
    device_spo2 = None
    qflag = 1
    metrics_t = 0.0
    wave_ring = deque(maxlen=250)   # ~2–5 s window
    last_status = time.time()

    while not stop_evt.is_set():
        try:
            raw = ser.readline()
        except Exception as e:
            print(f"[agent] serial read error: {e}", file=sys.stderr)
            break
        now = time.time()
        if not raw:
            # timeout — if device went quiet, note it but keep last values
            if now - last_status > 10:
                print("[agent] waiting for data from strap…", file=sys.stderr)
                last_status = now
            continue
        try:
            line = raw.decode("utf-8", "ignore")
        except Exception:
            continue
        msg = parse_line(line)
        if not msg:
            continue
        kind, val = msg

        if kind == "q":
            qflag = val
        elif kind == "metrics":
            if "hr" in val:
                device_hr = val["hr"]
            if "spo2" in val:
                device_spo2 = val["spo2"]
        elif kind == "wave":
            push_wave([val])
            wave_ring.append(val)
            det.feed([val], dt=0.004, t_wall=now)

        metrics_t += 0.04
        if metrics_t >= 0.5:
            metrics_t = 0.0
            derived_hr, derived_hrv = det.hr_hrv()
            hr = device_hr if device_hr is not None else derived_hr
            hrv = derived_hrv
            # quality: flag + whether we actually see beats
            beats = len(det.ibis)
            q = (0.3 if qflag == 0 else 1.0)
            if beats < 2:
                q *= 0.45
            elif beats >= 4:
                q = min(1.0, q + 0.1)
            push_metrics(hr, device_spo2, hrv, q)

    try:
        ser.close()
    except Exception:
        pass
    with LOCK:
        STATE["connected"] = False
    print("[agent] strap disconnected")
    if not stop_evt.is_set():
        run_simulator(stop_evt)   # keep the graph alive with demo data


# --------------------------------------------------------------------------
# HTTP + Server-Sent Events
# --------------------------------------------------------------------------
def find_html(cli_html: str | None) -> Path:
    here = Path(__file__).resolve().parent
    name = "cardiac-monitor-dashboard_upgrade.html"
    candidates = [
        Path(cli_html) if cli_html else None,
        here / name,
        here / "uploads" / name,
        Path.cwd() / name,
        Path.cwd() / "uploads" / name,
    ]
    for c in candidates:
        if c and c.is_file():
            return c
    return here / name   # will 404 with a helpful message


HTML_PATH: Path = find_html(None)


class AgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CardiacAgent/1.0"

    def log_message(self, fmt, *args):   # quieter logs
        pass

    # ---- helpers ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_html()
        if path == "/api/status":
            return self._json(snapshot())
        if path == "/api/history":
            with LOCK:
                hist = list(STATE["history"])[-800:]
            return self._json({"history": hist})
        if path == "/api/stream":
            return self._sse()
        self._json({"error": "not found"}, 404)

    def _serve_html(self):
        try:
            body = HTML_PATH.read_bytes()
        except OSError:
            body = (b"<!doctype html><p>Dashboard HTML not found. Start agent.py "
                    b"from the folder that contains cardiac-monitor-dashboard_upgrade.html "
                    b"or pass --html &lt;path&gt;.</p>")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        """Live stream → the line chart in the UI (one JSON event / 250 ms)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        try:
            while STATE["running"]:
                payload = json.dumps(snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# --------------------------------------------------------------------------
# Optional local matplotlib window (--plot)
# --------------------------------------------------------------------------
def run_plot(stop_evt: threading.Event):
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except Exception as e:
        print(f"[agent] --plot unavailable ({e}); web graph still works.",
              file=sys.stderr)
        return

    fig, (ax_w, ax_r) = plt.subplots(2, 1, figsize=(9, 6))
    fig.suptitle("Cardiac Monitor — live agent")
    plt.tight_layout()

    def _frame(_):
        snap = snapshot()
        ax_w.clear()
        ax_w.plot(snap["wave"], color="#4CE0B3", lw=1.2)
        ax_w.set_xlim(0, 300)
        ax_w.set_ylim(-1.3, 1.3)
        ax_w.set_title(f"Rhythm strip — {snap['source']}"
                       + (f" ({snap['device']})" if snap["device"] else ""))
        ax_r.clear()
        hist = STATE["history"][-300:]
        ys = [h["risk"] for h in hist] or [snap["risk"]]
        ax_r.plot(ys, color="#4CE0B3", lw=2)
        ax_r.axhline(75, color="#FF5C5C", ls="--", lw=1)
        ax_r.set_ylim(0, 100)
        ax_r.set_title(f"Heart-attack signal % — now {snap['risk']:.0f}% ({snap['band']})")
        return []

    FuncAnimation(fig, _frame, interval=250, cache_frame_data=False)
    plt.show()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    global HTML_PATH
    ap = argparse.ArgumentParser(description="Cardiac Monitor Python agent (strap → live line graph)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="web server port")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--serial-port", "--device", dest="serial_port", default=None,
                    help="serial device, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--port-alias", dest="port_alias", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default 9600)")
    ap.add_argument("--sim", action="store_true", help="force demo simulator (no hardware)")
    ap.add_argument("--plot", action="store_true", help="open a local matplotlib line window")
    ap.add_argument("--html", default=None, help="path to the dashboard HTML file")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    # backward-friendly alias: allow `--port COM3` style? no — device uses --serial-port
    device = args.serial_port
    if device and device.upper().startswith(("COM", "/DEV")):
        pass  # explicit device string
    HTML_PATH = find_html(args.html)

    stop_evt = threading.Event()

    # choose data source
    port = None
    if args.sim:
        print("[agent] simulator forced (--sim)")
    elif device:
        port = device
    elif HAS_SERIAL:
        port = pick_port()
        if port:
            print(f"[agent] auto-detected strap on {port}")
        else:
            print("[agent] no serial device found — using demo simulator")
    else:
        print("[agent] pyserial not installed (pip install pyserial) — using demo simulator")

    if port:
        t = threading.Thread(target=run_serial, args=(port, args.baud, stop_evt), daemon=True)
    else:
        t = threading.Thread(target=run_simulator, args=(stop_evt,), daemon=True)
    t.start()

    if args.plot:
        threading.Thread(target=run_plot, args=(stop_evt,), daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 62, flush=True)
    print("  Cardiac Monitor — Python agent is running", flush=True)
    print(f"  Dashboard + live line graph : {url}", flush=True)
    print("  Open that page → click  'Live sensor agent'", flush=True)
    print("  Tie the strap, plug the wire, watch the graph.", flush=True)
    print("  Ctrl+C to stop.  (educational demo — not a medical device)", flush=True)
    print("=" * 62, flush=True)
    if not args.no_open:
        def _open():
            time.sleep(0.6)   # let the server bind first
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent] shutting down…")
    finally:
        stop_evt.set()
        STATE["running"] = False
        try:
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

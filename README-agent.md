# Cardiac Monitor — Live Sensor Agent (Python)

BP-machine style flow: **strap on the arm → wire into the laptop → run `agent.py` → live heart-attack % line graph** in the dashboard.

> Educational simulation only — **not a medical device**, not a diagnosis.

## Files

| File | What it is |
|---|---|
| `cardiac-monitor-dashboard_upgrade.html` | Your dashboard with **3 UIs**: Simple dashboard · Problem matcher · **Live sensor agent** |
| `agent.py` | The Python agent — reads the strap (or a demo simulator), computes the live %, streams to the UI |
| `strap_node.ino` | Tiny Arduino sketch for the strap node (AD8232 ECG or pulse sensor → serial) |
| `requirements.txt` | Optional extras (`pyserial`, `matplotlib`) |

## Quick start — full code on localhost (no separate app)

```bash
python agent.py
```

That's the whole command. It:

1. Opens your browser on **http://localhost:8777** automatically (`--no-open` to skip)
2. Shows the **logo intro for ~5 seconds** (tap anywhere to skip), then the menu — all three UIs (your original, Problem matcher, Live sensor) live in that one page
3. Auto-detects a strap on USB — none attached? it silently uses the demo simulator
4. The **Live sensor agent** tab auto-connects to the local agent (no Connect button needed when you're on localhost)

Want only the demo line without touching hardware flags:

```bash
python agent.py --sim     # force simulator, same localhost page
```

Open **http://localhost:8777/** → click **Live sensor agent** → watch the green line move.
The simulator runs a 75 s scenario loop (calm → elevated → recovery) so the chart visibly changes.

> There is no installable app and nothing outside the browser — `agent.py` is just a tiny localhost server + strap reader, and the dashboard is plain HTML it serves.

## With the strap (hardware)

1. Flash `strap_node.ino` to an Arduino (UNO/Nano, etc.).
2. Wire an **AD8232 ECG** (signal → A0, LO- → D10, LO+ → D11) **or** a pulse/PPG sensor (signal → A0).
3. Tie the strap on your arm / clip the sensor, plug USB into the laptop.
4. ```bash
   pip install -r requirements.txt   # installs pyserial (optional matplotlib)
   python agent.py                   # auto-detects the port
   # or pick it explicitly:
   python agent.py --serial-port COM3            # Windows
   python agent.py --serial-port /dev/ttyUSB0    # Linux
   python agent.py --baud 115200                 # if your sketch uses 115200
   ```
5. The UI badge switches from **simulator** to **strap · COMx** when real data flows.
   No SpO₂ sensor? That tile just shows `—` and the score uses rate/rhythm/HRV only.

## Extra modes

```bash
python agent.py --plot      # also opens a local matplotlib line window
python agent.py --port 9000 # web server on a different port
python agent.py --html path/to/dashboard.html
python agent.py --help
```

## Serial protocol (for your own firmware)

One message per line, default **9600 baud**:

```
W:512      raw waveform sample (ADC 0..1023, or float -1..1)
Q:1        signal quality (1 = good, 0 = electrode off)
HR:72      optional — heart rate if your board computes it
SpO2:98    optional — oxygen saturation
```

Plain numbers and CSV (`72,98`) are also accepted. If you only stream `W:` samples, the agent finds R-peaks itself and derives **BPM + HRV (RMSSD)**.

## How the % is computed (teaching score)

Same bands as the UI: **0–24 low · 25–49 moderate · 50–74 high · 75–100 emergency.**

- Heart rate (very high / very low adds more)
- SpO₂ below 96 %
- Low HRV (strain)
- Poor signal quality → score is *discounted*, not inflated
- Smoothed with an EMA so the line is steady; history kept for the chart + CSV export

A strap alone cannot detect a heart attack — in the app this % sits next to the **Problem matcher** (symptoms) and the **Simple dashboard** (manual check) so you can compare all three.

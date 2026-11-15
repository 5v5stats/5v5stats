"""Probe choppy captures: record 15 s of demo playback while sampling CS2's own frame rate (telemetry overlay),
GPU load and the CS2 window state, to tell a slow game apart from a starved capture."""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render  # noqa: E402
import settings_guard  # noqa: E402

DEMO = sys.argv[1]
TICK = int(sys.argv[2])
OUT = sys.argv[3]
os.makedirs(OUT, exist_ok=True)
samples = []
stop = threading.Event()


def sampler():
    u32 = render.user32
    while not stop.is_set():
        hwnd = u32.FindWindowW(None, render.WINDOW_TITLE)
        fg = u32.GetForegroundWindow()
        smi = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,clocks.gr,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
        samples.append({"t": round(time.time() - t0, 1), "gpu": smi, "iconic": bool(hwnd and u32.IsIconic(hwnd)),
                        "visible": bool(hwnd and u32.IsWindowVisible(hwnd)), "cs2_foreground": fg == hwnd})
        time.sleep(1)


orig = settings_guard.snapshot()
con = render.Console()
t0 = time.time()
try:
    render.launch_cs2(1920, 1080)
    render.load_demo(con, DEMO)
    con.send("cl_hud_telemetry_frametime_show 2", "demo_pause", f"demo_gototick {TICK}")
    con.wait_for("Demo Skipping finished", 30)
    time.sleep(2)
    threading.Thread(target=sampler, daemon=True).start()
    cap = render.capture(os.path.join(OUT, "probe.mp4"), 60, 1080, 15)
    time.sleep(0.5)
    con.send("demo_resume")
    cap.wait(60)
    con.send("demo_pause")
    stop.set()
    print("live fps (new frames/s after 1 s):", round(render.live_fps(cap, 1.0) or 0, 1))
    print("unique fps (decoded):", round(render.unique_fps(os.path.join(OUT, "probe.mp4")), 1))
    for t in (2, 5, 8, 11, 14):
        subprocess.run([render.FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i", os.path.join(OUT, "probe.mp4"),
                        "-frames:v", "1", os.path.join(OUT, f"f{t:02d}.jpg")])
    json.dump(samples, open(os.path.join(OUT, "samples.json"), "w"), indent=0)
    for s in samples:
        print(s)
finally:
    stop.set()
    con.send("cl_hud_telemetry_frametime_show 0") if con.s else None
    render.close_cs2(con, orig)
    print("restored:", settings_guard.restore_pending())

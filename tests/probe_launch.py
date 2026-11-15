"""Probe: launch CS2 the way render.py does and report whether the console port opens."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render  # noqa: E402
import settings_guard  # noqa: E402

orig = settings_guard.snapshot()
con = render.Console()
try:
    t0 = time.time()
    render.launch_cs2(1920, 1080)
    print(f"window after {time.time() - t0:.0f} s; titles: {[t for t in render.window_titles() if 'Counter' in t or 'Steam' in t]}")
    for i in range(24):
        ns = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        listening = [l.strip() for l in ns.splitlines() if ":2121" in l and "LISTENING" in l]
        cmd = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Process -Filter \"name='cs2.exe'\").CommandLine"],
                             capture_output=True, text=True).stdout.strip()
        if i == 0 or listening:
            print(f"t+{time.time() - t0:.0f}s cmdline: {cmd!r}  port 2121: {listening or 'not listening'}")
        if listening:
            break
        time.sleep(5)
    print("other windows:", [t for t in render.window_titles() if t and len(t) < 80][:25])
finally:
    render.close_cs2(con, orig)
    print("restored:", settings_guard.restore_pending())

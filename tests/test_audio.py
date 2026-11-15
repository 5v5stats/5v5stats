import os, sys, subprocess, time, wave, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render, settings_guard
PC = os.path.join(render.ROOT, "tools", "proccap.exe")
DIGITAL = "{0.0.0.00000000}.{4D39EF1E-3C3F-41E2-A541-766DA69BEFF0}"
def cs2_pid():
    out = subprocess.run(["tasklist", "/fi", "imagename eq cs2.exe", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return int(out.split(",")[1].strip('"'))
def rms(path):
    w = wave.open(path); d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
    return round(float(np.sqrt((d**2).mean())), 1), w.getnframes() / w.getframerate()
def pc(*a): r = subprocess.run([PC, *map(str, a)], capture_output=True, text=True); return (r.stdout + r.stderr).strip()
orig = settings_guard.snapshot(); con = render.Console(); pid = None
try:
    render.launch_cs2(1920, 1080); pid = cs2_pid()
    print("route ->", pc("route", pid, DIGITAL))
    render.load_demo(con, "de_anubis_2026-11-06_07-27-49")
    con.send('volume "0.6"', 'snd_mute_losefocus "false"', "demo_gototick 117800"); con.wait_for("Demo Skipping finished", 30)
    con.send("soundinfo"); print([l for l in con.read(2).splitlines() if "SDL Audio" in l])
    con.send("demo_resume"); time.sleep(1)
    print(pc("capture", pid, 6, "tests/a_routed.wav")); con.send("demo_pause")
    print("routed rms/len:", rms("tests/a_routed.wav"))
finally:
    if pid: print("route back ->", pc("route", pid, "default"))
    render.close_cs2(con, orig); print("restored:", settings_guard.restore_pending())

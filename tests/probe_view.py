"""Probe: jump to each clip of a demo and grab a frame (checks viewmodel / end screen).
Usage: python tests/probe_view.py <demo name in csgo> <clips.json> <out dir>"""
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render, settings_guard  # noqa: E402

name, clips_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out, exist_ok=True)
clips = sorted(json.load(open(clips_json, encoding="utf-8")), key=lambda c: c["segments"][0][0])
orig = settings_guard.snapshot()
con = render.Console()
try:
    render.launch_cs2(1920, 1080)
    render.load_demo(con, name)
    con.send("cl_drawviewmodel", "viewmodel_fov", "r_drawviewmodel")
    print("cvars:", [l for l in con.read(2).splitlines() if "viewmodel" in l])
    for c in clips:
        start = c["segments"][0][0]
        spec = f'spec_player "{c["player"]}"'
        con.send(f"demo_gototick {start - 128}"); con.wait_for("Demo Skipping finished", 30)
        con.send("demo_pause", spec, f"demo_gototick {start}"); con.wait_for("Demo Skipping finished", 30)
        con.send("demo_pause", spec); time.sleep(2)
        con.send("demo_resume"); time.sleep(3)
        p = os.path.join(out, f"R{c['round']}.mp4")
        cap = render.capture(p, 30, 540, 1.5); cap.wait(30)
        con.send("demo_pause")
        subprocess.run([render.FFMPEG, "-v", "error", "-y", "-ss", "1", "-i", p, "-frames:v", "1",
                        os.path.join(out, f"R{c['round']}.jpg")])
        print("grabbed", c["round"], c["player"])
finally:
    render.close_cs2(con, orig)
    print("restored:", settings_guard.restore_pending())

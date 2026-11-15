"""Probe: does a given player's viewmodel show, in several rounds, and do re-spectate tricks fix it?"""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render, settings_guard  # noqa: E402
from demoparser2 import DemoParser

name, player, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out, exist_ok=True)
csgo = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo"
fe = DemoParser(os.path.join(csgo, name + ".dem")).parse_event("round_freeze_end")["tick"].tolist()
fe = [t for t in fe if t > 0]
picks = {"r5": fe[4] + 64 * 8, "r15": fe[14] + 64 * 8, "r22": fe[21] + 64 * 8}
orig = settings_guard.snapshot()
con = render.Console()


def grab(tag):
    p = os.path.join(out, tag + ".mp4")
    cap = render.capture(p, 30, 540, 1.2); cap.wait(30)
    subprocess.run([render.FFMPEG, "-v", "error", "-y", "-ss", "0.8", "-i", p, "-frames:v", "1",
                    os.path.join(out, tag + ".jpg")])


try:
    render.launch_cs2(1920, 1080)
    render.load_demo(con, name)
    spec = f'spec_player "{player}"'
    for tag, t in picks.items():
        con.send(f"demo_gototick {t}"); con.wait_for("Demo Skipping finished", 30)
        con.send("demo_pause", spec); time.sleep(1); con.send("demo_resume"); time.sleep(2)
        grab(tag)
    # remedies at r22
    con.send("spec_next"); time.sleep(1); con.send(spec); time.sleep(2); grab("r22_respec")
    con.send("spec_mode 5"); time.sleep(1); con.send("spec_mode 4", spec); time.sleep(2); grab("r22_mode")
    con.send("slot2"); time.sleep(1); grab("r22_slot")
    con.send("demo_pause")
finally:
    render.close_cs2(con, orig)
    print("done")

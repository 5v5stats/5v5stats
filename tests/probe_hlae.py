"""Test HLAE with the current CS2: launch, load demo, survive playback, optionally record a clip.
Usage: python tests/probe_hlae.py <hlae folder> [--record]"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio  # noqa: E402
import render  # noqa: E402
import settings_guard  # noqa: E402

HLAE_DIR = sys.argv[1]
RECORD = "--record" in sys.argv
CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
DEMO = "de_anubis_2026-11-06_07-27-49"
OUT = r"C:\cs2render\output\hlae_test"
CLIP = (117987, 119381)  # "!" round 18 4K
PLAYER = "!"


def launch_hlae():
    cmdline = "-insecure -novid -console -sw -width 1920 -height 1080 -netconport 2121"
    args = [os.path.join(HLAE_DIR, "HLAE.exe"), "-noGui", "-autoStart", "-noConfig", "-afxDisableSteamStorage",
            "-customLoader", "-hookDllPath", os.path.join(HLAE_DIR, "x64", "AfxHookSource2.dll"),
            "-programPath", CS2, "-cmdLine", cmdline]
    subprocess.Popen(args)


def crash_dumps():
    d = os.path.dirname(CS2)
    return {f for f in os.listdir(d) if f.endswith(".mdmp")}


orig = settings_guard.snapshot()
con = render.Console()
spare = audio.pick_spare()
dumps_before = crash_dumps()
result = "unknown"
try:
    prev = render.user32.GetForegroundWindow()
    launch_hlae()
    print("HLAE launched")
    pid, routed, t0 = None, False, time.time()
    while time.time() - t0 < 120 and not routed:
        pid = audio.cs2_pid()
        if pid and spare:
            routed, _ = audio.route(pid, spare[0])
        time.sleep(0.5)
    hwnd = 0
    while time.time() - t0 < 120 and not hwnd:
        hwnd = render.user32.FindWindowW(None, render.WINDOW_TITLE)
        time.sleep(1)
    time.sleep(3)
    render.send_to_back(hwnd, prev)
    print("CS2 window up, audio routed:", routed)
    con.connect(120)
    con.wait_ready(120)
    con.send("mirv_streams record"); print("mirv:", "OK" if "mirv_streams record" in con.read(2) else "?")
    time.sleep(3)
    con.send("demo_ui_mode 0", f"playdemo {DEMO}")
    print("playdemo sent")
    time.sleep(10)
    con.connect(120)
    con.wait_ready(120)
    con.send("demo_pause", f"demo_gototick {CLIP[0] - 200}")
    con.wait_for("Demo Skipping finished", 40)
    con.send("demo_resume")
    for i in range(6):  # 30 s of playback
        time.sleep(5)
        if not render.cs2_running():
            raise RuntimeError(f"CS2 crashed after {5 * (i + 1)} s of playback")
    con.send("demo_pause")
    print("Survived load + 30 s of playback")
    result = "plays"

    if RECORD:
        os.makedirs(OUT, exist_ok=True)
        out = OUT.replace("\\", "/")
        con.send(*[f'{k} "{v}"' for k, v in settings_guard.TOUCHED.items()],
                 'volume "0.6"', 'snd_mute_losefocus "false"', "cl_trueview_show_status 0",
                 f'mirv_streams record name "{out}"', "mirv_streams record screen enabled 1",
                 "mirv_streams record screen settings afxFfmpegYuv420p",
                 "mirv_streams record fps 60", "mirv_streams record startMovieWav 1",
                 "mirv_cmd clear",
                 f'mirv_cmd addAtTick {CLIP[0]} "mirv_streams record start"',
                 f'mirv_cmd addAtTick {CLIP[1]} "mirv_streams record end; demo_pause; echo CS2R_REC_DONE"')
        con.send(f"demo_gototick {CLIP[0] - 128}")
        con.wait_for("Demo Skipping finished", 40)
        con.send("demo_pause", f'spec_player "{PLAYER}"')
        time.sleep(2)
        con.send(f'spec_player "{PLAYER}"', "demo_resume")
        t_rec = time.time()
        done = con.wait_for("CS2R_REC_DONE", 240)
        print(f"Recording {'finished' if done else 'TIMED OUT'} in {time.time() - t_rec:.1f} s "
              f"for a {(CLIP[1] - CLIP[0]) / 64:.1f} s clip")
        time.sleep(5)
        result = "recorded" if done else "record-timeout"
except Exception as e:
    result = f"FAILED: {e}"
finally:
    try:
        if audio.route_pending():
            p = audio.cs2_pid()
            if p:
                audio.route(p, "default")
    except Exception:
        pass
    render.close_cs2(con, orig)
    if audio.route_pending():
        render.repair_audio_route()
    print("settings restored:", settings_guard.restore_pending(), "| route marker:", audio.route_pending())
    new_dumps = crash_dumps() - dumps_before
    print("new crash dumps:", sorted(new_dumps) or "none")
    print("RESULT:", result)

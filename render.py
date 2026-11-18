r"""Render CS2 demo highlights to MP4 clips + a highlight reel, in the background.

Usage examples:
  python render.py "C:\Users\me\Downloads\match.dem"                 # top 5 highlights, all players
  python render.py match.dem --player "!" --max-clips 10              # one player
  python render.py a.dem b.dem --min-score 150                        # several demos, only 4K/ace/big clutches
  python render.py match.dem --list                                   # just list highlights, record nothing

How it works: find highlights (highlights.py) -> snapshot your CS2 settings -> launch CS2 (-insecure, muted,
pushed behind your windows) -> for each highlight segment: jump to tick, spectate the player, capture the CS2
window with FFmpeg (Windows Graphics Capture + NVENC) -> check frame rate (retry if choppy) -> add a title ->
join segments -> highlight reel -> close CS2 and restore your exact settings.
"""
import argparse
import concurrent.futures
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import audio
import settings_guard
import sync
from highlights import find_highlights, TICKRATE

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(ROOT, "hlae", "ffmpeg", "bin", "ffmpeg.exe")
STEAM = r"C:\Program Files (x86)\Steam\steam.exe"
CSGO_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo"
FONT = "/Windows/Fonts/arialbd.ttf"  # drive-less path: avoids ':' escaping in the filter graph
FONT_CJK = "/Windows/Fonts/YuGothB.ttc"  # for names with Japanese/Chinese/Korean characters (Arial has none)


def title_font(text):
    if any(ord(ch) > 0x24F for ch in text) and os.path.exists("C:" + FONT_CJK):
        return FONT_CJK
    return FONT
PORT = 2121
AUDIO_START_LATENCY = 0.03  # seconds from starting proccap to its first captured sample
VIDEO_PIPELINE_LATENCY = 0.22  # fallback only: capture+encoder delay before FFmpeg reports a frame
AV_FINE_TUNE = 0.02  # frames reach the screen ~2 frames after the sound (measured on 94 shots)
# From sending demo_resume to the game's sound / moving picture in the captures (measured 2026-11-17 on 9 takes:
# sound 0.05-0.12 s, picture 0.20-0.23 s). The clock gives the resume moment even when the game stays silent
# or still for a while after resuming; the first sound / movement only refines it when it's close.
RESUME_SOUND_LATENCY = 0.06
RESUME_VIDEO_LATENCY = 0.215
ONSET_AGREE = 0.08  # the detected first sound / movement counts only this close to the clock's moment
PRE_ROLL = 1.5        # seconds captured before the demo resumes (trimmed off)
MIN_FPS_RATIO = 0.85  # re-record a segment if fewer unique frames than this share of the target fps
WINDOW_TITLE = "Counter-Strike 2"
# final clips: high quality but shareable size (~8-14 Mbps at 1080p60)
FINAL_ENCODE = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                "-maxrate", "16M", "-bufsize", "32M", "-pix_fmt", "yuv420p"]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class RenderError(Exception):
    pass


# ---------------------------------------------------------------- CS2 console (netcon) ----

class Console:
    def __init__(self, port=PORT):
        self.port, self.s = port, None

    def connect(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
                self.s.settimeout(0.2)
                return
            except OSError:
                if not cs2_running():
                    raise RenderError("CS2 is not running (it may have crashed).")
                time.sleep(2)
        raise RenderError("Could not connect to the CS2 console.")

    def send(self, *cmds):
        for c in cmds:
            try:
                self.s.sendall((c + "\n").encode("utf-8"))
            except OSError:
                self.connect(60)
                self.s.sendall((c + "\n").encode("utf-8"))
            time.sleep(0.1)

    def read(self, seconds):
        buf, end = b"", time.time() + seconds
        while time.time() < end:
            try:
                d = self.s.recv(65536)
                if d:
                    buf += d
            except socket.timeout:
                pass
            except OSError:
                self.connect(60)
        return buf.decode("utf-8", "replace")

    def wait_for(self, text, timeout):
        end, seen = time.time() + timeout, ""
        while time.time() < end:
            seen += self.read(0.5)
            if text in seen:
                return True
        return False

    def wait_ready(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.send("echo CS2R_READY")
            if self.wait_for("CS2R_READY", 3):
                return
        raise RenderError("CS2 console not responding.")


# ---------------------------------------------------------------- Windows helpers ----

def cs2_running():
    out = subprocess.run(["tasklist", "/fi", "imagename eq cs2.exe"], capture_output=True, text=True).stdout
    return "cs2.exe" in out


def faceit_ac_running():
    """FACEIT's anti-cheat protects cs2.exe while it runs, which blocks -netconport (the render can't work)."""
    out = subprocess.run(["sc", "query", "FACEITService"], capture_output=True, text=True).stdout
    return "RUNNING" in out


def window_titles():
    titles = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, b, n + 1)
                titles.append(b.value)
        return True

    user32.EnumWindows(proto(cb), 0)
    return titles


def keep_awake(on=True):
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0))


def send_to_back(hwnd, prev=None):
    HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    if prev and prev != hwnd:
        user32.SetForegroundWindow(prev)


def ensure_not_minimized():
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd and user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        send_to_back(hwnd)
        time.sleep(1)


# ---------------------------------------------------------------- CS2 lifecycle ----

def launch_cs2(width, height, extra=(), on_pid=None):
    if any("Sign in to Steam" in t for t in window_titles()):
        raise RenderError("Steam is asking you to sign in. Sign in to Steam first, then run again.")
    prev = user32.GetForegroundWindow()
    args = ["-applaunch", "730", "-insecure", "-novid", "-console", "-sw",
            "-width", str(width), "-height", str(height), "-netconport", str(PORT), *extra]
    subprocess.Popen([STEAM] + args)
    log("Launching CS2 (it will move behind your windows)...")
    end, pid_done, window_t = time.time() + 150, on_pid is None, None
    while time.time() < end:
        if not pid_done:
            pid = audio.cs2_pid()
            if pid:
                pid_done = on_pid(pid, final=window_t is not None and time.time() - window_t > 20)
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd and window_t is None:
            window_t = time.time()
            time.sleep(3)
            send_to_back(hwnd, prev)
        if window_t is not None and pid_done:
            return
        if any("Sign in to Steam" in t for t in window_titles()):
            raise RenderError("Steam is asking you to sign in. Sign in to Steam first, then run again.")
        time.sleep(0.5)
    if window_t is not None:
        return
    raise RenderError("CS2 did not start within 2.5 minutes (Steam update or dialog waiting?).")


def close_cs2(con, original):
    try:
        con.send(*settings_guard.restore_console_commands(original), "demo_ui_mode 2", "host_writeconfig", "quit")
    except Exception:
        pass
    end = time.time() + 40
    while cs2_running() and time.time() < end:
        time.sleep(1)
    if cs2_running():  # our own instance, launched by this script
        subprocess.run(["taskkill", "/im", "cs2.exe", "/f"], capture_output=True)
        time.sleep(3)


GAME_VOLUME = "0.6"  # loud enough, with headroom (only audible on the spare output)


def repair_audio_route():
    """CS2 died while its sound was redirected: start plain CS2 briefly, reset the routing, close it."""
    log("CS2 closed while its audio was redirected - starting it briefly to reset the audio output...")
    state = {"ok": False}

    def on_pid(pid, final=False):
        state["ok"], _ = audio.route(pid, "default")
        return state["ok"] or final

    for attempt in range(3):  # right after a crash Steam may still think CS2 is running for a while
        if attempt:
            log("    retrying in 30 s...")
            time.sleep(30)
        con = Console()
        try:
            launch_cs2(1920, 1080, on_pid=on_pid)
        except RenderError as e:
            log(f"    {e}")
        finally:
            close_cs2(con, {})
        if state["ok"]:
            break
    if state["ok"]:
        log("    CS2 audio output is back to your default device.")
    else:
        log("WARNING: could not reset CS2's audio output. If CS2 is silent, open CS2 and run: "
            "python render.py --fix-audio-route  (or Windows Volume mixer > Counter-Strike 2 > Output: Default)")


def load_demo(con, name, with_audio=False):
    con.connect()
    con.wait_ready(180)
    time.sleep(5)
    con.send("demo_ui_mode 0", f"playdemo {name}")  # no playback bar in recordings
    log("Loading demo...")
    time.sleep(10)
    con.connect(120)  # the console connection drops during map load
    con.wait_ready(180)
    # the demo is ready once a seek completes (was a fixed 8 s wait)
    con.send("demo_pause", "demo_gototick 2")
    if con.wait_for("Demo Skipping finished", 20):
        time.sleep(1)
    else:
        time.sleep(6)
    con.send("demo_pause",
             *[f'{k} "{v}"' for k, v in settings_guard.TOUCHED.items()],
             "cl_trueview_show_status 0", "cl_hud_telemetry_serverrecvmargin_graph_show 0")
    if with_audio:
        con.send(f'volume "{GAME_VOLUME}"', 'snd_mute_losefocus "false"')


# ---------------------------------------------------------------- video helpers ----

LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def ff(*args, check=True, cwd=None):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, cwd=cwd, creationflags=LOW_PRIORITY)
    if check and r.returncode != 0:
        raise RenderError(f"FFmpeg failed: {r.stderr.strip()[:400]}")
    return r


def capture(path, fps, out_h, seconds):
    vf = f"hwdownload,format=bgra,scale=-2:{out_h}" if out_h else "hwdownload,format=bgra"
    src = f"gfxcapture=window_title=^{WINDOW_TITLE}$:capture_cursor=0:capture_border=0:max_framerate={fps}"
    proc = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
         "-t", f"{seconds:.2f}", "-vf", vf, "-fps_mode", "cfr", "-r", str(fps),
         "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-pix_fmt", "yuv420p",
         "-progress", "pipe:1", "-stats_period", "0.02", "-nostats", path],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.first_frame_t = None
    proc.err_lines = []
    proc.samples = []  # (output time s, frames written, duplicated frames) per progress report

    def drain_stdout():  # progress lines; must always be read or FFmpeg blocks when the pipe is full
        cur = {}
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            cur[k] = v
            if proc.first_frame_t is None and k == "out_time_us" and v.isdigit() and int(v) > 0:
                proc.first_frame_t = time.perf_counter() - int(v) / 1e6
            if k == "progress":
                try:
                    proc.samples.append((int(cur["out_time_us"]) / 1e6, int(cur["frame"]), int(cur["dup_frames"])))
                except (KeyError, ValueError):
                    pass
                cur = {}

    def drain_stderr():
        for line in proc.stderr:
            proc.err_lines.append(line)

    threading.Thread(target=drain_stdout, daemon=True).start()
    threading.Thread(target=drain_stderr, daemon=True).start()
    return proc


def live_fps(cap, from_s):
    """New (not duplicated) frames per second after `from_s` of the capture, from FFmpeg's own
    counters. The game delivering fewer frames than asked for shows up as duplicated frames."""
    pts = [p for p in cap.samples if p[0] >= from_s]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1:
        return None
    (t1, f1, d1), (t2, f2, d2) = pts[0], pts[-1]
    return ((f2 - d2) - (f1 - d1)) / (t2 - t1)


def wait_first_frame(cap, timeout=15):
    """Return when FFmpeg captured its first frame (perf_counter time), waiting until it has."""
    end = time.time() + timeout
    while time.time() < end:
        if cap.first_frame_t is not None:
            return cap.first_frame_t
        if cap.poll() is not None:
            break
        time.sleep(0.01)
    raise RenderError("Window capture did not start (is CS2 minimized?). " + "".join(cap.err_lines)[-300:])


def unique_fps(path):
    """Frames per second that actually differ from the previous one (detects choppy captures)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-vf", "mpdecimate=hi=256:lo=128:frac=0.1",
                        "-fps_mode", "vfr", "-f", "null", "-"], capture_output=True, text=True, creationflags=LOW_PRIORITY)
    frames = re.findall(r"frame=\s*(\d+)", r.stderr)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not frames or not dur:
        return 0.0
    secs = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    return int(frames[-1]) / max(secs, 0.1)


def media_duration(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True, creationflags=LOW_PRIORITY)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def media_height(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True, creationflags=LOW_PRIORITY)
    m = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", r.stderr)
    return int(m.group(2)) if m else 1080


def has_audio(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True, creationflags=LOW_PRIORITY)
    return "Audio:" in r.stderr


def finish_clip(parts, dst, title, fps, ff_windows=(), card=None):
    """Join segment files, add the title (a FACEIT-style card image sliding in, or plain text), mute the
    fast-forwarded stretches, write the final clip. card: (png path, video height) from titlecard.make_card."""
    lst = dst + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    filters = []
    folder = os.path.dirname(dst)
    tf = os.path.basename(dst) + ".title.txt"  # relative to folder (ffmpeg runs there): no path escaping
    if title and not card:
        with open(os.path.join(folder, tf), "w", encoding="utf-8") as f:
            f.write(title)
        filters.append(f"drawtext=fontfile='{title_font(title)}':textfile='{tf}':fontsize=h/22:fontcolor=white:"
                       f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-tw)/2:y=h*0.72:"
                       f"alpha='if(lt(t,3),1,if(lt(t,3.6),(3.6-t)/0.6,0))'")
    # fast-forwarded stretches play without a speed label (their sound is muted below)
    import titlecard
    dur = sum(media_duration(p) for p in parts)
    end_fade = min(titlecard.END_FADE, dur / 4)
    vf = ",".join(filters + [f"fade=t=out:st={dur - end_fade:.3f}:d={end_fade:.3f}"])
    aopts = []
    if has_audio(parts[0]):
        mute = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e, _ in ff_windows)
        af = (f"volume=enable='{mute}':volume=0," if mute else "") + \
             f"afade=t=out:st={max(0.0, dur - end_fade - 0.3):.3f}:d={end_fade + 0.3:.3f}"
        aopts = ["-af", af, "-c:a", "aac", "-b:a", "192k"]
    # the ending: a small server badge slides in while the footage still plays, then picture and sound fade out
    vh = card[1] if card else media_height(parts[0])
    outro = dst + ".outro.png"
    try:
        titlecard.make_outro(outro, vh) if titlecard.available() and dur > titlecard.OUTRO + 1 else None
    except Exception as e:
        log(f"    note: end badge failed ({e}); fade only")
    has_outro = os.path.isfile(outro)
    inputs, graph, last = ["-f", "concat", "-safe", "0", "-i", lst], [], "0:v"
    if card:
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", card[0]]
        graph.append(f"[1:v]{titlecard.fade_filter()}[card];[{last}][card]{titlecard.overlay_filter(0, vh)}[vc]")
        last = "vc"
    if has_outro:
        n = 2 if card else 1
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", os.path.basename(outro)]
        graph.append(f"[{n}:v]format=rgba[ob];[{last}][ob]{titlecard.outro_filter(dur, vh)}[vo]")
        last = "vo"
    graph.append(f"[{last}]{vf}[v]")
    ff(*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "0:a?", "-r", str(fps), *aopts,
       *FINAL_ENCODE, "-movflags", "+faststart", dst, cwd=folder)
    os.remove(lst)
    if has_outro:
        os.remove(outro)
    if title and not card:
        os.remove(os.path.join(folder, tf))


def make_reel(files, dst):
    lst = dst + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", dst)
    os.remove(lst)


# ---------------------------------------------------------------- recording ----

def ff_speed(gap_ticks):
    """Fast-forward speed for a quiet stretch: aims for ~5 s on screen, between 4x and 16x."""
    return int(max(4, min(16, round(gap_ticks / TICKRATE / 5))))


def record_segment(con, clip, seg, path, a, gaps=()):
    """Record demo ticks seg=(start, end). Ticks inside `gaps` play fast-forwarded.
    Only records; process_take() turns the raw files into `path` later (in the background).
    Returns a take dict with the raw files, the fast-forward windows and the measured frame rate."""
    start, end = seg
    setup = max(1, start - 2 * TICKRATE)
    spec = f'spec_player "{clip["player"]}"'
    con.send(f"demo_gototick {setup}")
    con.wait_for("Demo Skipping finished", 30)
    con.send("demo_pause", spec, f"demo_gototick {start}")
    con.wait_for("Demo Skipping finished", 30)
    # after a seek CS2 sometimes doesn't draw the spectated player's weapon; switching away and back fixes it
    con.send("demo_pause", "spec_next")
    time.sleep(0.5)
    con.send(spec)
    ensure_not_minimized()
    time.sleep(2.0)
    # timeline: (ticks, speed) pieces
    pieces, cur = [], start
    for gs, ge in gaps:
        pieces.append((gs - cur, 1))
        pieces.append((ge - gs, ff_speed(ge - gs)))
        cur = ge
    pieces.append((end - cur, 1))
    duration = sum(t / TICKRATE / s for t, s in pieces)
    raw, wav = path + ".raw.mp4", path + ".wav"
    total = PRE_ROLL + duration + 0.5
    cap = capture(raw, a.fps, a.height, total)
    aud, audio_delay = None, 0.0
    t_video = None
    if a.audio_pid:
        t_video = wait_first_frame(cap)
        t_audio = time.perf_counter()
        aud = audio.start_capture(a.audio_pid, total + 0.5, wav)
        audio_delay = t_audio - t_video + AUDIO_START_LATENCY + VIDEO_PIPELINE_LATENCY  # audio starts this much later than video
        time.sleep(max(0.0, PRE_ROLL - (time.perf_counter() - t_video)))
    else:
        time.sleep(PRE_ROLL)
    windows, out_t = [], 0.0
    con.send("demo_timescale 1", "demo_resume")
    t0 = time.perf_counter()  # the resume command left (same clock as the audio capture's start stamp)
    for ticks, speed in pieces:
        secs = ticks / TICKRATE / speed
        if speed != 1:
            con.send(f"demo_timescale {speed}")
            windows.append((out_t, out_t + secs, speed))
        out_t += secs
        # sleep against the absolute schedule so small command delays don't add up
        time.sleep(max(0.0, out_t - (time.perf_counter() - t0)))
        if speed != 1:
            con.send("demo_timescale 1")
    con.send("demo_pause", "demo_timescale 1")
    cap.wait(timeout=90)
    if cap.returncode != 0 or not os.path.isfile(raw):
        raise RenderError(f"Window capture failed: {''.join(cap.err_lines).strip()[:300]}")
    audio_resume = None
    if aud:
        aout, aerr = aud.communicate(timeout=60)
        if aud.returncode != 0 or not os.path.isfile(wav):
            raise RenderError(f"Audio capture failed: {aerr.decode(errors='replace').strip()[:300]}")
        t_wav0 = audio.start_stamp(aout)
        if a.keep_raw:
            log("    audio capture: " + " ".join(aout.decode(errors="replace").split()))
        if t_wav0 is not None:  # where the resume command lands in the wav, by the clock (game silence can't fool it)
            audio_resume = t0 - t_wav0 + RESUME_SOUND_LATENCY
    fps = live_fps(cap, PRE_ROLL + 0.3)
    video_resume = (t0 - cap.first_frame_t) if cap.first_frame_t is not None else None
    return {"raw": raw, "wav": wav if aud else None, "audio_delay": audio_delay, "audio_resume": audio_resume,
            "video_resume": video_resume,
            "windows": windows,
            "fps": fps if fps is not None else float(a.fps), "path": path}


def process_take(take, a):
    """Cut the raw recording at the resume moment, align and mux the audio. Returns unique fps."""
    raw, wav, path, audio_delay = take["raw"], take["wav"], take["path"], take["audio_delay"]
    venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-pix_fmt", "yuv420p"]
    # Cut where playback actually resumed: by the clock, refined by the first movement in the picture.
    v_on = sync.video_onset(FFMPEG, raw, PRE_ROLL)
    v_res = take.get("video_resume")
    if v_res is not None:
        v_res += RESUME_VIDEO_LATENCY
        if v_on is not None and abs(v_on - v_res) <= ONSET_AGREE:
            v_res = v_on
    elif v_on is not None and PRE_ROLL - 0.3 <= v_on <= PRE_ROLL + 1.2:
        v_res = v_on
    v_ss = v_res - 1 / 60 if v_res is not None else PRE_ROLL
    if wav:
        # Align audio on the resume moment. In the wav it comes from the clock (when the resume command was
        # sent, on the same clock as the capture's start); the first sound after the silent pause only refines
        # it, since the game can stay quiet for a while after resuming (a player waiting in silence).
        a_on = sync.audio_onset(FFMPEG, wav, PRE_ROLL)
        a_res = take.get("audio_resume")
        if a_res is not None and a_on is not None and abs(a_on - a_res) <= ONSET_AGREE:
            a_res = a_on
        elif a_res is None:
            a_res = a_on
        if v_res is not None and a_res is not None:
            a_ss = a_res - (v_res - v_ss) - AV_FINE_TUNE
        else:
            a_ss = v_ss - audio_delay
            log("    note: resume moment not detected, audio aligned by timing estimate")
        if a.keep_raw:
            log(f"    sync: v_on={v_on} a_on={a_on} a_clock={take.get('audio_resume')} v_clock={take.get('video_resume')} "
                f"v_ss={v_ss:.3f} a_ss={a_ss:.3f}")
        ain = ["-ss", f"{a_ss:.3f}", "-i", wav] if a_ss >= 0 else ["-i", wav]
        af = "apad" if a_ss >= 0 else f"adelay={int(-a_ss * 1000)}:all=1,apad"
        ff("-ss", f"{v_ss:.3f}", "-i", raw, *ain, "-map", "0:v", "-map", "1:a", *venc,
           "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", path)
        if not a.keep_raw:
            os.remove(wav)
    else:
        ff("-ss", f"{v_ss:.3f}", "-i", raw, *venc, path)  # frame-accurate cut
    if not a.keep_raw:
        os.remove(raw)
    return unique_fps(path)


def finish_clip_job(i, n, c, takes, final, a):
    """Background: process a clip's takes, join them, add titles. Returns the result entry."""
    parts, ff_windows, offset = [], [], 0.0
    for take in takes:
        fps = process_take(take, a)
        if fps < a.fps * MIN_FPS_RATIO:
            log(f"    note: clip {i} has a choppy part ({fps:.0f} fps; live estimate was {take['fps']:.0f})")
        ff_windows += [(s + offset, e + offset, sp) for s, e, sp in take["windows"]]
        offset += media_duration(take["path"])
        parts.append(take["path"])
    title = None if a.no_titles else f"{c['player']}  |  {c['label']}  |  Round {c['round']}"
    card = None
    if title:
        import titlecard
        if titlecard.available():
            try:  # FACEIT-style card: avatar, level, the play; plain text title if anything goes wrong
                vh = a.height or media_height(parts[0])
                png = final + ".card.png"
                titlecard.make_card(png, c["player"], c.get("steamid", ""), c["label"], c["round"],
                                    os.path.basename(os.path.dirname(final)), vh)
                card = (os.path.basename(png), vh)
            except Exception as e:
                log(f"    note: title card failed ({e}); using a plain title")
    finish_clip(parts, final, title, a.fps, ff_windows, card)
    if card:
        os.remove(final + ".card.png")
    for p in parts:
        os.remove(p)
    log(f"    saved [{i}/{n}] {final}")
    return {**c, "file": final}


def render_demo(demo_arg, a, out_root):
    name, demo_path = prepare_demo(demo_arg)
    log(f"Analysing {name} ...")
    clips = find_highlights(demo_path, a.player[0] if a.player and len(a.player) == 1 else None,
                            a.min_kills, a.min_score)
    if a.player and len(a.player) > 1:
        clips = [c for c in clips if c["player"] in a.player]
    clips = clips[: a.max_clips]
    if not clips:
        log("No highlights found.")
        return []
    for c in clips:
        secs = sum(e - s for s, e in c["segments"]) / TICKRATE
        log(f"  {c['score']:>4} pts  R{c['round']:<3} {c['player']:<16} {c['label']:<26} {secs:5.1f}s")
        if a.list:
            log("            " + ", ".join(c["reasons"]))
    if a.list:
        return []

    out_dir = os.path.join(os.path.abspath(out_root), name)
    os.makedirs(out_dir, exist_ok=True)
    if cs2_running():
        raise RenderError("CS2 is already running. Close it first. (This script never touches a game you're playing.)")
    if faceit_ac_running():
        raise RenderError("The FACEIT client / anti-cheat is running. It protects CS2 and blocks the console the "
                          "renderer uses. Close the FACEIT client (tray icon > Exit), then run again.")

    spare = None
    if not a.no_audio:
        spare = audio.pick_spare(a.audio_device)
        if spare:
            log(f"Game audio: CS2 will play on '{spare[2]}' so you don't hear it; only CS2 is recorded.")
        elif a.audio_audible:
            log("Game audio: no spare output found; CS2 will play on your normal device (--audio-audible).")
        else:
            log("Game audio: OFF - no spare output to send CS2's sound to (see README, or use --audio-audible).")
    a.audio_pid = None

    def on_pid(pid, final=False):
        """Returns True when done (routed, or gave up)."""
        if spare:
            ok, out = audio.route(pid, spare[0])
            if not ok:
                if final:
                    log(f"    warning: could not route CS2 audio ({out.splitlines()[-1]}); recording without audio")
                    return True
                return False  # process still starting up: try again shortly
        if spare or a.audio_audible:
            a.audio_pid = pid
        return True

    original = settings_guard.snapshot()
    con, results, futures = Console(), [], {}
    worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        launch_cs2(1920, 1080, on_pid=None if a.no_audio else on_pid)
        load_demo(con, name, with_audio=bool(a.audio_pid))
        # record in match order: once the demo has played into the match end, CS2 keeps showing the
        # victory screen even after jumping back, so never seek backwards
        order = sorted(enumerate(clips, 1), key=lambda ic: ic[1]["segments"][0][0])
        results[:] = [None] * len(clips)
        for i, c in order:
            base = f"{i:02d}_R{c['round']}_{safe(c['label'])}_{safe(c['player'])}"
            final = os.path.join(out_dir, base + ".mp4")
            log(f"[{i}/{len(clips)}] {c['player']} - {c['label']} (round {c['round']})")
            segs = c["segments"]
            if a.gaps == "fast" and len(segs) > 1:
                # one continuous recording; quiet stretches between segments play fast-forwarded
                jobs = [((segs[0][0], segs[-1][1]), [(segs[k][1], segs[k + 1][0]) for k in range(len(segs) - 1)])]
            else:
                jobs = [(seg, []) for seg in segs]  # quiet stretches are cut out
            takes = []
            for j, (seg, gaps) in enumerate(jobs):
                part = os.path.join(out_dir, f"{base}.part{j}.mp4")
                for attempt in (1, 2):
                    take = record_segment(con, c, seg, part, a, gaps)
                    if take["fps"] >= a.fps * MIN_FPS_RATIO or attempt == 2:
                        break
                    log(f"    part {j + 1} was choppy ({take['fps']:.0f} fps), recording again...")
                    for f in (take["raw"], take["wav"]):
                        if f and os.path.isfile(f):
                            os.remove(f)
                if take["fps"] < a.fps * MIN_FPS_RATIO:
                    log(f"    warning: part {j + 1} is still choppy ({take['fps']:.0f} fps)")
                takes.append(take)
            # processing (sync, audio, titles) runs in the background while the next clip records
            futures[i] = worker.submit(finish_clip_job, i, len(clips), c, takes, final, a)
    finally:
        if audio.route_pending():
            pid = audio.cs2_pid()
            ok, out = audio.route(pid, "default") if pid else (False, "CS2 not running")
            if not ok:
                close_cs2(con, original)
                repair_audio_route()
        close_cs2(con, original)
        fixed = settings_guard.restore_pending()
        log("CS2 closed, your settings are restored." + (f" (fixed: {', '.join(fixed)})" if fixed else ""))
        if futures:
            log("Finishing clips...")
        worker.shutdown(wait=True)

    for i, fut in sorted(futures.items()):
        try:
            results[i - 1] = fut.result()
        except Exception as e:
            log(f"    clip {i} failed while processing: {e}")
    results = [r for r in results if r]
    if len(results) > 1 and not a.no_reel:
        reel = os.path.join(out_dir, "highlight_reel.mp4")
        make_reel([r["file"] for r in results], reel)
        log(f"Highlight reel: {reel}")
    with open(os.path.join(out_dir, "clips.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    write_scoreboard(demo_path, os.path.join(out_dir, "scoreboard.txt"))
    if a.upload and results:
        import upload
        try:
            upload.upload(out_dir, public=a.public)
        except SystemExit as e:  # upload problems shouldn't hide that the render worked
            log(f"Upload failed: {e}  (try again later with: python upload.py {os.path.basename(out_dir)})")
    return results


def write_scoreboard(demo_path, dst):
    """K/D/A, ADR, KAST and Swing for all players, plus swing per round (see stats.py)."""
    import contextlib
    import io
    import stats
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stats.print_scoreboard(stats.analyse(demo_path), show_rounds=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        log(f"Scoreboard (KAST, Swing): {dst}")
        import scoreboard_html
        page = scoreboard_html.write(demo_path, os.path.splitext(dst)[0] + ".html")
        log(f"Scoreboard page: {page}")
    except Exception as e:
        log(f"    warning: scoreboard failed ({e})")


# ---------------------------------------------------------------- misc ----

def prepare_demo(arg):
    """Full path to a .dem anywhere (copied into CS2 under a short name) or a name already in csgo/."""
    if os.path.isfile(arg):
        base = os.path.splitext(os.path.basename(arg))[0]
        m = re.search(r"de_[a-z0-9]+", base)
        name = safe(f"{m.group(0)}_{base[:19]}" if m else base)[:60]
        dest = os.path.join(CSGO_DIR, name + ".dem")
        if not (os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(arg)):
            log(f"Copying demo into CS2 as {name}.dem ...")
            shutil.copyfile(arg, dest)
        return name, dest
    name = os.path.splitext(os.path.basename(arg))[0]
    path = os.path.join(CSGO_DIR, name + ".dem")
    if not os.path.isfile(path):
        raise RenderError(f"Demo not found: {arg}")
    return name, path


def safe(s):
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s).strip("_")
    out = re.sub(r"_+", "_", out)
    return out or "p" + "".join(f"{ord(ch):x}" for ch in s)[:12]  # e.g. "!" -> "p21"


def main():
    ap = argparse.ArgumentParser(description="Render CS2 demo highlights in the background.")
    ap.add_argument("demos", nargs="*", help="path(s) to .dem files, or names of demos already in the csgo folder")
    ap.add_argument("--player", action="append", help="only this player (repeat for several)")
    ap.add_argument("--min-kills", type=int, default=1, help="ignore plays with fewer kills (unless clutch/defuse/etc.)")
    ap.add_argument("--min-score", type=int, default=130,
                    help="minimum highlight points (default 130: a 4K, ace, big clutch or an outstanding 3K; 45 = any 3K)")
    ap.add_argument("--max-clips", type=int, default=5, help="per demo (default 5)")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--height", type=int, default=1080, help="output height (0 = native window size)")
    ap.add_argument("--gaps", choices=["fast", "cut"], default="fast",
                    help="quiet stretches inside a highlight: fast-forward them (default) or cut them out")
    ap.add_argument("--no-titles", action="store_true", help="no player/round title overlay")
    ap.add_argument("--no-reel", action="store_true", help="don't join clips into a highlight reel")
    ap.add_argument("--no-audio", action="store_true", help="record without game sound")
    ap.add_argument("--audio-device", help="name (or part of it) of the output CS2 should play on while rendering")
    ap.add_argument("--audio-audible", action="store_true",
                    help="record audio even if no spare output exists (you will hear CS2 while it renders)")
    ap.add_argument("--fix-audio-route", action="store_true",
                    help="reset CS2's audio output to your default device (run while CS2 is open) and exit")
    ap.add_argument("--keep-raw", action="store_true", help=argparse.SUPPRESS)  # debugging
    ap.add_argument("--list", action="store_true", help="only list the highlights, record nothing")
    ap.add_argument("--upload", action="store_true", help="upload the clips and stats to the website afterwards")
    ap.add_argument("--hidden", dest="public", action="store_false", help="with --upload: keep the match hidden (default: public)")
    ap.add_argument("--public", dest="public", action="store_true", help=argparse.SUPPRESS)  # the default now
    ap.add_argument("--out", default=os.path.join(ROOT, "output"))
    a = ap.parse_args()

    if a.fix_audio_route:
        pid = audio.cs2_pid()
        if not pid:
            log("Start CS2 first, then run this again.")
            sys.exit(1)
        ok, out = audio.route(pid, "default")
        log("CS2 audio output reset to your default device." if ok else f"Failed: {out}")
        sys.exit(0 if ok else 1)
    if not a.demos:
        ap.error("give at least one demo")
    if audio.route_pending() and not cs2_running():
        repair_audio_route()  # an earlier run was interrupted while CS2's audio was redirected
    if not cs2_running():
        fixed = settings_guard.restore_pending(stale=True)  # recover from an interrupted earlier run
        if fixed:
            log(f"Restored settings left over from an interrupted run: {', '.join(fixed)}")

    keep_awake(True)
    try:
        for d in a.demos:
            render_demo(d, a, a.out)
    except RenderError as e:
        log(f"ERROR: {e}")
        sys.exit(1)
    finally:
        keep_awake(False)
    log("Done.")


if __name__ == "__main__":
    main()

"""Protects the user's CS2 settings while render.py temporarily changes some of them.

Before CS2 is launched, the current values of every setting we touch are snapshotted from the active
Steam account's cs2_machine_convars.vcfg into backup/pending_restore.json. After CS2 exits, those keys are
written back exactly (a key that did not exist before is removed again). If a run crashes, the pending
snapshot is applied at the start of the next run.
"""
import glob
import json
import os
import re
import shutil
import time

STEAM_USERDATA = r"C:\Program Files (x86)\Steam\userdata"
ROOT = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(ROOT, "backup", "pending_restore.json")

# Settings render.py changes while recording -> value used during recording.
TOUCHED = {
    "volume": "0",
    "engine_no_focus_sleep": "0",
    "cl_hud_telemetry_frametime_show": "0",
    "cl_hud_telemetry_ping_show": "0",
    "cl_hud_telemetry_net_misdelivery_show": "0",
    "r_show_build_info": "false",
    "snd_mute_losefocus": "false",
    # uncapped, CS2 runs the GPU at ~99% and Windows can't deliver captured frames smoothly (freezes);
    # recording is 60 fps, so 120 is plenty and leaves the GPU headroom
    "fps_max": "120",
}


# Display settings in cs2_video.txt (next to the convars file). render.py launches CS2 in a window at the
# recording size (-sw -width -height) and CS2 saves that as your display mode; these are put back after.
VIDEO_KEYS = ["setting.defaultres", "setting.defaultresheight", "setting.refreshrate_numerator",
              "setting.refreshrate_denominator", "setting.fullscreen", "setting.nowindowborder",
              "setting.coop_fullscreen"]


def video_file(cfg_path):
    return os.path.join(os.path.dirname(cfg_path), "cs2_video.txt") if cfg_path else None


def video_snapshot():
    """(file, {key: value}) of the active account's display settings, or None."""
    f = video_file(active_cfg_file())
    return (f, read_values(f, VIDEO_KEYS)) if f and os.path.isfile(f) else None


def video_restore(snap, only_if_windowed=False):
    """Put display settings back from video_snapshot(); returns the keys fixed. only_if_windowed: only when
    the file now shows the renderer's windowed mode (after an interrupted run you may have changed them)."""
    if not snap:
        return []
    f, values = snap
    if not os.path.isfile(f):
        return []
    now = read_values(f, values)
    if only_if_windowed and not (now.get("setting.fullscreen") == "0" and values.get("setting.fullscreen") != "0"):
        return []
    fixed = [k for k in values if values[k] is not None and now.get(k) != values[k]]
    if fixed:
        write_values(f, {k: values[k] for k in fixed})
    return fixed


# Settings changed in other ways (launch options / file edits) whose file key differs from the console name.
# They are snapshotted and restored through the file only.
FILE_ONLY_KEYS = ["sound_device_override$1"]


def steam_active_user():
    """32-bit account id of the account signed in to Steam right now (Steam keeps it in the registry), or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            uid = int(winreg.QueryValueEx(k, "ActiveUser")[0])
        return uid or None
    except (OSError, ValueError, ImportError):
        return None


def active_cfg_file():
    """cs2_machine_convars.vcfg of the account signed in to Steam (CS2 runs on it); if Steam can't tell, the
    most recently used one. (By file time alone, switching Steam accounts picked the wrong one.)"""
    uid = steam_active_user()
    if uid:
        f = os.path.join(STEAM_USERDATA, str(uid), "730", "local", "cfg", "cs2_machine_convars.vcfg")
        if os.path.isfile(f):
            return f
    files = glob.glob(os.path.join(STEAM_USERDATA, "*", "730", "local", "cfg", "cs2_machine_convars.vcfg"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _line_re(key):
    return re.compile(r'^(\s*)"' + re.escape(key) + r'"(\s+)"([^"]*)"\s*$', re.M)


def read_values(path, keys):
    text = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for k in keys:
        m = _line_re(k).search(text)
        out[k] = m.group(3) if m else None  # None = not present in file (game default)
    return out


def write_values(path, values):
    """Set keys to values in the vcfg file; value None removes the key's line."""
    text = open(path, encoding="utf-8", errors="replace").read()
    changed = False
    for k, v in values.items():
        rx = _line_re(k)
        m = rx.search(text)
        if v is None:
            if m:
                text = text[:m.start()] + text[m.end() + 1:]  # drop line incl. newline
                changed = True
        elif m:
            if m.group(3) != v:
                text = text[:m.start()] + f'{m.group(1)}"{k}"{m.group(2)}"{v}"' + text[m.end():]
                changed = True
        else:
            # insert right after the opening of the "convars" block
            ins = re.search(r'"convars"\s*\{\s*\n', text)
            if ins:
                text = text[:ins.end()] + f'\t\t"{k}"\t\t"{v}"\n' + text[ins.end():]
                changed = True
    if changed:
        shutil.copyfile(path, path + ".cs2render.bak")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return changed


def snapshot():
    """Record current values before launching CS2. Returns {key: original value} for in-game restore."""
    path = active_cfg_file()
    if not path:
        return {}
    original = read_values(path, list(TOUCHED) + FILE_ONLY_KEYS)
    video = video_snapshot()
    os.makedirs(os.path.dirname(PENDING), exist_ok=True)
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"file": path, "values": original, "video": list(video) if video else None, "time": time.time()},
                  f, indent=1)
    return original


def restore_console_commands(original):
    """Console commands that put the original values back (for keys that existed)."""
    return [f'{k} "{v}"' for k, v in original.items() if v is not None and k in TOUCHED]


RENDER_VALUES = {"volume": {"0", "0.6"}}  # values a render sets besides TOUCHED (render.GAME_VOLUME)


def restore_pending(stale=False):
    """After CS2 has exited: make the settings file match the snapshot exactly. Safe to call any time
    CS2 is not running. Returns a list of keys that had to be fixed.
    stale=True: the snapshot was left by an interrupted earlier run, and you may have played since then.
    Only values that look like the renderer's own are reverted; anything else is your own change and stays."""
    if not os.path.isfile(PENDING):
        return []
    with open(PENDING, encoding="utf-8") as f:
        snap = json.load(f)
    path, values = snap["file"], snap["values"]
    fixed = []
    if os.path.isfile(path):
        now = read_values(path, values)
        fixed = [k for k in values if now.get(k) != values[k]]
        if stale:
            def ours(k):
                return now.get(k) in ({TOUCHED[k]} | RENDER_VALUES.get(k, set()))
            fixed = [k for k in fixed if k in TOUCHED and ours(k)]
        if fixed:
            write_values(path, {k: values[k] for k in fixed})
    if snap.get("video"):
        fixed += video_restore(tuple(snap["video"]), only_if_windowed=stale)
    os.remove(PENDING)
    return fixed

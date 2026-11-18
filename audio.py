"""Game-audio support for render.py, built on tools/proccap.exe.

- CS2's sound is sent to a spare output you don't listen to (for example an unused "Digital Output"), so
  you hear nothing while it renders. This is Windows' per-app output device, set only for the CS2 we
  launched and reset before it quits.
- proccap records only CS2's own audio stream (process loopback), so Discord or music are never recorded.
"""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
PROCCAP = os.path.join(ROOT, "tools", "proccap.exe")
MARKER = os.path.join(ROOT, "backup", "audio_route_pending.json")


def _run(*args):
    r = subprocess.run([PROCCAP, *map(str, args)], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def devices():
    """[(endpoint_id, is_default, name), ...] for active outputs."""
    rc, out = _run("devices")
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            res.append((parts[0], parts[1] == "default", parts[2]))
    return res


def pick_spare(preference=None):
    """An active output that isn't your default device (or the one matching `preference`)."""
    devs = devices()
    if preference:
        for d in devs:
            if preference.lower() in d[2].lower() or preference.lower() == d[0].lower():
                return d
        return None
    spares = [d for d in devs if not d[1]]
    # prefer outputs that are usually not connected to anything you listen to
    spares.sort(key=lambda d: 0 if any(k in d[2].lower() for k in ("digital", "spdif", "s/pdif", "optical")) else 1)
    return spares[0] if spares else None


def cs2_pid():
    out = subprocess.run(["tasklist", "/fi", "imagename eq cs2.exe", "/fo", "csv", "/nh"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) > 1 and cols[0].lower().strip('"') == "cs2.exe":
            return int(cols[1])
    return None


def route(pid, endpoint):
    """Send process `pid` to output `endpoint` ('default' = undo). Keeps a marker while routed."""
    rc, out = _run("route", pid, endpoint)
    if endpoint != "default" and rc == 0:
        os.makedirs(os.path.dirname(MARKER), exist_ok=True)
        with open(MARKER, "w", encoding="utf-8") as f:
            json.dump({"endpoint": endpoint}, f)
    elif rc == 0 and os.path.isfile(MARKER):
        os.remove(MARKER)
    return rc == 0, out


def route_pending():
    return os.path.isfile(MARKER)


def start_stamp(out):
    """The capture's time 0 (time.perf_counter() clock) from proccap's 'start <ticks> <frequency>' line."""
    for line in (out or b"").decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "start":
            return int(parts[1]) / int(parts[2])
    return None


def start_capture(pid, seconds, wav_path):
    return subprocess.Popen([PROCCAP, "capture", str(pid), f"{seconds:.3f}", wav_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

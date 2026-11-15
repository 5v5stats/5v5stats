"""Serve the locally published website (the "local" publish target) at http://localhost:8080.

    python serve_site.py            serve C:\\cs2render\\site (LOCAL_SITE in publish.env)
    python serve_site.py --port 9000

Only reachable from this PC (bound to 127.0.0.1). Supports byte ranges so videos can be seeked, like a
real host. Stop it with Ctrl+C.
"""
import argparse
import http.server
import os
import re
import sys

import publish

TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json", ".mp4": "video/mp4",
         ".jpg": "image/jpeg", ".png": "image/png", ".css": "text/css", ".js": "text/javascript"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):
        return TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if path.endswith(("/", "\\")):
            path = os.path.join(path, "index.html")
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        size = os.path.getsize(path)
        if not m or (not m.group(1) and not m.group(2)):
            self.send_error(416)
            return None
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:  # last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0:
                chunk = source.read(min(1 << 16, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass  # the browser stops a video request when you seek elsewhere
        finally:
            self._remaining = None

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    ap = argparse.ArgumentParser(description="Serve the locally published site.")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    root = publish.load_env()["LOCAL_SITE"]
    os.makedirs(root, exist_ok=True)
    handler = lambda *args, **kw: Handler(*args, directory=root, **kw)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print(f"Serving {root} at http://localhost:{a.port}/index.html  (Ctrl+C to stop)")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

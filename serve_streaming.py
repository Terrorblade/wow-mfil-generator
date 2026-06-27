#!/usr/bin/env python3
"""
serve_streaming.py - minimal static HTTP server with Range support, for testing the
client's streaming downloader locally.

The 3.3.5 streaming client fetches files with HTTP byte-range requests
(Range: bytes=start-end). Python's stock SimpleHTTPRequestHandler ignores Range and
always returns 200 with the whole body, which the client's transport rejects. This
handler implements single-range 206 responses plus full 200 responses.

Usage:
  python serve_streaming.py SERVE_ROOT [--host 0.0.0.0] [--port 80] [--prefix /streaming]

  SERVE_ROOT  directory whose tree mirrors the in-game relative paths (the same layout the
              generator's --serve-root produces).
  --prefix    URL path prefix the client requests under. With
              source=http://host/streaming the client GETs /streaming/<relpath>, so pass
              --prefix /streaming and point SERVE_ROOT at the files.

This is for local testing. In production, any static server with range support (nginx,
Caddy, S3, a CDN) serving <source>/<relpath> works - nginx serves ranges by default.
"""

import argparse
import os
import posixpath
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def make_handler(serve_root, prefix):
    serve_root = os.path.abspath(serve_root)
    prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "StreamingServer/1.0"

        def _resolve(self):
            path = urllib.parse.urlparse(self.path).path
            path = urllib.parse.unquote(path)
            if prefix:
                if path == prefix:
                    path = "/"
                elif path.startswith(prefix + "/"):
                    path = path[len(prefix):]
                else:
                    return None
            # Normalise and prevent directory traversal.
            path = posixpath.normpath(path).lstrip("/")
            full = os.path.join(serve_root, path.replace("/", os.sep))
            if os.path.commonpath([serve_root, os.path.abspath(full)]) != serve_root:
                return None
            return full

        def do_HEAD(self):
            self._serve(head_only=True)

        def do_GET(self):
            self._serve(head_only=False)

        def _serve(self, head_only):
            full = self._resolve()
            if not full or not os.path.isfile(full):
                self.send_error(404, "Not Found")
                return
            size = os.path.getsize(full)
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if rng:
                m = RANGE_RE.fullmatch(rng.strip())
                if m:
                    g1, g2 = m.group(1), m.group(2)
                    if g1 == "" and g2 != "":  # suffix range: last N bytes
                        start = max(0, size - int(g2))
                    else:
                        start = int(g1)
                        end = int(g2) if g2 else size - 1
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.end_headers()
                        return
                    status = 206

            length = end - start + 1
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if head_only:
                return
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        def log_message(self, fmt, *a):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    return Handler


def main(argv):
    ap = argparse.ArgumentParser(description="Range-capable static server for streaming.")
    ap.add_argument("serve_root")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--prefix", default="/streaming")
    args = ap.parse_args(argv)

    handler = make_handler(args.serve_root, args.prefix)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print("Serving %s on http://%s:%d%s/  (Ctrl-C to stop)" %
          (os.path.abspath(args.serve_root), args.host, args.port,
           "/" + args.prefix.strip("/")))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

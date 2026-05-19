#!/usr/bin/env python3
"""Local IPTV server: serves a folder of videos as an M3U playlist for IPTV
Smarters Pro (or any M3U player). Uses only the Python standard library."""

import argparse
import html
import mimetypes
import os
import re
import socket
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".mpg", ".mpeg",
    ".wmv", ".flv", ".m2ts", ".vob", ".ogv", ".3gp",
}

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".ts")

ROOT = ""  # set in main
HOST_URL = ""  # set per request from Host header


def discover_videos(root):
    items = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                items.append(rel.replace(os.sep, "/"))
    items.sort()
    return items


def build_m3u(videos, base_url):
    lines = ["#EXTM3U"]
    for rel in videos:
        title = os.path.splitext(os.path.basename(rel))[0]
        group = os.path.dirname(rel) or "Movies"
        lines.append(
            f'#EXTINF:-1 tvg-id="" tvg-name="{title}" '
            f'group-title="{group}",{title}'
        )
        url_path = "/stream/" + urllib.parse.quote(rel)
        lines.append(f"{base_url}{url_path}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalIPTV/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    def _base_url(self):
        host = self.headers.get("Host") or HOST_URL
        return f"http://{host}"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path in ("/playlist.m3u", "/playlist", "/playlist.m3u8"):
            return self._serve_playlist()
        if path.startswith("/stream/"):
            rel = urllib.parse.unquote(path[len("/stream/"):])
            return self._serve_file(rel)
        return self._error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/stream/"):
            rel = urllib.parse.unquote(parsed.path[len("/stream/"):])
            return self._serve_file(rel, head_only=True)
        return self._error(HTTPStatus.NOT_FOUND, "Not Found")

    def _serve_index(self):
        videos = discover_videos(ROOT)
        base = self._base_url()
        rows = "".join(
            f'<li><a href="/stream/{urllib.parse.quote(v)}">{html.escape(v)}</a></li>'
            for v in videos
        )
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Local IPTV</title>
<style>body{{font-family:system-ui;margin:2em;max-width:900px}}code{{background:#eee;padding:2px 6px;border-radius:4px}}</style>
</head><body>
<h1>Local IPTV Server</h1>
<p>Playlist URL for IPTV Smarters Pro:<br><code>{base}/playlist.m3u</code></p>
<p>{len(videos)} video(s) found in <code>{html.escape(ROOT)}</code></p>
<ul>{rows}</ul>
</body></html>
"""
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_playlist(self):
        videos = discover_videos(ROOT)
        data = build_m3u(videos, self._base_url()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Disposition", 'inline; filename="playlist.m3u"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe_path(self, rel):
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(ROOT, rel))
        root_real = os.path.realpath(ROOT)
        if not (full == root_real or full.startswith(root_real + os.sep)):
            return None
        return full

    def _serve_file(self, rel, head_only=False):
        full = self._safe_path(rel)
        if not full or not os.path.isfile(full):
            return self._error(HTTPStatus.NOT_FOUND, "File not found")

        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"

        range_hdr = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
            if m:
                s, e = m.group(1), m.group(2)
                if s == "" and e == "":
                    return self._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Bad Range")
                if s == "":
                    length = int(e)
                    start = max(0, size - length)
                    end = size - 1
                else:
                    start = int(s)
                    end = int(e) if e else size - 1
                if start >= size or end >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if head_only:
            return

        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 64 * 1024
                while remaining > 0:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, msg):
        body = msg.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Local IPTV server (M3U) for a folder of videos.")
    ap.add_argument("--folder", "-f", required=True, help="Folder containing your movies")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    ap.add_argument("--port", "-p", type=int, default=8080, help="Port (default 8080)")
    args = ap.parse_args()

    global ROOT, HOST_URL
    ROOT = os.path.abspath(args.folder)
    if not os.path.isdir(ROOT):
        print(f"error: folder not found: {ROOT}", file=sys.stderr)
        sys.exit(1)

    ip = local_ip()
    HOST_URL = f"{ip}:{args.port}"

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {ROOT}")
    print(f"  Web UI:   http://{ip}:{args.port}/")
    print(f"  Playlist: http://{ip}:{args.port}/playlist.m3u")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Local IPTV server + web portal.

Scans a folder for video files, serves them over HTTP with byte-range support
(for IPTV Smarters Pro, VLC, browsers, etc.), exposes a /playlist.m3u, and
hosts a web portal at / for managing metadata: titles, groups, descriptions,
thumbnails (custom upload or auto-extract via ffmpeg), favorite/hidden flags.

Stdlib only. ffmpeg is optional; required for auto-thumbnail generation."""

import argparse
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".mpg", ".mpeg",
    ".wmv", ".flv", ".m2ts", ".vob", ".ogv", ".3gp",
}
IMAGE_CT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".ts")

ROOT = ""
META_DIR = ""
META_FILE = ""
THUMB_DIR = ""
META_LOCK = threading.Lock()
META = {"videos": {}}
HAS_FFMPEG = False


# ---------- metadata ----------

def _vid_id(rel):
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def load_meta():
    global META
    if os.path.isfile(META_FILE):
        try:
            with open(META_FILE, "r", encoding="utf-8") as f:
                META = json.load(f)
        except (OSError, json.JSONDecodeError):
            META = {"videos": {}}
    META.setdefault("videos", {})


def save_meta():
    os.makedirs(META_DIR, exist_ok=True)
    tmp = META_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(META, f, indent=2, ensure_ascii=False)
    os.replace(tmp, META_FILE)


def video_record(rel):
    v = META["videos"].get(rel, {})
    base = os.path.splitext(os.path.basename(rel))[0]
    group = os.path.dirname(rel) or "Movies"
    return {
        "id": _vid_id(rel),
        "path": rel,
        "title": v.get("title") or base,
        "group": v.get("group") or group,
        "description": v.get("description", ""),
        "year": v.get("year"),
        "rating": v.get("rating"),
        "favorite": bool(v.get("favorite")),
        "hidden": bool(v.get("hidden")),
        "thumbnail": _thumb_url_for(rel),
        "has_custom_thumb": _thumb_path(rel, "custom") is not None,
        "has_auto_thumb": _thumb_path(rel, "auto") is not None,
    }


def update_video(rel, patch):
    with META_LOCK:
        cur = META["videos"].get(rel, {})
        for k in ("title", "group", "description", "year", "rating", "favorite", "hidden"):
            if k in patch:
                cur[k] = patch[k]
        META["videos"][rel] = cur
        save_meta()


# ---------- thumbnails ----------

def _thumb_path(rel, kind):
    """kind = 'custom' or 'auto'. Returns existing path or None."""
    vid = _vid_id(rel)
    suffix = "" if kind == "custom" else "_auto"
    for ext in (".jpg", ".png", ".webp"):
        p = os.path.join(THUMB_DIR, f"{vid}{suffix}{ext}")
        if os.path.isfile(p):
            return p
    return None


def _thumb_url_for(rel):
    if _thumb_path(rel, "custom") or _thumb_path(rel, "auto"):
        return f"/thumb/{urllib.parse.quote(rel)}"
    return None


def save_custom_thumb(rel, data, content_type):
    ext = IMAGE_CT.get(content_type)
    if not ext:
        return False
    os.makedirs(THUMB_DIR, exist_ok=True)
    # remove other-ext custom variants
    for e in IMAGE_CT.values():
        p = os.path.join(THUMB_DIR, f"{_vid_id(rel)}{e}")
        if os.path.isfile(p):
            os.remove(p)
    with open(os.path.join(THUMB_DIR, f"{_vid_id(rel)}{ext}"), "wb") as f:
        f.write(data)
    return True


def delete_custom_thumb(rel):
    for e in IMAGE_CT.values():
        p = os.path.join(THUMB_DIR, f"{_vid_id(rel)}{e}")
        if os.path.isfile(p):
            os.remove(p)


def auto_generate_thumb(rel, timestamp="00:00:30"):
    if not HAS_FFMPEG:
        return False
    full = os.path.join(ROOT, rel)
    if not os.path.isfile(full):
        return False
    os.makedirs(THUMB_DIR, exist_ok=True)
    out = os.path.join(THUMB_DIR, f"{_vid_id(rel)}_auto.jpg")
    # try at requested ts; if it fails (short video), fall back to 1s
    for ts in (timestamp, "00:00:01"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", ts, "-i", full,
                 "-frames:v", "1", "-q:v", "3", "-vf", "scale=480:-1", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


# ---------- discovery ----------

def discover_videos():
    items = []
    for dirpath, dirs, files in os.walk(ROOT):
        # skip our own metadata folder
        dirs[:] = [d for d in dirs if not d.startswith(".iptv")]
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                items.append(rel.replace(os.sep, "/"))
    items.sort()
    return items


def build_m3u(base_url, include_hidden=False):
    lines = ["#EXTM3U"]
    for rel in discover_videos():
        rec = video_record(rel)
        if rec["hidden"] and not include_hidden:
            continue
        logo = f"{base_url}{rec['thumbnail']}" if rec["thumbnail"] else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{rec["id"]}" tvg-name="{rec["title"]}" '
            f'tvg-logo="{logo}" group-title="{rec["group"]}",{rec["title"]}'
        )
        lines.append(f"{base_url}/stream/{urllib.parse.quote(rel)}")
    return "\n".join(lines) + "\n"


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "LocalIPTV/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    # routing -------------------------------------------------
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        try:
            if path == "/" or path == "/index.html":
                return self._html(PORTAL_HTML)
            if path == "/api/videos":
                return self._api_list_videos(p)
            if path.startswith("/api/video/") and path.count("/") == 3:
                return self._api_get_video(path.split("/")[-1])
            if path in ("/playlist.m3u", "/playlist", "/playlist.m3u8"):
                return self._serve_playlist()
            if path.startswith("/stream/"):
                rel = urllib.parse.unquote(path[len("/stream/"):])
                return self._serve_file(rel)
            if path.startswith("/thumb/"):
                rel = urllib.parse.unquote(path[len("/thumb/"):])
                return self._serve_thumb(rel)
            return self._err(HTTPStatus.NOT_FOUND, "Not Found")
        except BrokenPipeError:
            pass

    def do_HEAD(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/stream/"):
            rel = urllib.parse.unquote(p.path[len("/stream/"):])
            return self._serve_file(rel, head_only=True)
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_PATCH(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        if path.startswith("/api/video/") and path.count("/") == 3:
            return self._api_patch_video(path.split("/")[-1])
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        m = re.match(r"^/api/video/([^/]+)/thumbnail$", path)
        if m:
            return self._api_upload_thumb(m.group(1))
        m = re.match(r"^/api/video/([^/]+)/auto-thumb$", path)
        if m:
            return self._api_auto_thumb(m.group(1), urllib.parse.parse_qs(p.query))
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/video/([^/]+)/thumbnail$", p.path)
        if m:
            return self._api_delete_thumb(m.group(1))
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    # helpers -------------------------------------------------
    def _base_url(self):
        host = self.headers.get("Host") or "localhost"
        return f"http://{host}"

    def _rel_for_id(self, vid):
        for rel in discover_videos():
            if _vid_id(rel) == vid:
                return rel
        return None

    def _safe_path(self, rel):
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(ROOT, rel))
        root_real = os.path.realpath(ROOT)
        if not (full == root_real or full.startswith(root_real + os.sep)):
            return None
        return full

    def _read_body(self, max_bytes=20 * 1024 * 1024):
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_bytes:
            return None
        return self.rfile.read(length)

    def _json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body):
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # API -----------------------------------------------------
    def _api_list_videos(self, parsed):
        q = urllib.parse.parse_qs(parsed.query)
        include_hidden = q.get("hidden", ["1"])[0] == "1"
        records = [video_record(r) for r in discover_videos()]
        if not include_hidden:
            records = [r for r in records if not r["hidden"]]
        groups = sorted({r["group"] for r in records})
        self._json({
            "videos": records,
            "groups": groups,
            "ffmpeg": HAS_FFMPEG,
            "root": ROOT,
            "playlist_url": f"{self._base_url()}/playlist.m3u",
        })

    def _api_get_video(self, vid):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        self._json(video_record(rel))

    def _api_patch_video(self, vid):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        body = self._read_body(max_bytes=1024 * 1024)
        try:
            patch = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._err(HTTPStatus.BAD_REQUEST, "Invalid JSON")
        update_video(rel, patch)
        self._json(video_record(rel))

    def _api_upload_thumb(self, vid):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct not in IMAGE_CT:
            return self._err(HTTPStatus.BAD_REQUEST, "Send image/jpeg, image/png, or image/webp as body")
        data = self._read_body()
        if not data:
            return self._err(HTTPStatus.BAD_REQUEST, "Empty body")
        save_custom_thumb(rel, data, ct)
        self._json(video_record(rel))

    def _api_auto_thumb(self, vid, q):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        if not HAS_FFMPEG:
            return self._err(HTTPStatus.BAD_REQUEST, "ffmpeg not installed on server")
        ts = q.get("ts", ["00:00:30"])[0]
        ok = auto_generate_thumb(rel, ts)
        if not ok:
            return self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "Thumbnail generation failed")
        self._json(video_record(rel))

    def _api_delete_thumb(self, vid):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        delete_custom_thumb(rel)
        self._json(video_record(rel))

    # serving -------------------------------------------------
    def _serve_playlist(self):
        data = build_m3u(self._base_url()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Disposition", 'inline; filename="playlist.m3u"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_thumb(self, rel):
        path = _thumb_path(rel, "custom") or _thumb_path(rel, "auto")
        if not path:
            return self._err(HTTPStatus.NOT_FOUND, "No thumbnail")
        ct = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, rel, head_only=False):
        full = self._safe_path(rel)
        if not full or not os.path.isfile(full):
            return self._err(HTTPStatus.NOT_FOUND, "File not found")

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
                    return self._err(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Bad Range")
                if s == "":
                    start = max(0, size - int(e))
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


# ---------- portal HTML ----------

PORTAL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Local IPTV Portal</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --bg: #0e1116; --panel: #161b22; --panel2: #1f2630; --border: #2a3140;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --accent2: #1f6feb;
    --danger: #f85149; --ok: #3fb950; --warn: #d29922;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); }
  header { position: sticky; top:0; z-index: 5; background: var(--panel);
           border-bottom: 1px solid var(--border); padding: 14px 22px;
           display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; }
  header .url { font-family: ui-monospace, monospace; font-size: 12px;
                background: var(--panel2); padding: 6px 10px; border-radius: 6px;
                color: var(--muted); cursor: pointer; }
  header .url:hover { color: var(--accent); }
  header input[type=search], header select {
    background: var(--panel2); color: var(--text); border:1px solid var(--border);
    padding: 7px 10px; border-radius: 6px; font-size: 14px; min-width: 180px; }
  header label { font-size: 13px; color: var(--muted); display: inline-flex;
                 align-items: center; gap: 6px; }
  main { padding: 18px 22px; }
  .grid { display: grid; gap: 16px;
          grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
  .card { background: var(--panel); border:1px solid var(--border); border-radius: 10px;
          overflow: hidden; display: flex; flex-direction: column; cursor: pointer;
          transition: transform .1s, border-color .1s; }
  .card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .thumb { aspect-ratio: 16/9; background: #000 center/cover no-repeat;
           position: relative; display:flex; align-items:center; justify-content:center;
           color: var(--muted); font-size: 36px; }
  .thumb .badges { position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; }
  .badge { background: rgba(0,0,0,.6); border-radius: 4px; padding: 2px 6px;
           font-size: 11px; }
  .badge.fav { color: gold; }
  .badge.hid { color: var(--muted); }
  .body { padding: 10px 12px; }
  .title { font-weight: 600; font-size: 14px; line-height: 1.3;
           overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
           -webkit-line-clamp: 2; -webkit-box-orient: vertical; min-height: 36px; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .empty { color: var(--muted); padding: 40px; text-align: center; }

  /* modal */
  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.7);
              display: none; align-items: center; justify-content: center;
              z-index: 20; padding: 20px; }
  .modal-bg.show { display: flex; }
  .modal { background: var(--panel); border:1px solid var(--border);
           border-radius: 12px; width: 100%; max-width: 900px; max-height: 92vh;
           overflow: auto; }
  .modal header { position: static; background: transparent; border:0;
                  padding: 18px 22px 0; }
  .modal-body { padding: 18px 22px 22px;
                display: grid; grid-template-columns: 320px 1fr; gap: 22px; }
  @media (max-width: 720px) { .modal-body { grid-template-columns: 1fr; } }
  .modal-thumb { aspect-ratio: 16/9; background: #000 center/cover no-repeat;
                 border-radius: 8px; border:1px solid var(--border);
                 display:flex; align-items:center; justify-content:center;
                 color: var(--muted); }
  .actions { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
  .row { display: flex; gap: 8px; }
  .row > * { flex: 1; }
  button, .btn { background: var(--panel2); color: var(--text);
                 border:1px solid var(--border); padding: 8px 12px;
                 border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent2); border-color: var(--accent2); }
  button.danger { color: var(--danger); }
  button:disabled { opacity:.5; cursor: not-allowed; }
  label.field { display: block; margin-bottom: 12px; font-size: 13px;
                color: var(--muted); }
  label.field input, label.field textarea, label.field select {
    width: 100%; margin-top: 4px; background: var(--bg); color: var(--text);
    border:1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    font-size: 14px; font-family: inherit; }
  label.field textarea { resize: vertical; min-height: 80px; }
  .checks { display: flex; gap: 16px; margin: 10px 0 16px; }
  .checks label { color: var(--text); }
  .modal-footer { padding: 14px 22px; border-top: 1px solid var(--border);
                  display: flex; justify-content: space-between; gap: 8px; }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: var(--panel2); border:1px solid var(--border);
           padding: 10px 16px; border-radius: 8px; z-index: 50; opacity: 0;
           transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; }
  .toast.err { border-color: var(--danger); color: var(--danger); }
  video { width: 100%; border-radius: 8px; background: #000; }
  .path { font-family: ui-monospace, monospace; font-size: 11px;
          color: var(--muted); word-break: break-all; }
</style>
</head>
<body>
<header>
  <h1>📺 Local IPTV</h1>
  <span class="url" id="playlistUrl" title="Click to copy">loading…</span>
  <input type="search" id="search" placeholder="Search title or path…">
  <select id="groupFilter"><option value="">All groups</option></select>
  <label><input type="checkbox" id="showHidden"> show hidden</label>
  <label><input type="checkbox" id="favOnly"> favorites only</label>
  <span style="flex:1"></span>
  <span id="status" style="color: var(--muted); font-size: 13px;"></span>
</header>
<main>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty" style="display:none">No videos match.</div>
</main>

<div class="modal-bg" id="modal">
  <div class="modal">
    <header><h2 id="mTitle" style="margin:0;font-size:18px"></h2></header>
    <div class="modal-body">
      <div>
        <div class="modal-thumb" id="mThumb">no thumbnail</div>
        <div class="actions">
          <div class="row">
            <button id="btnUpload">Upload image</button>
            <button id="btnAuto">Auto from video</button>
          </div>
          <div class="row">
            <input id="autoTs" value="00:00:30" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px" placeholder="HH:MM:SS">
            <button id="btnDelThumb" class="danger">Remove thumbnail</button>
          </div>
          <input type="file" id="fileInput" accept="image/jpeg,image/png,image/webp" style="display:none">
          <details style="margin-top:6px">
            <summary style="cursor:pointer;color:var(--muted);font-size:13px">Preview video</summary>
            <video id="preview" controls preload="none" style="margin-top:8px"></video>
          </details>
        </div>
      </div>
      <div>
        <label class="field">Title
          <input id="fTitle" type="text">
        </label>
        <label class="field">Group / Category
          <input id="fGroup" type="text" list="groupList">
          <datalist id="groupList"></datalist>
        </label>
        <div class="row">
          <label class="field">Year
            <input id="fYear" type="number" min="1900" max="2100">
          </label>
          <label class="field">Rating
            <input id="fRating" type="number" step="0.1" min="0" max="10">
          </label>
        </div>
        <label class="field">Description
          <textarea id="fDesc"></textarea>
        </label>
        <div class="checks">
          <label><input type="checkbox" id="fFav"> ★ Favorite</label>
          <label><input type="checkbox" id="fHidden"> Hide from playlist</label>
        </div>
        <div class="path" id="mPath"></div>
      </div>
    </div>
    <div class="modal-footer">
      <button id="btnClose">Close</button>
      <button id="btnSave" class="primary">Save changes</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let STATE = { videos: [], groups: [], current: null, ffmpeg: false };

function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('err', !!isErr);
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2200);
}

async function api(path, opts={}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch(e) {}
    throw new Error(msg);
  }
  return r.json();
}

async function load() {
  const data = await api('/api/videos');
  STATE.videos = data.videos;
  STATE.groups = data.groups;
  STATE.ffmpeg = data.ffmpeg;
  $('playlistUrl').textContent = data.playlist_url;
  $('playlistUrl').onclick = () => {
    navigator.clipboard.writeText(data.playlist_url);
    toast('Copied playlist URL');
  };
  const sel = $('groupFilter');
  sel.innerHTML = '<option value="">All groups</option>' +
    data.groups.map(g => `<option>${escapeHtml(g)}</option>`).join('');
  $('groupList').innerHTML = data.groups.map(g => `<option value="${escapeHtml(g)}">`).join('');
  $('status').textContent = `${data.videos.length} video(s)` + (data.ffmpeg ? '' : ' · ffmpeg not installed');
  render();
}

function escapeHtml(s) {
  return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function render() {
  const q = $('search').value.toLowerCase();
  const g = $('groupFilter').value;
  const showHidden = $('showHidden').checked;
  const favOnly = $('favOnly').checked;
  const list = STATE.videos.filter(v => {
    if (!showHidden && v.hidden) return false;
    if (favOnly && !v.favorite) return false;
    if (g && v.group !== g) return false;
    if (q && !(v.title.toLowerCase().includes(q) || v.path.toLowerCase().includes(q))) return false;
    return true;
  });
  $('empty').style.display = list.length ? 'none' : '';
  $('grid').innerHTML = list.map(v => `
    <div class="card" data-id="${v.id}">
      <div class="thumb" style="${v.thumbnail ? `background-image:url('${v.thumbnail}?t=${Date.now()}')` : ''}">
        ${v.thumbnail ? '' : '🎬'}
        <div class="badges">
          ${v.favorite ? '<span class="badge fav">★</span>' : ''}
          ${v.hidden ? '<span class="badge hid">hidden</span>' : ''}
        </div>
      </div>
      <div class="body">
        <div class="title">${escapeHtml(v.title)}</div>
        <div class="meta">${escapeHtml(v.group)}${v.year ? ' · '+v.year : ''}${v.rating ? ' · ★'+v.rating : ''}</div>
      </div>
    </div>
  `).join('');
  document.querySelectorAll('.card').forEach(c => {
    c.onclick = () => openEdit(c.dataset.id);
  });
}

function openEdit(id) {
  const v = STATE.videos.find(x => x.id === id);
  if (!v) return;
  STATE.current = v;
  $('mTitle').textContent = v.title;
  $('mPath').textContent = v.path;
  $('mThumb').style.backgroundImage = v.thumbnail ? `url('${v.thumbnail}?t=${Date.now()}')` : '';
  $('mThumb').textContent = v.thumbnail ? '' : 'no thumbnail';
  $('fTitle').value = v.title;
  $('fGroup').value = v.group;
  $('fYear').value = v.year || '';
  $('fRating').value = v.rating || '';
  $('fDesc').value = v.description || '';
  $('fFav').checked = v.favorite;
  $('fHidden').checked = v.hidden;
  $('preview').src = '/stream/' + v.path.split('/').map(encodeURIComponent).join('/');
  $('btnAuto').disabled = !STATE.ffmpeg;
  $('btnAuto').title = STATE.ffmpeg ? '' : 'Install ffmpeg on the server to enable';
  $('modal').classList.add('show');
}

function closeEdit() {
  $('modal').classList.remove('show');
  $('preview').pause();
  $('preview').removeAttribute('src');
  STATE.current = null;
}

async function refreshCurrent(updated) {
  const i = STATE.videos.findIndex(v => v.id === updated.id);
  if (i >= 0) STATE.videos[i] = updated;
  STATE.current = updated;
  $('mThumb').style.backgroundImage = updated.thumbnail ? `url('${updated.thumbnail}?t=${Date.now()}')` : '';
  $('mThumb').textContent = updated.thumbnail ? '' : 'no thumbnail';
  render();
}

$('btnClose').onclick = closeEdit;
$('modal').onclick = e => { if (e.target === $('modal')) closeEdit(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEdit(); });

$('btnSave').onclick = async () => {
  const v = STATE.current; if (!v) return;
  const patch = {
    title: $('fTitle').value.trim() || v.title,
    group: $('fGroup').value.trim() || 'Movies',
    description: $('fDesc').value,
    year: $('fYear').value ? Number($('fYear').value) : null,
    rating: $('fRating').value ? Number($('fRating').value) : null,
    favorite: $('fFav').checked,
    hidden: $('fHidden').checked,
  };
  try {
    const r = await api(`/api/video/${v.id}`, {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(patch)
    });
    await load();
    refreshCurrent(r);
    toast('Saved');
  } catch(e) { toast(e.message, true); }
};

$('btnUpload').onclick = () => $('fileInput').click();
$('fileInput').onchange = async () => {
  const f = $('fileInput').files[0]; if (!f || !STATE.current) return;
  try {
    const r = await api(`/api/video/${STATE.current.id}/thumbnail`, {
      method:'POST', headers:{'Content-Type': f.type}, body: f
    });
    await load(); refreshCurrent(r); toast('Thumbnail uploaded');
  } catch(e) { toast(e.message, true); }
  $('fileInput').value = '';
};

$('btnAuto').onclick = async () => {
  if (!STATE.current) return;
  const ts = encodeURIComponent($('autoTs').value || '00:00:30');
  $('btnAuto').disabled = true;
  try {
    const r = await api(`/api/video/${STATE.current.id}/auto-thumb?ts=${ts}`, {method:'POST'});
    await load(); refreshCurrent(r); toast('Auto-thumbnail generated');
  } catch(e) { toast(e.message, true); }
  $('btnAuto').disabled = !STATE.ffmpeg;
};

$('btnDelThumb').onclick = async () => {
  if (!STATE.current) return;
  try {
    const r = await api(`/api/video/${STATE.current.id}/thumbnail`, {method:'DELETE'});
    await load(); refreshCurrent(r); toast('Thumbnail removed');
  } catch(e) { toast(e.message, true); }
};

['search','groupFilter','showHidden','favOnly'].forEach(id =>
  $(id).addEventListener('input', render));

load().catch(e => toast(e.message, true));
</script>
</body></html>
"""


# ---------- bootstrap ----------

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
    ap = argparse.ArgumentParser(description="Local IPTV server + web portal.")
    ap.add_argument("--folder", "-f", required=True, help="Folder containing your movies")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    ap.add_argument("--port", "-p", type=int, default=8080, help="Port (default 8080)")
    args = ap.parse_args()

    global ROOT, META_DIR, META_FILE, THUMB_DIR, HAS_FFMPEG
    ROOT = os.path.abspath(args.folder)
    if not os.path.isdir(ROOT):
        print(f"error: folder not found: {ROOT}", file=sys.stderr)
        sys.exit(1)
    META_DIR = os.path.join(ROOT, ".iptv")
    META_FILE = os.path.join(META_DIR, "metadata.json")
    THUMB_DIR = os.path.join(META_DIR, "thumbs")
    os.makedirs(THUMB_DIR, exist_ok=True)
    HAS_FFMPEG = shutil.which("ffmpeg") is not None
    load_meta()

    ip = local_ip()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {ROOT}")
    print(f"  Portal:   http://{ip}:{args.port}/")
    print(f"  Playlist: http://{ip}:{args.port}/playlist.m3u")
    print(f"  ffmpeg:   {'found' if HAS_FFMPEG else 'NOT FOUND (auto-thumbnails disabled)'}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()

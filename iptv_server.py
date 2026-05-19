#!/usr/bin/env python3
"""Local IPTV server + web portal.

Scans a folder for video files, serves them over HTTP with byte-range support
(for IPTV Smarters Pro, VLC, browsers, etc.), exposes a /playlist.m3u, and
hosts a web portal at / for managing metadata: titles, groups, descriptions,
thumbnails (custom upload or auto-extract via ffmpeg), favorite/hidden flags.

Stdlib only. ffmpeg is optional; required for auto-thumbnail generation."""

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import secrets
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
AUTH_FILE = ""
META_LOCK = threading.Lock()
META = {"videos": {}}
HAS_FFMPEG = False

AUTH_ENABLED = True
AUTH_USER = "admin"
AUTH_HASH = ""  # "salt_b64:hash_b64"
AUTH_ITERS = 200_000

import time
STARTED_AT = time.time()
SERVER_VERSION = "2.1"
LISTEN_HOST = ""
LISTEN_PORT = 0


# ---------- auth ----------

def hash_password(pwd):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, AUTH_ITERS)
    return base64.b64encode(salt).decode() + ":" + base64.b64encode(h).decode()


def verify_password(pwd):
    if not AUTH_HASH:
        return False
    try:
        salt_b64, h_b64 = AUTH_HASH.split(":", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(h_b64)
    except (ValueError, base64.binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, AUTH_ITERS)
    return hmac.compare_digest(actual, expected)


def load_auth():
    global AUTH_USER, AUTH_HASH
    if not os.path.isfile(AUTH_FILE):
        return False
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        AUTH_USER = data.get("user") or "admin"
        AUTH_HASH = data.get("hash") or ""
        return bool(AUTH_HASH)
    except (OSError, json.JSONDecodeError):
        return False


def save_auth(user, pwd):
    global AUTH_USER, AUTH_HASH
    AUTH_USER = user
    AUTH_HASH = hash_password(pwd)
    os.makedirs(META_DIR, exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump({"user": AUTH_USER, "hash": AUTH_HASH}, f, indent=2)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


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


def _ffprobe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def auto_generate_thumb(rel, timestamp=None, mode="fixed"):
    """mode: 'fixed' uses timestamp, 'random' picks a random ts in middle 80%."""
    if not HAS_FFMPEG:
        return False
    full = os.path.join(ROOT, rel)
    if not os.path.isfile(full):
        return False
    os.makedirs(THUMB_DIR, exist_ok=True)
    out = os.path.join(THUMB_DIR, f"{_vid_id(rel)}_auto.jpg")

    candidates = []
    if mode == "random":
        dur = _ffprobe_duration(full)
        if dur and dur > 5:
            lo, hi = dur * 0.1, dur * 0.9
            for _ in range(3):
                candidates.append(f"{secrets.SystemRandom().uniform(lo, hi):.2f}")
    if timestamp:
        candidates.append(timestamp)
    candidates += ["00:00:30", "00:00:10", "00:00:01"]

    for ts in candidates:
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", ts, "-i", full,
                 "-frames:v", "1", "-q:v", "3", "-vf", "scale=640:-1", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


# ---------- system status ----------

def _ffmpeg_version():
    try:
        r = subprocess.run(["ffmpeg", "-version"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
        first = r.stdout.decode("utf-8", "replace").splitlines()[0] if r.stdout else ""
        return first
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _human_bytes(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def system_status():
    videos = discover_videos()
    records = [video_record(r) for r in videos]
    custom = sum(1 for r in records if r["has_custom_thumb"])
    auto = sum(1 for r in records if r["has_auto_thumb"])
    hidden = sum(1 for r in records if r["hidden"])
    favs = sum(1 for r in records if r["favorite"])

    du = shutil.disk_usage(ROOT) if os.path.isdir(ROOT) else None
    ffmpeg_path = shutil.which("ffmpeg") if HAS_FFMPEG else None
    ffprobe_path = shutil.which("ffprobe")

    install_hint = {
        "windows": "winget install Gyan.FFmpeg  (or: choco install ffmpeg)",
        "macos": "brew install ffmpeg",
        "linux": "sudo apt install ffmpeg   (Debian/Ubuntu)\nsudo dnf install ffmpeg   (Fedora)",
    }

    return {
        "server": {
            "version": SERVER_VERSION,
            "host": LISTEN_HOST, "port": LISTEN_PORT,
            "started_at": STARTED_AT,
            "uptime_seconds": int(time.time() - STARTED_AT),
            "pid": os.getpid(),
        },
        "python": {
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "executable": sys.executable,
        },
        "ffmpeg": {
            "installed": HAS_FFMPEG,
            "path": ffmpeg_path or "",
            "version": _ffmpeg_version() if HAS_FFMPEG else "",
            "ffprobe": ffprobe_path or "",
            "install_hint": install_hint,
        },
        "folder": {
            "path": ROOT,
            "exists": os.path.isdir(ROOT),
            "free_bytes": du.free if du else None,
            "total_bytes": du.total if du else None,
            "free_human": _human_bytes(du.free) if du else None,
            "total_human": _human_bytes(du.total) if du else None,
        },
        "library": {
            "total": len(records),
            "visible": len(records) - hidden,
            "hidden": hidden,
            "favorites": favs,
            "custom_thumbnails": custom,
            "auto_thumbnails": auto,
        },
        "auth": {
            "enabled": AUTH_ENABLED,
            "user": AUTH_USER if AUTH_ENABLED else None,
        },
    }


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


def build_m3u(base_url, auth_base_url=None, include_hidden=False):
    """base_url is shown to humans / used for tvg-logo; auth_base_url (with
    embedded user:pass@) is used for stream URLs so players auto-authenticate."""
    stream_base = auth_base_url or base_url
    logo_base = auth_base_url or base_url  # so Smarters can fetch logos too
    lines = ["#EXTM3U"]
    for rel in discover_videos():
        rec = video_record(rel)
        if rec["hidden"] and not include_hidden:
            continue
        logo = f"{logo_base}{rec['thumbnail']}" if rec["thumbnail"] else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{rec["id"]}" tvg-name="{rec["title"]}" '
            f'tvg-logo="{logo}" group-title="{rec["group"]}",{rec["title"]}'
        )
        lines.append(f"{stream_base}/stream/{urllib.parse.quote(rel)}")
    return "\n".join(lines) + "\n"


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "LocalIPTV/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    # auth ----------------------------------------------------
    def _check_auth(self):
        if not AUTH_ENABLED:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(h[6:]).decode("utf-8", errors="replace")
        except (ValueError, base64.binascii.Error):
            return False
        user, _, pwd = decoded.partition(":")
        user_ok = hmac.compare_digest(user, AUTH_USER)
        pass_ok = verify_password(pwd)
        return user_ok and pass_ok

    def _require_auth(self):
        if self._check_auth():
            return True
        body = b"Authentication required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Local IPTV", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        return False

    # routing -------------------------------------------------
    def do_GET(self):
        if not self._require_auth():
            return
        p = urllib.parse.urlparse(self.path)
        path = p.path
        try:
            if path == "/" or path == "/index.html":
                return self._html(PORTAL_HTML)
            if path == "/admin":
                return self._html(ADMIN_HTML)
            if path == "/api/status":
                return self._json(system_status())
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
        if not self._require_auth():
            return
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/stream/"):
            rel = urllib.parse.unquote(p.path[len("/stream/"):])
            return self._serve_file(rel, head_only=True)
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_PATCH(self):
        if not self._require_auth():
            return
        p = urllib.parse.urlparse(self.path)
        path = p.path
        if path.startswith("/api/video/") and path.count("/") == 3:
            return self._api_patch_video(path.split("/")[-1])
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self):
        if not self._require_auth():
            return
        p = urllib.parse.urlparse(self.path)
        path = p.path
        m = re.match(r"^/api/video/([^/]+)/thumbnail$", path)
        if m:
            return self._api_upload_thumb(m.group(1))
        m = re.match(r"^/api/video/([^/]+)/auto-thumb$", path)
        if m:
            return self._api_auto_thumb(m.group(1), urllib.parse.parse_qs(p.query))
        if path == "/api/auth/password":
            return self._api_change_password()
        if path == "/api/ffmpeg-test":
            return self._api_ffmpeg_test()
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    def do_DELETE(self):
        if not self._require_auth():
            return
        p = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/video/([^/]+)/thumbnail$", p.path)
        if m:
            return self._api_delete_thumb(m.group(1))
        return self._err(HTTPStatus.NOT_FOUND, "Not Found")

    # helpers -------------------------------------------------
    def _base_url(self):
        host = self.headers.get("Host") or "localhost"
        return f"http://{host}"

    def _auth_base_url(self):
        """Same as _base_url but with user:pass@ embedded, if request was
        authenticated. Used inside the M3U so external players auto-auth."""
        if not AUTH_ENABLED:
            return self._base_url()
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(h[6:]).decode("utf-8", errors="replace")
        except (ValueError, base64.binascii.Error):
            return None
        user, _, pwd = decoded.partition(":")
        host = self.headers.get("Host") or "localhost"
        creds = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pwd, safe='')}"
        return f"http://{creds}@{host}"

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
        mode = q.get("mode", ["fixed"])[0]
        ts = q.get("ts", [None])[0]
        ok = auto_generate_thumb(rel, ts, mode=mode)
        if not ok:
            return self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "Thumbnail generation failed")
        self._json(video_record(rel))

    def _api_change_password(self):
        body = self._read_body(max_bytes=4096)
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._err(HTTPStatus.BAD_REQUEST, "Invalid JSON")
        cur = data.get("current") or ""
        new = data.get("new") or ""
        if not AUTH_ENABLED:
            return self._err(HTTPStatus.BAD_REQUEST, "Auth is disabled")
        if not verify_password(cur):
            return self._err(HTTPStatus.UNAUTHORIZED, "Current password incorrect")
        if len(new) < 6:
            return self._err(HTTPStatus.BAD_REQUEST, "New password must be at least 6 chars")
        save_auth(AUTH_USER, new)
        self._json({"ok": True})

    def _api_ffmpeg_test(self):
        if not HAS_FFMPEG:
            return self._json({"ok": False, "error": "ffmpeg not installed"})
        try:
            r = subprocess.run(["ffmpeg", "-version"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=5, check=False)
            out = r.stdout.decode("utf-8", "replace")
            self._json({"ok": r.returncode == 0, "output": out[:2000]})
        except (OSError, subprocess.TimeoutExpired) as e:
            self._json({"ok": False, "error": str(e)})

    def _api_delete_thumb(self, vid):
        rel = self._rel_for_id(vid)
        if not rel:
            return self._err(HTTPStatus.NOT_FOUND, "Video not found")
        delete_custom_thumb(rel)
        self._json(video_record(rel))

    # serving -------------------------------------------------
    def _serve_playlist(self):
        data = build_m3u(self._base_url(), self._auth_base_url()).encode("utf-8")
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

COMMON_CSS = r"""
  :root {
    --bg: #0e1116; --panel: #161b22; --panel2: #1f2630; --border: #2a3140;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --accent2: #1f6feb;
    --danger: #f85149; --ok: #3fb950; --warn: #d29922;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); }
  header.top { position: sticky; top:0; z-index: 5; background: var(--panel);
           border-bottom: 1px solid var(--border); padding: 14px 22px;
           display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  header.top h1 { font-size: 18px; margin: 0; }
  header.top .url { font-family: ui-monospace, monospace; font-size: 12px;
                background: var(--panel2); padding: 6px 10px; border-radius: 6px;
                color: var(--muted); cursor: pointer; }
  header.top .url:hover { color: var(--accent); }
  header.top input[type=search], header.top select {
    background: var(--panel2); color: var(--text); border:1px solid var(--border);
    padding: 7px 10px; border-radius: 6px; font-size: 14px; min-width: 180px; }
  header.top label { font-size: 13px; color: var(--muted); display: inline-flex;
                 align-items: center; gap: 6px; }
  header.top a.nav { color: var(--accent); text-decoration: none; font-size: 14px;
                 padding: 6px 10px; border:1px solid var(--border); border-radius: 6px; }
  header.top a.nav:hover { background: var(--panel2); }
  button, .btn { background: var(--panel2); color: var(--text);
                 border:1px solid var(--border); padding: 8px 12px;
                 border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent2); border-color: var(--accent2); }
  button.danger { color: var(--danger); }
  button:disabled { opacity:.5; cursor: not-allowed; }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: var(--panel2); border:1px solid var(--border);
           padding: 10px 16px; border-radius: 8px; z-index: 50; opacity: 0;
           transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; }
  .toast.err { border-color: var(--danger); color: var(--danger); }
  .toast.ok { border-color: var(--ok); color: var(--ok); }
"""

PORTAL_HTML = (r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Local IPTV Portal</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__
  main { padding: 18px 22px; }
  .grid { display: grid; gap: 16px;
          grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
  .card { background: var(--panel); border:1px solid var(--border); border-radius: 10px;
          overflow: hidden; display: flex; flex-direction: column; cursor: pointer;
          transition: transform .1s, border-color .1s; position: relative; }
  .card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .card.drag { border-color: var(--ok); box-shadow: 0 0 0 2px var(--ok); }
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

  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.7);
              display: none; align-items: center; justify-content: center;
              z-index: 20; padding: 20px; }
  .modal-bg.show { display: flex; }
  .modal { background: var(--panel); border:1px solid var(--border);
           border-radius: 12px; width: 100%; max-width: 980px; max-height: 92vh;
           overflow: auto; }
  .modal .mhead { padding: 18px 22px 0; }
  .modal-body { padding: 18px 22px 22px;
                display: grid; grid-template-columns: 340px 1fr; gap: 22px; }
  @media (max-width: 760px) { .modal-body { grid-template-columns: 1fr; } }
  .modal-thumb { aspect-ratio: 16/9; background: #000 center/cover no-repeat;
                 border-radius: 8px; border:2px dashed var(--border);
                 display:flex; align-items:center; justify-content:center;
                 color: var(--muted); transition: border-color .15s; position: relative; }
  .modal-thumb.drag { border-color: var(--ok); background-color: rgba(63,185,80,.08); }
  .modal-thumb .hint { position: absolute; bottom: 8px; right: 10px;
                       background: rgba(0,0,0,.6); padding: 3px 8px; border-radius: 4px;
                       font-size: 11px; color: #ddd; }
  .actions { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
  .row { display: flex; gap: 8px; }
  .row > * { flex: 1; }
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
  video { width: 100%; border-radius: 8px; background: #000; max-height: 360px; }
  .scrubbox { margin-top: 10px; padding: 10px; background: var(--bg);
              border:1px solid var(--border); border-radius: 8px; }
  .scrubbox.hidden { display: none; }
  .scrubbox .hint { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .path { font-family: ui-monospace, monospace; font-size: 11px;
          color: var(--muted); word-break: break-all; }
</style>
</head>
<body>
<header class="top">
  <h1>📺 Local IPTV</h1>
  <span class="url" id="playlistUrl" title="Click to copy">loading…</span>
  <input type="search" id="search" placeholder="Search title or path…">
  <select id="groupFilter"><option value="">All groups</option></select>
  <label><input type="checkbox" id="showHidden"> show hidden</label>
  <label><input type="checkbox" id="favOnly"> favorites only</label>
  <span style="flex:1"></span>
  <a class="nav" href="/admin">⚙ Admin</a>
  <span id="status" style="color: var(--muted); font-size: 13px;"></span>
</header>
<main>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty" style="display:none">No videos match.</div>
  <p style="color:var(--muted);font-size:12px;margin-top:24px">
    Tip: drag an image file onto any card to set its thumbnail. Click a card to edit.
  </p>
</main>

<div class="modal-bg" id="modal">
  <div class="modal">
    <div class="mhead"><h2 id="mTitle" style="margin:0;font-size:18px"></h2></div>
    <div class="modal-body">
      <div>
        <div class="modal-thumb" id="mThumb">
          no thumbnail
          <div class="hint">drop image here</div>
        </div>
        <div class="actions">
          <div class="row">
            <button id="btnUpload">📁 Upload</button>
            <button id="btnRandom" title="Pick a random frame from the video (browser)">🎲 Random frame</button>
          </div>
          <div class="row">
            <button id="btnScrubMode">🎬 Pick from video</button>
            <button id="btnDelThumb" class="danger">🗑 Remove</button>
          </div>
          <input type="file" id="fileInput" accept="image/*" style="display:none">
          <div class="scrubbox hidden" id="scrubBox">
            <div class="hint">Scrub to the frame you want, then click capture.</div>
            <video id="scrubVid" controls preload="metadata"></video>
            <div class="row" style="margin-top:8px">
              <button id="btnCapture" class="primary">📸 Use this frame</button>
              <button id="btnFFAuto" title="Use ffmpeg on the server (works for any format)">⚙ Server auto</button>
            </div>
            <div id="ffWarn" style="display:none;color:var(--warn);font-size:12px;margin-top:6px"></div>
          </div>
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

function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (kind === 'err' ? 'err' : kind === 'ok' ? 'ok' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = 'toast', 2400);
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

function escapeHtml(s) {
  return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function streamUrl(rel) {
  return '/stream/' + rel.split('/').map(encodeURIComponent).join('/');
}

async function load() {
  const data = await api('/api/videos');
  STATE.videos = data.videos;
  STATE.groups = data.groups;
  STATE.ffmpeg = data.ffmpeg;
  $('playlistUrl').textContent = data.playlist_url;
  $('playlistUrl').onclick = () => {
    navigator.clipboard.writeText(data.playlist_url);
    toast('Copied playlist URL', 'ok');
  };
  const sel = $('groupFilter');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All groups</option>' +
    data.groups.map(g => `<option ${g===cur?'selected':''}>${escapeHtml(g)}</option>`).join('');
  $('groupList').innerHTML = data.groups.map(g => `<option value="${escapeHtml(g)}">`).join('');
  $('status').textContent = `${data.videos.length} video(s)` + (data.ffmpeg ? '' : ' · no ffmpeg');
  render();
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
    enableDropOnCard(c);
  });
}

function openEdit(id) {
  const v = STATE.videos.find(x => x.id === id);
  if (!v) return;
  STATE.current = v;
  $('mTitle').textContent = v.title;
  $('mPath').textContent = v.path;
  setThumbDisplay($('mThumb'), v.thumbnail);
  $('fTitle').value = v.title;
  $('fGroup').value = v.group;
  $('fYear').value = v.year || '';
  $('fRating').value = v.rating || '';
  $('fDesc').value = v.description || '';
  $('fFav').checked = v.favorite;
  $('fHidden').checked = v.hidden;
  $('scrubBox').classList.add('hidden');
  $('scrubVid').removeAttribute('src');
  $('ffWarn').style.display = 'none';
  $('btnFFAuto').disabled = !STATE.ffmpeg;
  $('btnFFAuto').title = STATE.ffmpeg ? 'Server-side ffmpeg auto-thumbnail' : 'Install ffmpeg on the server';
  $('modal').classList.add('show');
}

function setThumbDisplay(el, url) {
  el.style.backgroundImage = url ? `url('${url}?t=${Date.now()}')` : '';
  el.firstChild && (el.firstChild.nodeValue = url ? '' : 'no thumbnail');
  if (url) { el.innerHTML = '<div class="hint">drop image here</div>'; }
  else { el.innerHTML = 'no thumbnail<div class="hint">drop image here</div>'; }
}

function closeEdit() {
  $('modal').classList.remove('show');
  $('scrubVid').pause(); $('scrubVid').removeAttribute('src');
  STATE.current = null;
}

async function refreshCurrent(updated) {
  const i = STATE.videos.findIndex(v => v.id === updated.id);
  if (i >= 0) STATE.videos[i] = updated;
  STATE.current = updated;
  setThumbDisplay($('mThumb'), updated.thumbnail);
  render();
}

async function uploadThumbBlob(blob) {
  if (!STATE.current) return;
  const r = await api(`/api/video/${STATE.current.id}/thumbnail`, {
    method:'POST', headers:{'Content-Type': blob.type || 'image/jpeg'}, body: blob
  });
  await refreshCurrent(r);
}

async function uploadThumbFor(id, blob) {
  const r = await api(`/api/video/${id}/thumbnail`, {
    method:'POST', headers:{'Content-Type': blob.type || 'image/jpeg'}, body: blob
  });
  const i = STATE.videos.findIndex(v => v.id === id);
  if (i >= 0) STATE.videos[i] = r;
  if (STATE.current && STATE.current.id === id) {
    STATE.current = r; setThumbDisplay($('mThumb'), r.thumbnail);
  }
  render();
}

function captureFrame(videoEl) {
  return new Promise((res, rej) => {
    if (!videoEl.videoWidth) return rej(new Error('Video not ready'));
    const c = document.createElement('canvas');
    c.width = videoEl.videoWidth; c.height = videoEl.videoHeight;
    c.getContext('2d').drawImage(videoEl, 0, 0);
    c.toBlob(b => b ? res(b) : rej(new Error('Capture failed')), 'image/jpeg', 0.9);
  });
}

async function browserRandomFrame(rel) {
  return new Promise((res, rej) => {
    const v = document.createElement('video');
    v.muted = true; v.preload = 'metadata'; v.crossOrigin = 'anonymous';
    v.src = streamUrl(rel);
    let done = false;
    const fail = (e) => { if (done) return; done = true; rej(e); };
    v.onerror = () => fail(new Error('Browser cannot decode this video format'));
    setTimeout(() => fail(new Error('Timeout loading video')), 20000);
    v.onloadedmetadata = () => {
      const dur = v.duration;
      if (!isFinite(dur) || dur < 1) return fail(new Error('Unknown duration'));
      const lo = dur > 30 ? dur * 0.1 : 0;
      const hi = dur > 30 ? dur * 0.9 : dur;
      v.currentTime = lo + Math.random() * (hi - lo);
    };
    v.onseeked = async () => {
      if (done) return;
      try { const blob = await captureFrame(v); done = true; res(blob); }
      catch (e) { fail(e); }
    };
  });
}

// drop targets
function enableDrop(el, onFile) {
  ['dragenter','dragover'].forEach(ev => el.addEventListener(ev, e => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.items||[]).some(i => i.kind==='file')) return;
    e.preventDefault(); el.classList.add('drag');
  }));
  ['dragleave','dragend'].forEach(ev => el.addEventListener(ev, () => el.classList.remove('drag')));
  el.addEventListener('drop', async e => {
    e.preventDefault(); el.classList.remove('drag');
    const f = e.dataTransfer.files[0];
    if (!f) return;
    if (!f.type.startsWith('image/')) return toast('Drop an image file', 'err');
    try { await onFile(f); toast('Thumbnail updated', 'ok'); }
    catch (err) { toast(err.message, 'err'); }
  });
}

function enableDropOnCard(cardEl) {
  enableDrop(cardEl, f => uploadThumbFor(cardEl.dataset.id, f));
}

enableDrop($('mThumb'), uploadThumbBlob);

// modal wiring
$('btnClose').onclick = closeEdit;
$('modal').onclick = e => { if (e.target === $('modal')) closeEdit(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('modal').classList.contains('show')) closeEdit(); });

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
      method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(patch)
    });
    await load(); refreshCurrent(r); toast('Saved', 'ok');
  } catch(e) { toast(e.message, 'err'); }
};

$('btnUpload').onclick = () => $('fileInput').click();
$('fileInput').onchange = async () => {
  const f = $('fileInput').files[0]; if (!f || !STATE.current) return;
  try { await uploadThumbBlob(f); toast('Thumbnail uploaded', 'ok'); }
  catch(e) { toast(e.message, 'err'); }
  $('fileInput').value = '';
};

$('btnRandom').onclick = async () => {
  if (!STATE.current) return;
  $('btnRandom').disabled = true;
  try {
    const blob = await browserRandomFrame(STATE.current.path);
    await uploadThumbBlob(blob);
    toast('Random frame saved', 'ok');
  } catch(e) {
    if (STATE.ffmpeg) {
      try {
        const r = await api(`/api/video/${STATE.current.id}/auto-thumb?mode=random`, {method:'POST'});
        await refreshCurrent(r);
        toast('Random frame (via ffmpeg) saved', 'ok');
      } catch(e2) { toast(e2.message, 'err'); }
    } else {
      toast(e.message + ' (install ffmpeg for fallback)', 'err');
    }
  }
  $('btnRandom').disabled = false;
};

$('btnScrubMode').onclick = () => {
  const box = $('scrubBox');
  const hidden = box.classList.toggle('hidden');
  const v = $('scrubVid');
  if (!hidden) {
    v.src = streamUrl(STATE.current.path);
    v.onerror = () => {
      $('ffWarn').style.display = '';
      $('ffWarn').textContent = STATE.ffmpeg
        ? 'Browser cannot play this file. Use "Server auto" or "Random frame" (will use ffmpeg).'
        : 'Browser cannot play this file. Install ffmpeg on the server to capture from any format.';
    };
  } else {
    v.pause(); v.removeAttribute('src');
  }
};

$('btnCapture').onclick = async () => {
  try {
    const blob = await captureFrame($('scrubVid'));
    await uploadThumbBlob(blob);
    toast('Frame captured', 'ok');
  } catch(e) { toast(e.message, 'err'); }
};

$('btnFFAuto').onclick = async () => {
  if (!STATE.current) return;
  $('btnFFAuto').disabled = true;
  try {
    const r = await api(`/api/video/${STATE.current.id}/auto-thumb?mode=random`, {method:'POST'});
    await refreshCurrent(r);
    toast('Server thumbnail generated', 'ok');
  } catch(e) { toast(e.message, 'err'); }
  $('btnFFAuto').disabled = !STATE.ffmpeg;
};

$('btnDelThumb').onclick = async () => {
  if (!STATE.current) return;
  try {
    const r = await api(`/api/video/${STATE.current.id}/thumbnail`, {method:'DELETE'});
    await refreshCurrent(r); toast('Thumbnail removed', 'ok');
  } catch(e) { toast(e.message, 'err'); }
};

['search','groupFilter','showHidden','favOnly'].forEach(id =>
  $(id).addEventListener('input', render));

load().catch(e => toast(e.message, 'err'));
</script>
</body></html>
""").replace("__CSS__", COMMON_CSS)


ADMIN_HTML = (r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Local IPTV — Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__
  main { padding: 18px 22px; max-width: 1100px; margin: 0 auto; }
  .cards { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .card { background: var(--panel); border:1px solid var(--border); border-radius: 10px;
          padding: 16px 18px; }
  .card h3 { margin: 0 0 12px; font-size: 14px; color: var(--muted);
             text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }
  .row2 { display: grid; grid-template-columns: 110px 1fr; gap: 6px 14px;
          font-size: 14px; line-height: 1.5; }
  .row2 .k { color: var(--muted); }
  .row2 .v { word-break: break-all; font-family: ui-monospace, monospace; font-size: 13px; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 99px; font-size: 12px;
          font-weight: 600; }
  .pill.ok { background: rgba(63,185,80,.15); color: var(--ok); }
  .pill.bad { background: rgba(248,81,73,.15); color: var(--danger); }
  .pill.warn { background: rgba(210,153,34,.15); color: var(--warn); }
  pre { background: var(--bg); border:1px solid var(--border); border-radius: 6px;
        padding: 10px; font-size: 12px; overflow: auto; max-height: 220px;
        white-space: pre-wrap; word-break: break-all; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
           margin-top: 4px; }
  .stat { background: var(--bg); border:1px solid var(--border); border-radius: 8px;
          padding: 10px; text-align: center; }
  .stat .n { font-size: 22px; font-weight: 700; }
  .stat .l { font-size: 11px; color: var(--muted); text-transform: uppercase; }
  .install { background: var(--bg); border-left: 3px solid var(--warn);
             padding: 8px 12px; margin-top: 10px; font-size: 13px; }
  .install code { display: block; background: transparent; padding: 2px 0; }
  dialog { background: var(--panel); color: var(--text); border:1px solid var(--border);
           border-radius: 12px; padding: 22px; width: min(420px, 90vw); }
  dialog::backdrop { background: rgba(0,0,0,.6); }
  dialog input { width: 100%; margin: 6px 0 12px; padding: 8px 10px;
                 background: var(--bg); color: var(--text); border:1px solid var(--border);
                 border-radius: 6px; font-size: 14px; }
</style>
</head>
<body>
<header class="top">
  <h1>⚙ Admin Dashboard</h1>
  <span style="flex:1"></span>
  <a class="nav" href="/">← Back to portal</a>
  <button id="btnRefresh">🔄 Refresh</button>
</header>
<main>
  <div class="cards" id="cards">Loading…</div>
</main>

<dialog id="pwdDlg">
  <h3 style="margin:0 0 12px">Change password</h3>
  <label style="font-size:13px;color:var(--muted)">Current password
    <input type="password" id="pwCur"></label>
  <label style="font-size:13px;color:var(--muted)">New password (min 6 chars)
    <input type="password" id="pwNew"></label>
  <div style="display:flex;gap:8px;justify-content:flex-end">
    <button id="pwCancel">Cancel</button>
    <button id="pwSave" class="primary">Change</button>
  </div>
</dialog>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (kind === 'err' ? 'err' : kind === 'ok' ? 'ok' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = 'toast', 2400);
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
function esc(s) { return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pill(ok, label) {
  return `<span class="pill ${ok ? 'ok' : 'bad'}">${label}</span>`;
}
function fmtUptime(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  if (s < 86400) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  return Math.floor(s/86400) + 'd ' + Math.floor((s%86400)/3600) + 'h';
}

async function loadStatus() {
  let s;
  try { s = await api('/api/status'); }
  catch(e) { $('cards').innerHTML = `<div class="card">Failed: ${esc(e.message)}</div>`; return; }

  const ff = s.ffmpeg;
  const ffInstall = !ff.installed ? `
    <div class="install">
      <b>Install ffmpeg:</b>
      <code>Windows: ${esc(ff.install_hint.windows)}</code>
      <code>macOS:   ${esc(ff.install_hint.macos)}</code>
      <code>Linux:   ${esc(ff.install_hint.linux.split('\n').join(' / '))}</code>
    </div>` : '';

  $('cards').innerHTML = `
    <div class="card">
      <h3>🟢 Server</h3>
      <div class="row2">
        <span class="k">Status</span><span class="v">${pill(true, 'RUNNING')}</span>
        <span class="k">Version</span><span class="v">${esc(s.server.version)}</span>
        <span class="k">Listening</span><span class="v">${esc(s.server.host)}:${s.server.port}</span>
        <span class="k">Uptime</span><span class="v">${fmtUptime(s.server.uptime_seconds)}</span>
        <span class="k">PID</span><span class="v">${s.server.pid}</span>
      </div>
    </div>

    <div class="card">
      <h3>🐍 Python</h3>
      <div class="row2">
        <span class="k">Status</span><span class="v">${pill(true, 'OK')}</span>
        <span class="k">Version</span><span class="v">${esc(s.python.version)}</span>
        <span class="k">Implementation</span><span class="v">${esc(s.python.implementation)}</span>
        <span class="k">Executable</span><span class="v">${esc(s.python.executable)}</span>
      </div>
    </div>

    <div class="card">
      <h3>🎞 ffmpeg <small style="font-weight:400;color:var(--muted)">(thumbnail generation)</small></h3>
      <div class="row2">
        <span class="k">Status</span><span class="v">${ff.installed ? pill(true,'INSTALLED') : pill(false,'NOT INSTALLED')}</span>
        ${ff.installed ? `
          <span class="k">Path</span><span class="v">${esc(ff.path)}</span>
          <span class="k">ffprobe</span><span class="v">${ff.ffprobe ? esc(ff.ffprobe) : '<span class="pill warn">missing</span>'}</span>
          <span class="k">Version</span><span class="v">${esc(ff.version)}</span>` : ''}
      </div>
      ${ffInstall}
      <div class="actions">
        <button id="btnTestFF" ${ff.installed?'':'disabled'}>Test ffmpeg</button>
      </div>
      <pre id="ffOut" style="display:none"></pre>
    </div>

    <div class="card">
      <h3>📁 Movies Folder</h3>
      <div class="row2">
        <span class="k">Status</span><span class="v">${s.folder.exists ? pill(true,'OK') : pill(false,'MISSING')}</span>
        <span class="k">Path</span><span class="v">${esc(s.folder.path)}</span>
        <span class="k">Free space</span><span class="v">${esc(s.folder.free_human || '—')} / ${esc(s.folder.total_human || '—')}</span>
      </div>
    </div>

    <div class="card">
      <h3>🎬 Library</h3>
      <div class="stats">
        <div class="stat"><div class="n">${s.library.total}</div><div class="l">Total</div></div>
        <div class="stat"><div class="n">${s.library.visible}</div><div class="l">Visible</div></div>
        <div class="stat"><div class="n">${s.library.hidden}</div><div class="l">Hidden</div></div>
        <div class="stat"><div class="n">${s.library.favorites}</div><div class="l">Favorites</div></div>
        <div class="stat"><div class="n">${s.library.custom_thumbnails}</div><div class="l">Custom thumbs</div></div>
        <div class="stat"><div class="n">${s.library.auto_thumbnails}</div><div class="l">Auto thumbs</div></div>
      </div>
    </div>

    <div class="card">
      <h3>🔒 Authentication</h3>
      <div class="row2">
        <span class="k">Status</span><span class="v">${s.auth.enabled ? pill(true,'ENABLED') : pill(false,'DISABLED')}</span>
        ${s.auth.enabled ? `<span class="k">User</span><span class="v">${esc(s.auth.user)}</span>` : ''}
      </div>
      <div class="actions">
        ${s.auth.enabled ? '<button id="btnPwd" class="primary">Change password</button>' : ''}
      </div>
      <p style="font-size:12px;color:var(--muted);margin:12px 0 0">
        ${s.auth.enabled
          ? 'Use <code>--reset-password</code> on the command line to wipe and regenerate.'
          : 'Auth disabled. Restart without <code>--no-auth</code> to enable.'}
      </p>
    </div>
  `;

  const tf = $('btnTestFF');
  if (tf) tf.onclick = async () => {
    tf.disabled = true;
    try {
      const r = await api('/api/ffmpeg-test', {method:'POST'});
      $('ffOut').style.display = '';
      $('ffOut').textContent = r.output || r.error || (r.ok ? 'OK' : 'failed');
    } catch(e) { toast(e.message, 'err'); }
    tf.disabled = false;
  };
  const pw = $('btnPwd');
  if (pw) pw.onclick = () => $('pwdDlg').showModal();
}

$('btnRefresh').onclick = loadStatus;
$('pwCancel').onclick = () => $('pwdDlg').close();
$('pwSave').onclick = async () => {
  const cur = $('pwCur').value, nw = $('pwNew').value;
  if (nw.length < 6) return toast('New password must be 6+ chars', 'err');
  try {
    await api('/api/auth/password', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({current: cur, new: nw})
    });
    $('pwdDlg').close();
    $('pwCur').value = ''; $('pwNew').value = '';
    toast('Password changed — you may need to re-enter it on next request', 'ok');
  } catch(e) { toast(e.message, 'err'); }
};

loadStatus();
setInterval(loadStatus, 10000);
</script>
</body></html>
""").replace("__CSS__", COMMON_CSS)


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
    ap.add_argument("--user", default=None, help="Portal username (default admin)")
    ap.add_argument("--password", default=None,
                    help="Set/change portal password. If omitted and no password "
                         "is saved yet, one is generated and printed.")
    ap.add_argument("--reset-password", action="store_true",
                    help="Discard saved password and prompt/generate a new one")
    ap.add_argument("--no-auth", action="store_true",
                    help="Disable authentication (NOT recommended)")
    args = ap.parse_args()

    global ROOT, META_DIR, META_FILE, THUMB_DIR, AUTH_FILE
    global HAS_FFMPEG, AUTH_ENABLED, AUTH_USER
    ROOT = os.path.abspath(args.folder)
    if not os.path.isdir(ROOT):
        print(f"error: folder not found: {ROOT}", file=sys.stderr)
        sys.exit(1)
    META_DIR = os.path.join(ROOT, ".iptv")
    META_FILE = os.path.join(META_DIR, "metadata.json")
    THUMB_DIR = os.path.join(META_DIR, "thumbs")
    AUTH_FILE = os.path.join(META_DIR, "auth.json")
    os.makedirs(THUMB_DIR, exist_ok=True)
    HAS_FFMPEG = shutil.which("ffmpeg") is not None
    load_meta()

    AUTH_ENABLED = not args.no_auth
    if AUTH_ENABLED:
        existing = load_auth()
        if args.reset_password and os.path.isfile(AUTH_FILE):
            os.remove(AUTH_FILE)
            existing = False
        user = args.user or (AUTH_USER if existing else "admin")
        if args.password:
            save_auth(user, args.password)
            print(f"Password set for user '{user}'.")
        elif not existing:
            pwd = secrets.token_urlsafe(12)
            save_auth(user, pwd)
            print("=" * 60)
            print(" No password set — generated one for you:")
            print(f"   Username: {user}")
            print(f"   Password: {pwd}")
            print(" Save it now. Reset later with --reset-password or change")
            print(" with --password NEWPASS.")
            print("=" * 60)
        elif args.user and args.user != AUTH_USER:
            # username changed without password change — keep hash, swap user
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["user"] = args.user
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            AUTH_USER = args.user

    ip = local_ip()
    global LISTEN_HOST, LISTEN_PORT, STARTED_AT
    LISTEN_HOST = ip
    LISTEN_PORT = args.port
    STARTED_AT = time.time()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {ROOT}")
    print(f"  Portal:   http://{ip}:{args.port}/")
    print(f"  Playlist: http://{ip}:{args.port}/playlist.m3u")
    if AUTH_ENABLED:
        print(f"  Auth:     Basic (user '{AUTH_USER}')")
        print(f"  Smarters: http://{AUTH_USER}:<password>@{ip}:{args.port}/playlist.m3u")
    else:
        print(f"  Auth:     DISABLED")
    print(f"  ffmpeg:   {'found' if HAS_FFMPEG else 'NOT FOUND (server-side auto-thumb disabled — browser capture still works)'}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()

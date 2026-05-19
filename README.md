# Local IPTV Server

A single-file Python web server that turns a folder of video files into an
M3U playlist for IPTV Smarters Pro (Firestick, phone, etc.) plus a browser
portal for managing titles, thumbnails, groups, favorites, and more.

No external dependencies — uses only the Python standard library.
Optional: `ffmpeg` (for server-side thumbnail extraction of formats the
browser can't decode).

## Quick start

```bash
python3 iptv_server.py --folder "/path/to/movies" --port 8080
```

First run prints a generated password. Save it.

Then in a browser: `http://<your-pc-ip>:8080/` and sign in.

In IPTV Smarters Pro → Add New User → Load M3U URL:
`http://<your-pc-ip>:8080/playlist.m3u`

The M3U embeds your credentials so Smarters authenticates automatically.

## Features

- **Web portal** with grid view, search, group filter, favorites.
- **Edit per video**: title, group, year, rating, description, favorite, hidden.
- **Thumbnails**:
  - Drag-and-drop an image onto any card.
  - Drop *multiple* images onto the page to bulk-assign by filename match
    (e.g. `Heat.jpg` → matches `Heat.mp4`).
  - Random frame (browser picks; ffmpeg fallback for tricky formats).
  - Scrub the video and capture the exact frame you want.
  - Bulk-generate missing thumbnails for the whole library (background job
    with progress bar).
- **Admin dashboard** (`/admin`) showing server uptime, Python version,
  ffmpeg status with per-OS install hints, disk space, library stats,
  change-password button.
- **Auth**: HTML login page (no browser auth popup), HTTP-Basic fallback so
  IPTV Smarters can still authenticate via URL-embedded credentials.
- **Hidden videos** are excluded from the playlist but stay in the portal.
- All metadata stored in `<folder>/.iptv/` — original files never modified.

## CLI options

```
--folder, -f      Folder containing your movies (required)
--port, -p        Port (default 8080)
--host            Bind address (default 0.0.0.0 = all interfaces)
--user            Portal username (default admin)
--password        Set/change portal password
--reset-password  Discard saved password and regenerate
--no-auth         Disable authentication (NOT recommended)
```

## Running on boot

- **Windows**: edit `scripts/iptv-server.bat` (set FOLDER and PORT), then
  put a shortcut to it in the Startup folder (`shell:startup`).
- **Linux**: edit `scripts/iptv-server.service` (set User, paths), then:
  ```
  sudo cp scripts/iptv-server.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now iptv-server
  ```

## ffmpeg (optional)

Required for server-side thumbnail extraction (bulk generation, formats the
browser can't decode like many MKVs/AVIs).

- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

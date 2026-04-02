#!/usr/bin/env python3
"""
fastdl server — local web UI for downloading media at max speed.
No API keys. No cloud. Everything local.

Usage:  python3 server.py
Then open http://localhost:8888
"""

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    print("Installing websockets...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets
    from websockets.asyncio.server import serve as ws_serve

# Import database layer
from db import init_db, log_session, log_download, log_search, detect_platform, get_analytics

# ── Config ──────────────────────────────────────────────────────────────────
HTTP_PORT = 8888
WS_PORT = 8889
DOWNLOAD_DIR = os.path.expanduser("~/Downloads")
STATIC_DIR = Path(__file__).parent / "static"

# ── Globals ─────────────────────────────────────────────────────────────────
downloads = {}  # id -> {status, progress, speed, filename, ...}
ws_clients = set()
main_loop = None  # Set in main(), used by threads

# ── Spotify credentials (optional) ──────────────────────────────────────────
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
_spotify_token = {"token": None, "expires": 0}

_preview_cache = {}  # video_id -> (url, timestamp)
PREVIEW_CACHE_TTL = 3600  # 1 hour
_popular_cache = {"results": [], "fetched": 0}
POPULAR_CACHE_TTL = 1800  # 30 min

def get_preview_url(video_id):
    """Get direct stream URL for ad-free preview playback."""
    now = time.time()
    if video_id in _preview_cache:
        url, ts = _preview_cache[video_id]
        if now - ts < PREVIEW_CACHE_TTL:
            return url
    yt_url = f'https://www.youtube.com/watch?v={video_id}'
    cmd = ['yt-dlp', '--get-url', '-f', '18/worst[ext=mp4]/worst', '--no-warnings', '--no-playlist', yt_url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        stream_url = result.stdout.strip().split('\n')[0]
        _preview_cache[video_id] = (stream_url, now)
        return stream_url
    return None

def get_popular_youtube():
    """Get popular/trending YouTube videos with caching."""
    now = time.time()
    if _popular_cache["results"] and now - _popular_cache["fetched"] < POPULAR_CACHE_TTL:
        return _popular_cache["results"]
    results = search_youtube("trending music videos 2024", limit=12)
    if results:
        _popular_cache["results"] = results
        _popular_cache["fetched"] = now
    return results


# ── Utility ─────────────────────────────────────────────────────────────────
def has_aria2c():
    return shutil.which("aria2c") is not None


def format_size(nbytes):
    if not nbytes:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


PAYWALL_PATTERNS = [
    r"requires?\s+(premium|payment|subscription|login|sign.?in)",
    r"(drm|protected|encrypted)\s+(content|video|media)",
    r"(paid|premium)\s+(content|members?\s+only)",
    r"not\s+available.*?(country|region)",
    r"(age.?restrict|sign.?in\s+to\s+confirm)",
    r"this\s+video\s+is\s+(private|unavailable)",
]


def check_paywall_error(stderr_text):
    """Check if error indicates paywall/DRM/restriction."""
    lower = stderr_text.lower()
    for pattern in PAYWALL_PATTERNS:
        if re.search(pattern, lower):
            return True
    if "drm" in lower or "widevine" in lower or "fairplay" in lower:
        return True
    return False


def is_direct_file(url):
    exts = (
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".exe", ".dmg", ".pkg", ".deb", ".rpm",
        ".iso", ".img", ".bin", ".csv", ".json", ".xml", ".txt",
        ".ttf", ".otf", ".woff", ".woff2", ".apk",
    )
    path = url.split("?")[0].split("#")[0].lower()
    return any(path.endswith(ext) for ext in exts)


# ── Spotify Auth ─────────────────────────────────────────────────────────────
def get_spotify_token():
    """Get Spotify access token via client_credentials flow."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    now = time.time()
    if _spotify_token["token"] and _spotify_token["expires"] > now:
        return _spotify_token["token"]
    try:
        import base64
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        cmd = [
            "curl", "-s", "-X", "POST", "https://accounts.spotify.com/api/token",
            "-H", f"Authorization: Basic {creds}",
            "-d", "grant_type=client_credentials"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        _spotify_token["token"] = data.get("access_token")
        _spotify_token["expires"] = now + data.get("expires_in", 3600) - 60
        return _spotify_token["token"]
    except Exception as e:
        print(f"Spotify auth error: {e}")
        return None


def search_spotify(query, limit=10):
    """Search Spotify for tracks."""
    token = get_spotify_token()
    if not token:
        return []
    try:
        from urllib.parse import quote
        cmd = [
            "curl", "-s", "-X", "GET",
            f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={limit}",
            "-H", f"Authorization: Bearer {token}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        tracks = data.get("tracks", {}).get("items", [])
        results = []
        for t in tracks:
            images = t.get("album", {}).get("images", [])
            results.append({
                "title": t.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
                "album": t.get("album", {}).get("name", ""),
                "thumbnail": images[0]["url"] if images else "",
                "preview_url": t.get("preview_url", ""),
                "duration": (t.get("duration_ms") or 0) / 1000,
                "spotify_url": t.get("external_urls", {}).get("spotify", ""),
            })
        return results
    except Exception as e:
        print(f"Spotify search error: {e}")
        return []


# ── YouTube Search ───────────────────────────────────────────────────────────
def search_youtube(query, limit=30):
    """Search YouTube via yt-dlp."""
    try:
        cmd = ["yt-dlp", "--flat-playlist", "-j", "--no-warnings",
               f"ytsearch{limit}:{query}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        results = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line)
                results.append({
                    "title": info.get("title", ""),
                    "url": info.get("url") or info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id', '')}",
                    "thumbnail": info.get("thumbnails", [{}])[-1].get("url", "") if info.get("thumbnails") else "",
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "duration": info.get("duration"),
                    "view_count": info.get("view_count"),
                    "video_id": info.get("id", ""),
                })
            except json.JSONDecodeError:
                continue
        return results
    except Exception as e:
        print(f"YouTube search error: {e}")
        return []


# ── Broadcast to all WS clients ────────────────────────────────────────────
async def broadcast(msg):
    if ws_clients:
        data = json.dumps(msg)
        await asyncio.gather(
            *[c.send(data) for c in ws_clients],
            return_exceptions=True,
        )


def broadcast_sync(msg):
    """Thread-safe broadcast."""
    if main_loop:
        asyncio.run_coroutine_threadsafe(broadcast(msg), main_loop)


# ── Probe URL ───────────────────────────────────────────────────────────────
def probe_url(url):
    """Extract metadata with yt-dlp. Returns dict or error."""
    cmd = ["yt-dlp", "-j", "--no-warnings", "--no-playlist", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        stderr = result.stderr or ""
        if check_paywall_error(stderr):
            return {"error": "restricted", "message": "This content is protected, paywalled, or region-locked. Download is not possible."}
        return {"error": "probe_failed", "message": stderr.strip() or "Could not fetch media info."}

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "parse_failed", "message": "Failed to parse media info."}

    # Check for DRM in formats
    formats = info.get("formats", [])
    has_drm = any(f.get("has_drm") or f.get("drm_info") for f in formats)
    if has_drm:
        return {"error": "restricted", "message": "This content is DRM-protected. Download is not possible."}

    duration = info.get("duration") or 0

    # Build resolution list — pick best format per height
    resolutions = {}
    for f in formats:
        h = f.get("height")
        if h and f.get("vcodec", "none") != "none":
            existing = resolutions.get(h)
            new_tbr = f.get("tbr") or 0
            old_tbr = existing.get("tbr", 0) if existing else 0
            if not existing or new_tbr > old_tbr:
                resolutions[h] = f

    res_list = []
    labels = {144: "144p", 240: "240p", 360: "360p", 480: "480p (SD)", 720: "720p (HD)",
              1080: "1080p (Full HD)", 1440: "1440p (2K)", 2160: "2160p (4K)", 4320: "4320p (8K)"}

    # Find best audio stream bitrate + size for combo estimates
    best_audio_size = 0
    best_audio_abr = 0
    for f in formats:
        if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none":
            asize = f.get("filesize") or f.get("filesize_approx") or 0
            abr = f.get("abr") or f.get("tbr") or 0
            if asize > best_audio_size:
                best_audio_size = asize
            if abr > best_audio_abr:
                best_audio_abr = abr

    if not best_audio_size and best_audio_abr and duration:
        best_audio_size = int(best_audio_abr * 1000 / 8 * duration)

    for h in sorted(resolutions.keys()):
        f = resolutions[h]
        vsize = f.get("filesize") or f.get("filesize_approx") or 0

        if not vsize and duration:
            tbr = f.get("tbr") or 0
            if tbr:
                vsize = int(tbr * 1000 / 8 * duration)

        total = vsize + best_audio_size if vsize else 0
        res_list.append({
            "height": h,
            "label": labels.get(h, f"{h}p"),
            "size": format_size(total) if total else "",
            "size_bytes": total,
            "format_id": f.get("format_id", ""),
        })

    has_audio = any(
        f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
        for f in formats
    )

    best_total = 0
    if res_list:
        best_total = res_list[-1].get("size_bytes", 0)

    return {
        "title": info.get("title", "Unknown"),
        "uploader": info.get("uploader", ""),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail", ""),
        "resolutions": res_list,
        "has_audio": has_audio,
        "best_audio_size": best_audio_size,
        "best_total_size": best_total,
        "url": url,
        "type": "media",
    }


# ── Download worker ─────────────────────────────────────────────────────────
def download_worker(dl_id, url, mode, height, loop=None, audio_format="mp3", audio_quality="0", session_id=None):
    """Run download in a thread, push progress via WS."""

    def send(msg):
        msg["id"] = dl_id
        asyncio.run_coroutine_threadsafe(broadcast(msg), main_loop)

    # Log download start
    platform = detect_platform(url)
    quality_label = ""
    if mode == "audio":
        quality_label = f"{audio_format.upper()} {audio_quality}k"
    elif mode == "video" and height:
        quality_label = f"{height}p"
    else:
        quality_label = "Best"
    log_download(platform, url, "", mode, quality_label, "started", session_id)

    send({"status": "downloading", "progress": 0, "speed": ""})

    cmd = ["yt-dlp", "--newline", "--no-warnings", "--no-playlist"]

    if has_aria2c():
        cmd += ["--downloader", "aria2c",
                "--downloader-args", "aria2c:-x 16 -s 16 -j 8 -k 1M --file-allocation=none"]
    else:
        cmd += ["--concurrent-fragments", "4"]

    aq_map = {"320": "0", "256": "1", "192": "2", "128": "5", "best": "0"}
    aq = aq_map.get(str(audio_quality), "0")

    valid_audio_formats = ("mp3", "flac", "wav", "aac", "opus", "ogg", "m4a")
    af = audio_format if audio_format in valid_audio_formats else "mp3"

    if mode == "audio":
        cmd += ["-x", "--audio-format", af, "--audio-quality", aq]
    elif mode == "video" and height:
        cmd += ["-f", f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd += ["-o", os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "--restrict-filenames", "--print", "after_move:filepath", url]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    filepath = ""
    title = ""
    for line in proc.stdout:
        line = line.strip()

        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            pct = float(m.group(1))
            speed_m = re.search(r"at\s+([\d.]+\s*\w+/s)", line)
            eta_m = re.search(r"ETA\s+(\S+)", line)
            size_m = re.search(r"of\s+~?\s*([\d.]+\s*\w+)", line)
            dl_m = re.search(r"([\d.]+\s*\w+)\s+at\s+", line)
            send({
                "status": "downloading",
                "progress": round(pct, 1),
                "speed": speed_m.group(1) if speed_m else "",
                "total_size": size_m.group(1) if size_m else "",
                "downloaded": dl_m.group(1) if dl_m else "",
                "eta": eta_m.group(1) if eta_m else "",
            })

        if "[Merger]" in line or "[ExtractAudio]" in line:
            send({"status": "processing", "progress": 100, "speed": ""})

        if line and not line.startswith("[") and not line.startswith("Deleting") and os.path.sep in line:
            filepath = line

    proc.wait()

    if proc.returncode == 0:
        filename = os.path.basename(filepath) if filepath else "download complete"
        filesize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else None
        send({"status": "done", "progress": 100, "filename": filename, "path": filepath})
        # Log completion
        log_download(platform, url, filename, mode, quality_label, "done", session_id, filesize)
    else:
        send({"status": "error", "message": "Download failed. Check the URL."})
        log_download(platform, url, "", mode, quality_label, "error", session_id)


def download_direct_worker(dl_id, url, loop=None, session_id=None):
    """Download a direct file link."""

    def send(msg):
        msg["id"] = dl_id
        asyncio.run_coroutine_threadsafe(broadcast(msg), main_loop)

    filename = url.split("?")[0].split("#")[0].split("/")[-1] or "download"
    dest = os.path.join(DOWNLOAD_DIR, filename)

    base, ext = os.path.splitext(dest)
    counter = 1
    while os.path.exists(dest):
        dest = f"{base}_{counter}{ext}"
        counter += 1

    platform = detect_platform(url)
    log_download(platform, url, filename, "direct", "original", "started", session_id)

    send({"status": "downloading", "progress": 0, "speed": "", "filename": filename})

    if has_aria2c():
        cmd = ["aria2c", "-x", "16", "-s", "16", "-k", "1M",
               "--file-allocation=none", "-d", DOWNLOAD_DIR,
               "-o", os.path.basename(dest), url]
    else:
        cmd = ["curl", "-L", "--progress-bar", "-o", dest,
               "-H", "User-Agent: Mozilla/5.0", url]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    for line in proc.stdout:
        m = re.search(r"([\d.]+)%", line)
        if m:
            send({"status": "downloading", "progress": float(m.group(1))})

    proc.wait()

    if proc.returncode == 0 and os.path.exists(dest):
        size = os.path.getsize(dest)
        send({"status": "done", "progress": 100, "filename": os.path.basename(dest),
              "path": dest, "size": format_size(size)})
        log_download(platform, url, filename, "direct", "original", "done", session_id, size)
    else:
        send({"status": "error", "message": "Download failed."})
        log_download(platform, url, filename, "direct", "original", "error", session_id)


# ── WebSocket handler ───────────────────────────────────────────────────────
async def ws_handler(websocket):
    ws_clients.add(websocket)
    loop = asyncio.get_running_loop()
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = msg.get("action")
            session_id = msg.get("session_id", "")

            if action == "probe":
                url = msg.get("url", "").strip()
                if not url:
                    await websocket.send(json.dumps({"error": "no_url", "message": "No URL provided."}))
                    continue
                if not re.match(r"https?://", url):
                    url = "https://" + url

                def do_probe():
                    if is_direct_file(url):
                        result = {"type": "direct", "url": url,
                                  "filename": url.split("?")[0].split("/")[-1]}
                    else:
                        result = probe_url(url)
                    asyncio.run_coroutine_threadsafe(
                        websocket.send(json.dumps({"action": "probe_result", **result})),
                        main_loop,
                    )

                threading.Thread(target=do_probe, daemon=True).start()

            elif action == "download":
                url = msg.get("url", "")
                mode = msg.get("mode", "best")
                height = msg.get("height")
                dl_type = msg.get("type", "media")
                audio_format = msg.get("audio_format", "mp3")
                audio_quality = msg.get("audio_quality", "best")
                title = msg.get("title", "")
                thumbnail = msg.get("thumbnail", "")
                dl_id = str(uuid.uuid4())[:8]

                if dl_type == "direct":
                    threading.Thread(
                        target=download_direct_worker,
                        args=(dl_id, url, loop, session_id), daemon=True,
                    ).start()
                else:
                    threading.Thread(
                        target=download_worker,
                        args=(dl_id, url, mode, height, loop, audio_format, audio_quality, session_id),
                        daemon=True,
                    ).start()

                label = title or url.split("/")[-1]
                quality_label = ""
                if mode == "audio":
                    quality_label = f"{audio_format.upper()}"
                elif mode == "video" and height:
                    quality_label = f"{height}p"
                else:
                    quality_label = "Best"

                await websocket.send(json.dumps({
                    "action": "download_started", "id": dl_id,
                    "title": label, "thumbnail": thumbnail,
                    "quality": quality_label,
                }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(websocket)


# ── HTTP server with routing ─────────────────────────────────────────────────
class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass  # Silence logs

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── Route: Home page ──
        if path == "/" or path == "/index.html":
            self._serve_file("index.html")
            return

        # ── Route: Analytics page ──
        if path == "/analytics" or path == "/analytics.html":
            self._serve_file("analytics.html")
            return

        # ── API: YouTube search ──
        if path == "/api/search/youtube":
            query = params.get("q", [""])[0]
            session_id = params.get("session_id", [""])[0]
            if not query:
                self._json_response({"error": "No query", "results": []})
                return
            # Log session and search
            if session_id:
                log_session(session_id, self.headers.get("User-Agent", ""), "web")
            log_search("youtube", query, session_id)
            results = search_youtube(query)
            self._json_response({"results": results})
            return

        # ── API: Spotify search ──
        if path == "/api/search/spotify":
            query = params.get("q", [""])[0]
            session_id = params.get("session_id", [""])[0]
            if not query:
                self._json_response({"error": "No query", "results": []})
                return
            if session_id:
                log_session(session_id, self.headers.get("User-Agent", ""), "web")
            log_search("spotify", query, session_id)
            results = search_spotify(query)
            self._json_response({"results": results})
            return

        # ── API: Analytics data ──
        if path == "/api/analytics":
            data = get_analytics()
            self._json_response(data)
            return

        # ── API: YouTube preview stream URL ──
        if path == "/api/preview/youtube":
            video_id = params.get("v", [""])[0]
            if not video_id:
                self._json_response({"error": "No video ID"})
                return
            stream_url = get_preview_url(video_id)
            if stream_url:
                self._json_response({"url": stream_url})
            else:
                self._json_response({"error": "Preview unavailable"}, 404)
            return

        # ── API: Popular YouTube videos ──
        if path == "/api/popular/youtube":
            results = get_popular_youtube()
            self._json_response({"results": results})
            return

        # ── Static files fallback ──
        super().do_GET()

    def _serve_file(self, filename):
        filepath = STATIC_DIR / filename
        if not filepath.exists():
            self.send_error(404, f"File not found: {filename}")
            return
        content = filepath.read_bytes()
        self.send_response(200)
        if filename.endswith(".html"):
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif filename.endswith(".css"):
            self.send_header("Content-Type", "text/css; charset=utf-8")
        elif filename.endswith(".js"):
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def run_http():
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), AppHandler)
    server.serve_forever()


# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()

    # Initialize database
    init_db()

    # Start HTTP server in thread
    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()

    print(f"""
\033[1m\033[96m  ⚡ fastdl server\033[0m
\033[2m  ─────────────────────────────────────\033[0m
  Web UI:    \033[4mhttp://localhost:{HTTP_PORT}\033[0m
  WebSocket: ws://localhost:{WS_PORT}
  Downloads: {DOWNLOAD_DIR}
  aria2c:    {"✓ enabled (turbo mode)" if has_aria2c() else "✗ not found (install for 10x speed)"}
  Spotify:   {"✓ configured" if SPOTIFY_CLIENT_ID else "✗ set SPOTIFY_CLIENT_ID & SPOTIFY_CLIENT_SECRET"}
  Database:  fastdl.db (SQLite)
\033[2m  ─────────────────────────────────────\033[0m
  \033[2mPress Ctrl+C to stop\033[0m
""")

    async with ws_serve(ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\033[93m  Stopped.\033[0m")

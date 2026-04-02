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
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    print("Installing websockets...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets
    from websockets.asyncio.server import serve as ws_serve

# ── Config ──────────────────────────────────────────────────────────────────
HTTP_PORT = 8888
WS_PORT = 8889
DOWNLOAD_DIR = os.path.expanduser("~/Downloads")
STATIC_DIR = Path(__file__).parent / "static"

# ── Globals ─────────────────────────────────────────────────────────────────
downloads = {}  # id -> {status, progress, speed, filename, ...}
ws_clients = set()
main_loop = None  # Set in main(), used by threads


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
    loop = asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None
    if loop:
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)


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
            # Prefer higher tbr (total bitrate) for better quality estimate
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

    # Estimate audio size from bitrate if no filesize available
    if not best_audio_size and best_audio_abr and duration:
        best_audio_size = int(best_audio_abr * 1000 / 8 * duration)

    for h in sorted(resolutions.keys()):
        f = resolutions[h]
        vsize = f.get("filesize") or f.get("filesize_approx") or 0

        # If no filesize, estimate from tbr (total bitrate) × duration
        if not vsize and duration:
            tbr = f.get("tbr") or 0
            if tbr:
                vsize = int(tbr * 1000 / 8 * duration)

        # Total = video stream + audio stream
        total = vsize + best_audio_size if vsize else 0
        res_list.append({
            "height": h,
            "label": labels.get(h, f"{h}p"),
            "size": format_size(total) if total else "",
            "size_bytes": total,
            "format_id": f.get("format_id", ""),
        })

    # Check if audio-only available
    has_audio = any(
        f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
        for f in formats
    )

    # Get best video+audio total for "best quality" estimate
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
def download_worker(dl_id, url, mode, height, loop=None, audio_format="mp3", audio_quality="0"):
    """Run download in a thread, push progress via WS."""

    def send(msg):
        msg["id"] = dl_id
        asyncio.run_coroutine_threadsafe(broadcast(msg), main_loop)

    send({"status": "downloading", "progress": 0, "speed": ""})

    cmd = ["yt-dlp", "--newline", "--no-warnings", "--no-playlist"]

    if has_aria2c():
        cmd += ["--downloader", "aria2c",
                "--downloader-args", "aria2c:-x 16 -s 16 -j 8 -k 1M --file-allocation=none"]
    else:
        cmd += ["--concurrent-fragments", "4"]

    # Audio quality mapping: "0"=best, "5"=worst for VBR; or specific bitrate
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
    for line in proc.stdout:
        line = line.strip()

        # Parse progress
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            pct = float(m.group(1))
            speed_m = re.search(r"at\s+([\d.]+\s*\w+/s)", line)
            eta_m = re.search(r"ETA\s+(\S+)", line)
            # Parse "of ~XXX" or "of XXX" for total size
            size_m = re.search(r"of\s+~?\s*([\d.]+\s*\w+)", line)
            # Parse downloaded so far "XXX at"
            dl_m = re.search(r"([\d.]+\s*\w+)\s+at\s+", line)
            send({
                "status": "downloading",
                "progress": round(pct, 1),
                "speed": speed_m.group(1) if speed_m else "",
                "total_size": size_m.group(1) if size_m else "",
                "downloaded": dl_m.group(1) if dl_m else "",
                "eta": eta_m.group(1) if eta_m else "",
            })

        # Merger / postprocessor
        if "[Merger]" in line or "[ExtractAudio]" in line:
            send({"status": "processing", "progress": 100, "speed": ""})

        # Final filepath
        if line and not line.startswith("[") and not line.startswith("Deleting") and os.path.sep in line:
            filepath = line

    proc.wait()

    if proc.returncode == 0:
        filename = os.path.basename(filepath) if filepath else "download complete"
        send({"status": "done", "progress": 100, "filename": filename, "path": filepath})
    else:
        send({"status": "error", "message": "Download failed. Check the URL."})


def download_direct_worker(dl_id, url, loop=None):
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
        # curl progress
        m = re.search(r"([\d.]+)%", line)
        if m:
            send({"status": "downloading", "progress": float(m.group(1))})

    proc.wait()

    if proc.returncode == 0 and os.path.exists(dest):
        size = os.path.getsize(dest)
        send({"status": "done", "progress": 100, "filename": os.path.basename(dest),
              "path": dest, "size": format_size(size)})
    else:
        send({"status": "error", "message": "Download failed."})


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

            if action == "probe":
                url = msg.get("url", "").strip()
                if not url:
                    await websocket.send(json.dumps({"error": "no_url", "message": "No URL provided."}))
                    continue
                if not re.match(r"https?://", url):
                    url = "https://" + url

                # Run probe in thread to not block
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
                        args=(dl_id, url, loop), daemon=True,
                    ).start()
                else:
                    threading.Thread(
                        target=download_worker,
                        args=(dl_id, url, mode, height, loop, audio_format, audio_quality),
                        daemon=True,
                    ).start()

                # Send back title/thumb so the queue item can display it
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


# ── HTTP server (serves static files) ──────────────────────────────────────
class StaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):
        pass  # Silence HTTP logs


def run_http():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), StaticHandler)
    server.serve_forever()


# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()

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

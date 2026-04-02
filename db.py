"""SQLite database for fastdl analytics and tracking."""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fastdl.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_agent TEXT,
            source TEXT DEFAULT 'web'
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'other',
            url TEXT NOT NULL,
            title TEXT,
            mode TEXT,
            quality TEXT,
            status TEXT NOT NULL DEFAULT 'started',
            session_id TEXT,
            filesize INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            query TEXT NOT NULL,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def log_session(session_id, user_agent="", source="web"):
    if not session_id:
        return
    conn = get_conn()
    conn.execute("""
        INSERT INTO sessions (id, user_agent, source)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP, user_agent = excluded.user_agent
    """, (session_id, user_agent, source))
    conn.commit()
    conn.close()

def log_download(platform, url, title, mode, quality, status, session_id=None, filesize=None):
    conn = get_conn()
    conn.execute("""
        INSERT INTO downloads (platform, url, title, mode, quality, status, session_id, filesize)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (platform, url, title, mode, quality, status, session_id, filesize))
    conn.commit()
    conn.close()

def log_search(platform, query, session_id=None):
    conn = get_conn()
    conn.execute("INSERT INTO search_log (platform, query, session_id) VALUES (?, ?, ?)",
                 (platform, query, session_id))
    conn.commit()
    conn.close()

def detect_platform(url):
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    if "spotify.com" in url_lower:
        return "spotify"
    if "soundcloud.com" in url_lower:
        return "soundcloud"
    if "tiktok.com" in url_lower:
        return "tiktok"
    if "instagram.com" in url_lower:
        return "instagram"
    if "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    if "vimeo.com" in url_lower:
        return "vimeo"
    return "other"

def get_analytics():
    conn = get_conn()

    # Monthly active users (last 12 months)
    monthly_users = [dict(r) for r in conn.execute("""
        SELECT strftime('%Y-%m', last_seen) as month, COUNT(DISTINCT id) as count
        FROM sessions
        GROUP BY month ORDER BY month DESC LIMIT 12
    """).fetchall()]

    # Downloads by platform
    by_platform = {}
    for r in conn.execute("SELECT platform, COUNT(*) as count FROM downloads WHERE status='done' GROUP BY platform"):
        by_platform[r["platform"]] = r["count"]

    # Popular qualities
    by_quality = [dict(r) for r in conn.execute("""
        SELECT quality, COUNT(*) as count FROM downloads WHERE status='done' AND quality IS NOT NULL
        GROUP BY quality ORDER BY count DESC LIMIT 10
    """).fetchall()]

    # Daily downloads (last 30 days)
    daily = [dict(r) for r in conn.execute("""
        SELECT strftime('%Y-%m-%d', created_at) as date, COUNT(*) as count
        FROM downloads WHERE created_at >= date('now', '-30 days')
        GROUP BY date ORDER BY date
    """).fetchall()]

    # By source (web vs telegram)
    by_source = {}
    for r in conn.execute("SELECT source, COUNT(DISTINCT id) as count FROM sessions GROUP BY source"):
        by_source[r["source"]] = r["count"]

    # Totals
    total_downloads = conn.execute("SELECT COUNT(*) FROM downloads WHERE status='done'").fetchone()[0]
    total_users = conn.execute("SELECT COUNT(DISTINCT id) FROM sessions").fetchone()[0]

    # Top searched
    top_searches = [dict(r) for r in conn.execute("""
        SELECT query, platform, COUNT(*) as count FROM search_log
        GROUP BY query, platform ORDER BY count DESC LIMIT 10
    """).fetchall()]

    conn.close()

    return {
        "monthly_users": list(reversed(monthly_users)),
        "by_platform": by_platform,
        "by_quality": by_quality,
        "daily_downloads": daily,
        "by_source": by_source,
        "total_downloads": total_downloads,
        "total_users": total_users,
        "top_searches": top_searches,
    }

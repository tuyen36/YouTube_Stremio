import json
import logging
import os
import re
import time
from pathlib import Path
from threading import Lock

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# =========================================================
# 1. CẤU HÌNH
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

if not CONFIG_FILE.exists():
    raise FileNotFoundError(
        f"Khong tim thay {CONFIG_FILE}. "
        "Hay dat config.json cung thu muc voi main.py."
    )

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

CACHE_SECONDS = max(30, int(CONFIG.get("cache_seconds", 600)))
HOST = os.environ.get("HOST", CONFIG.get("host", "0.0.0.0"))
PORT = int(os.environ.get("PORT", CONFIG.get("port", 7000)))


def load_channels(config):
    """Đọc danh sách kênh từ config.json, có hỗ trợ config cũ."""
    raw_channels = config.get("channels")

    # Tương thích ngược với config cũ chỉ có 1 kênh
    if not raw_channels:
        raw_channels = [
            {
                "id": "youtube_videos",
                "name": config.get("catalog_name", "YouTube Movies"),
                "url": config.get(
                    "channel_url",
                    "https://www.youtube.com/@GoogleDevelopers/videos"
                ),
                "limit": config.get("max_videos", 100)
            }
        ]

    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("config.json phai co 'channels' la danh sach khong rong")

    channels = []
    seen_ids = set()

    for index, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            raise ValueError(f"Channel #{index} khong phai object")

        channel_id = str(item.get("id", "")).strip()
        if not channel_id:
            raise ValueError(f"Channel #{index} thieu 'id'")

        if channel_id in seen_ids:
            raise ValueError(f"Trung id kenh: {channel_id}")

        if not re.fullmatch(r"[A-Za-z0-9_-]+", channel_id):
            raise ValueError(
                f"id kenh khong hop le: {channel_id}. "
                "Chi dung chu, so, dau _ va dau -"
            )

        url = str(item.get("url", "")).strip()
        if not url:
            raise ValueError(f"Channel {channel_id} thieu 'url'")

        name = str(item.get("name", channel_id)).strip() or channel_id

        try:
            limit = int(item.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100

        limit = max(1, min(limit, 500))

        channels.append({
            "id": channel_id,
            "name": name,
            "url": url,
            "limit": limit
        })
        seen_ids.add(channel_id)

    return channels


CHANNELS = load_channels(CONFIG)
CHANNELS_BY_ID = {channel["id"]: channel for channel in CHANNELS}

# =========================================================
# 2. KHỞI TẠO MÁY CHỦ
# =========================================================

app = FastAPI(
    title="YouTube Stremio Add-on",
    version="2.0.0",
    description="Browse public YouTube videos from multiple channels in Stremio"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"]
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("youtube_stremio")

# =========================================================
# 3. MANIFEST
# =========================================================

MANIFEST = {
    "id": "org.tuyen.youtube.stremio",
    "version": "2.0.0",
    "name": "YouTube Channels",
    "description": "Browse public YouTube videos from multiple channels",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie"],
    "idPrefixes": ["yt_"],
    "catalogs": [
        {
            "type": "movie",
            "id": channel["id"],
            "name": channel["name"]
        }
        for channel in CHANNELS
    ],
    "behaviorHints": {
        "configurable": False,
        "configurationRequired": False
    }
}

# =========================================================
# 4. CACHE DANH SÁCH VIDEO THEO TỪNG KÊNH
# =========================================================

video_cache = {}
cache_time = {}
cache_lock = Lock()


def extract_video_id(item):
    """Lấy mã video YouTube từ dữ liệu yt-dlp."""
    video_id = item.get("id")

    if not video_id:
        url = item.get("url", "")
        match = re.search(
            r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
            url
        )
        if match:
            video_id = match.group(1)

    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None

    return video_id


def get_video_list(channel_id, force=False):
    """Lấy danh sách video của một kênh, có cache riêng."""
    channel = CHANNELS_BY_ID.get(channel_id)

    if channel is None:
        raise KeyError(f"Khong tim thay kenh: {channel_id}")

    now = time.time()

    with cache_lock:
        cached = video_cache.get(channel_id)
        cached_at = cache_time.get(channel_id, 0)

        if (
            not force
            and cached is not None
            and now - cached_at < CACHE_SECONDS
        ):
            return cached

    options = {
        "extract_flat": True,
        "playlistend": channel["limit"],
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True
    }

    try:
        logger.info(
            "Dang lay danh sach kenh %s (%s): %s",
            channel["id"],
            channel["name"],
            channel["url"]
        )

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(channel["url"], download=False)

        entries = info.get("entries") or []
        result = []

        for item in entries:
            if not item:
                continue

            video_id = extract_video_id(item)
            if not video_id:
                continue

            title = item.get("title") or "YouTube video"
            thumbnail = item.get("thumbnail") or (
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            )

            result.append({
                "id": video_id,
                "name": title,
                "poster": thumbnail,
                "description": item.get("description") or "",
                "duration": item.get("duration"),
                "url": "https://www.youtube.com/watch?v=" + video_id
            })

        with cache_lock:
            video_cache[channel_id] = result
            cache_time[channel_id] = time.time()

        logger.info(
            "Kenh %s: da nap %d video",
            channel_id,
            len(result)
        )
        return result

    except Exception:
        logger.exception(
            "Khong the lay danh sach YouTube cho kenh %s",
            channel_id
        )

        with cache_lock:
            cached = video_cache.get(channel_id)

        if cached is not None:
            logger.warning("Su dung cache cu cho kenh %s", channel_id)
            return cached

        raise


def find_video(video_id):
    """Tìm video trong cache của tất cả kênh."""
    with cache_lock:
        for videos in video_cache.values():
            for video in videos:
                if video["id"] == video_id:
                    return video
    return None


# =========================================================
# 5. MANIFEST ENDPOINT
# =========================================================

@app.get("/manifest.json")
def manifest():
    return MANIFEST


# =========================================================
# 6. CATALOG ENDPOINT THEO TỪNG KÊNH
# =========================================================

@app.get("/catalog/movie/{catalog_id}.json")
def catalog(catalog_id: str):
    if catalog_id not in CHANNELS_BY_ID:
        raise HTTPException(
            status_code=404,
            detail="Khong tim thay danh muc"
        )

    try:
        videos = get_video_list(catalog_id)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Khong the tai danh sach YouTube"
        )

    metas = []

    for video in videos:
        item = {
            "id": "yt_" + video["id"],
            "type": "movie",
            "name": video["name"],
            "poster": video["poster"],
            "description": video["description"][:1000]
        }

        duration = video.get("duration")
        if duration:
            seconds = int(duration)
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)

            item["runtime"] = (
                f"{hours}:{minutes:02d}:{secs:02d}"
                if hours else f"{minutes}:{secs:02d}"
            )

        metas.append(item)

    return {"metas": metas}


# =========================================================
# 7. META ENDPOINT
# =========================================================

@app.get("/meta/movie/{item_id}.json")
def meta(item_id: str):
    if not item_id.startswith("yt_"):
        raise HTTPException(
            status_code=404,
            detail="Khong tim thay video"
        )

    video_id = item_id[3:]

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(
            status_code=404,
            detail="Ma video khong hop le"
        )

    try:
        video = find_video(video_id)
    except Exception:
        video = None

    if video is None:
        video = {
            "id": video_id,
            "name": "YouTube video " + video_id,
            "poster": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "description": ""
        }

    return {
        "meta": {
            "id": "yt_" + video_id,
            "type": "movie",
            "name": video["name"],
            "poster": video["poster"],
            "description": video.get("description", ""),
            "videos": [
                {
                    "id": "yt_" + video_id,
                    "title": video["name"],
                    "season": 1,
                    "episode": 1
                }
            ]
        }
    }


# =========================================================
# 8. STREAM ENDPOINT
# =========================================================

@app.get("/stream/movie/{item_id}.json")
def stream(item_id: str):
    if not item_id.startswith("yt_"):
        raise HTTPException(
            status_code=404,
            detail="Khong tim thay video"
        )

    video_id = item_id[3:]

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(
            status_code=404,
            detail="Ma video khong hop le"
        )

    logger.info("Tra ve YouTube ytId: %s", video_id)

    return {
        "streams": [
            {
                "ytId": video_id,
                "name": "YouTube",
                "title": "Phat bang YouTube"
            }
        ]
    }


# =========================================================
# 9. TRANG KIỂM TRA
# =========================================================

@app.get("/")
def home():
    return {
        "name": "YouTube Stremio Add-on",
        "version": MANIFEST["version"],
        "status": "running",
        "manifest": "/manifest.json",
        "channels": [
            {
                "id": channel["id"],
                "name": channel["name"],
                "catalog": f"/catalog/movie/{channel['id']}.json"
            }
            for channel in CHANNELS
        ]
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": MANIFEST["version"]
    }


# =========================================================
# 10. KHỞI ĐỘNG MÁY CHỦ
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info"
    )
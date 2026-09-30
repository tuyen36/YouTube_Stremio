import json
import logging
import re
import time
from pathlib import Path
from threading import Lock

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# =========================================================
# 1. CAU HINH
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

CHANNEL_URL = CONFIG.get(
    "channel_url",
    "https://www.youtube.com/@GoogleDevelopers/videos"
).strip()

MAX_VIDEOS = max(1, min(int(CONFIG.get("max_videos", 100)), 500))
CATALOG_NAME = CONFIG.get("catalog_name", "YouTube Movies")
CACHE_SECONDS = max(30, int(CONFIG.get("cache_seconds", 600)))
HOST = CONFIG.get("host", "127.0.0.1")
PORT = int(CONFIG.get("port", 7000))

# =========================================================
# 2. KHOI TAO MAY CHU
# =========================================================

app = FastAPI(
    title="YouTube Stremio Add-on",
    version="1.2.0",
    description="Browse public YouTube videos in Stremio"
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
    "version": "1.2.0",
    "name": "YouTube Channel",
    "description": "Browse public YouTube videos",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie"],
    "idPrefixes": ["yt_"],
    "catalogs": [
        {
            "type": "movie",
            "id": "youtube_videos",
            "name": CATALOG_NAME
        }
    ],
    "behaviorHints": {
        "configurable": False,
        "configurationRequired": False
    }
}

# =========================================================
# 4. CACHE DANH SACH VIDEO
# =========================================================

video_cache = []
cache_time = 0
cache_lock = Lock()


def extract_video_id(item):
    """Lay ma video YouTube tu du lieu yt-dlp."""
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


def get_video_list(force=False):
    """Lay danh sach video, su dung cache de giam truy van."""
    global video_cache, cache_time

    with cache_lock:
        if (
            not force
            and video_cache
            and time.time() - cache_time < CACHE_SECONDS
        ):
            return video_cache

        options = {
            "extract_flat": True,
            "playlistend": MAX_VIDEOS,
            "ignoreerrors": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True
        }

        try:
            logger.info("Dang lay danh sach: %s", CHANNEL_URL)

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(CHANNEL_URL, download=False)

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
                    "https://i.ytimg.com/vi/"
                    + video_id
                    + "/hqdefault.jpg"
                )

                result.append({
                    "id": video_id,
                    "name": title,
                    "poster": thumbnail,
                    "description": item.get("description") or "",
                    "duration": item.get("duration"),
                    "url": "https://www.youtube.com/watch?v=" + video_id
                })

            video_cache = result
            cache_time = time.time()

            logger.info("Da nap thanh cong %d video", len(result))
            return video_cache

        except Exception:
            logger.exception("Khong the lay danh sach YouTube")
            if video_cache:
                logger.warning("Su dung danh sach cache cu")
                return video_cache
            raise


def find_video(video_id):
    """Tim video theo ma YouTube."""
    for video in get_video_list():
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
# 6. CATALOG ENDPOINT
# =========================================================

@app.get("/catalog/movie/youtube_videos.json")
def catalog():
    try:
        videos = get_video_list()
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
        raise HTTPException(status_code=404, detail="Khong tim thay video")

    video_id = item_id[3:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=404, detail="Ma video khong hop le")

    try:
        video = find_video(video_id)
    except Exception:
        video = None

    if video is None:
        video = {
            "id": video_id,
            "name": "YouTube video " + video_id,
            "poster": (
                "https://i.ytimg.com/vi/"
                + video_id
                + "/hqdefault.jpg"
            ),
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
# Su dung ytId de Stremio xu ly video YouTube.
# Khong goi yt-dlp va khong trich xuat URL luong tai day.

@app.get("/stream/movie/{item_id}.json")
def stream(item_id: str):
    if not item_id.startswith("yt_"):
        raise HTTPException(status_code=404, detail="Khong tim thay video")

    video_id = item_id[3:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=404, detail="Ma video khong hop le")

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
# 9. TRANG KIEM TRA
# =========================================================

@app.get("/")
def home():
    return {
        "name": "YouTube Stremio Add-on",
        "version": "1.2.0",
        "status": "running",
        "manifest": "/manifest.json",
        "catalog": "/catalog/movie/youtube_videos.json"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "1.2.0"
    }


# =========================================================
# 10. KHOI DONG MAY CHU
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info"
    )

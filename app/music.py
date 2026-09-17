"""Search YouTube Music for songs, and turn a video id into a playable audio URL.

Both libraries here are blocking and can take seconds on a Pi 3B, so every public
function hands off to a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import yt_dlp
from ytmusicapi import YTMusic

log = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Google's stream URLs carry an expiry, typically ~6 hours. Re-resolve well before that.
STREAM_TTL_SECONDS = 60 * 60

_YDL_OPTS = {
    # Let yt-dlp pick its own current player_client list. Hardcoding one
    # (android_music, ios_music, etc.) breaks the moment YouTube changes
    # what that client is served, which happens every few months.
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "cachedir": False,
}

_client: YTMusic | None = None


def _ytmusic() -> YTMusic:
    global _client
    if _client is None:
        _client = YTMusic()  # unauthenticated: search works, no personal library
    return _client


@dataclass(slots=True)
class SearchResult:
    video_id: str
    title: str
    artist: str
    album: str | None
    duration: int
    thumbnail: str | None

    def as_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
        }


def _pick_thumbnail(thumbs: list[dict] | None) -> str | None:
    if not thumbs:
        return None
    # Smallest one that is at least 120px wide; phones don't need the 544px art.
    usable = sorted(thumbs, key=lambda t: t.get("width", 0))
    for t in usable:
        if t.get("width", 0) >= 120:
            return t.get("url")
    return usable[-1].get("url")


def _search_sync(query: str, limit: int) -> list[SearchResult]:
    raw = _ytmusic().search(query, filter="songs", limit=limit)
    results: list[SearchResult] = []
    for item in raw:
        vid = item.get("videoId")
        if not vid or not VIDEO_ID_RE.match(vid):
            continue
        artists = [a.get("name") for a in (item.get("artists") or []) if a.get("name")]
        album = (item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else None
        results.append(
            SearchResult(
                video_id=vid,
                title=item.get("title") or "Unknown title",
                artist=", ".join(artists) or "Unknown artist",
                album=album,
                duration=int(item.get("duration_seconds") or 0),
                thumbnail=_pick_thumbnail(item.get("thumbnails")),
            )
        )
        if len(results) >= limit:
            break
    return results


def _extract(video_id: str, opts: dict) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _best_audio_url(info: dict) -> str | None:
    if info.get("url"):
        return info["url"]
    for fmt in info.get("requested_formats") or []:
        if fmt.get("acodec") != "none" and fmt.get("url"):
            return fmt["url"]
    return None


def _resolve_sync(video_id: str) -> str:
    try:
        info = _extract(video_id, _YDL_OPTS)
        stream = _best_audio_url(info)
    except yt_dlp.utils.DownloadError:
        stream = None

    if not stream:
        # yt-dlp's current default client list came up empty for this video.
        # web is usually the most consistently available fallback client.
        log.warning("retrying %s with the web client", video_id)
        fallback = {**_YDL_OPTS, "extractor_args": {"youtube": {"player_client": ["web"]}}}
        info = _extract(video_id, fallback)
        stream = _best_audio_url(info)

    if not stream:
        raise RuntimeError(f"no audio stream found for {video_id}")
    return stream


async def search(query: str, limit: int = 12) -> list[SearchResult]:
    query = query.strip()
    if not query:
        return []
    return await asyncio.to_thread(_search_sync, query, limit)


async def resolve_stream(video_id: str) -> tuple[str, float]:
    """Return (stream_url, expires_at_monotonic)."""
    if not VIDEO_ID_RE.match(video_id):
        raise ValueError("invalid video id")
    started = time.monotonic()
    stream = await asyncio.to_thread(_resolve_sync, video_id)
    log.info("resolved %s in %.1fs", video_id, time.monotonic() - started)
    return stream, time.monotonic() + STREAM_TTL_SECONDS

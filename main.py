from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, music
from .mpv import Mpv, MpvError
from .queue import PlayQueue, QueueError, Track

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jukebox")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class State:
    def __init__(self):
        extra = (f"--audio-device={config.AUDIO_DEVICE}",) if config.AUDIO_DEVICE else ()
        self.mpv = Mpv(config.MPV_SOCKET, volume=config.START_VOLUME, extra_args=extra)
        self.queue = PlayQueue(config.MAX_PER_PERSON, config.MAX_QUEUE)
        self.current: Track | None = None
        self.started_at: float = 0.0
        self.skip_votes: set[str] = set()
        self.last_error: str | None = None
        self.player_task: asyncio.Task | None = None
        self.prefetch_task: asyncio.Task | None = None


state = State()


def clean_name(raw: str | None) -> str:
    name = (raw or "").strip()[:24]
    return name or "Anonymous"


async def ensure_stream(track: Track) -> str:
    if track.stream_url and track.stream_expires > time.monotonic():
        return track.stream_url
    track.stream_url, track.stream_expires = await music.resolve_stream(track.video_id)
    return track.stream_url


async def prefetch_next() -> None:
    """Resolve the next track's URL while the current one plays, so gaps stay short."""
    nxt = state.queue.peek_next()
    if not nxt or (nxt.stream_url and nxt.stream_expires > time.monotonic()):
        return
    try:
        await ensure_stream(nxt)
    except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
        log.warning("prefetch failed for %s: %s", nxt.video_id, exc)


async def player_loop() -> None:
    while True:
        track = await state.queue.next_track()
        state.current = track
        state.started_at = time.time()
        state.skip_votes.clear()
        try:
            url = await ensure_stream(track)
            await state.mpv.play(url)

            if state.prefetch_task:
                state.prefetch_task.cancel()
            state.prefetch_task = asyncio.create_task(prefetch_next())

            reason = await state.mpv.wait_for_end()
            if reason == "error":
                state.last_error = f"Playback failed for {track.title}"
                log.error("mpv reported an error playing %s", track.video_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad track must not kill the loop
            state.last_error = f"Could not play {track.title}"
            log.exception("failed to play %s", track.video_id)
            await asyncio.sleep(1)
        finally:
            state.current = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await state.mpv.start()
    state.player_task = asyncio.create_task(player_loop(), name="player")
    try:
        yield
    finally:
        if state.player_task:
            state.player_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.player_task
        await state.mpv.stop_process()


app = FastAPI(title="Pi Jukebox", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/search")
async def api_search(q: str = Query(min_length=1, max_length=120)):
    try:
        results = await music.search(q)
    except Exception as exc:  # noqa: BLE001
        log.exception("search failed")
        raise HTTPException(502, "Search is unavailable right now.") from exc
    return {"results": [r.as_dict() for r in results]}


@app.get("/api/state")
async def api_state():
    now = None
    if state.current:
        now = state.current.as_dict()
        now["position"] = round(state.mpv.position, 1)
        now["duration"] = round(state.mpv.duration or state.current.duration, 1)
        now["paused"] = await state.mpv.is_paused()
        now["skip_votes"] = len(state.skip_votes)
    error, state.last_error = state.last_error, None
    return {
        "now": now,
        "queue": state.queue.snapshot(),
        "volume": state.mpv.volume,
        "skip_votes_needed": config.SKIP_VOTES,
        "error": error,
    }


@app.post("/api/add")
async def api_add(payload: dict = Body(...)):
    name = clean_name(payload.get("submitter"))
    video_id = (payload.get("video_id") or "").strip()
    if not music.VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "That song id doesn't look right.")

    duration = int(payload.get("duration") or 0)
    if duration > config.MAX_DURATION:
        return JSONResponse(
            {"error": f"Tracks over {config.MAX_DURATION // 60} minutes aren't allowed."},
            status_code=400,
        )

    track = Track(
        video_id=video_id,
        title=str(payload.get("title") or "Unknown title")[:120],
        artist=str(payload.get("artist") or "Unknown artist")[:120],
        duration=duration,
        thumbnail=(payload.get("thumbnail") or None),
        submitter=name,
    )
    try:
        await state.queue.add(track)
    except QueueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if state.current is None:
        asyncio.create_task(prefetch_next())
    return {"added": track.as_dict()}


@app.post("/api/remove")
async def api_remove(payload: dict = Body(...)):
    entry_id = str(payload.get("id") or "")
    name = clean_name(payload.get("submitter"))
    force = bool(config.ADMIN_TOKEN) and payload.get("token") == config.ADMIN_TOKEN
    try:
        removed = await state.queue.remove(entry_id, name, force=force)
    except QueueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    if not removed:
        raise HTTPException(404, "That song is no longer in the queue.")
    return {"removed": entry_id}


@app.post("/api/skip")
async def api_skip(payload: dict = Body(...)):
    if not state.current:
        raise HTTPException(409, "Nothing is playing.")
    name = clean_name(payload.get("submitter"))
    force = bool(config.ADMIN_TOKEN) and payload.get("token") == config.ADMIN_TOKEN

    state.skip_votes.add(name)
    enough = force or name == state.current.submitter or len(state.skip_votes) >= config.SKIP_VOTES
    if enough:
        await state.mpv.skip()
        return {"skipped": True}
    return {
        "skipped": False,
        "votes": len(state.skip_votes),
        "needed": config.SKIP_VOTES,
    }


@app.post("/api/pause")
async def api_pause(payload: dict = Body(default={})):
    paused = await state.mpv.is_paused()
    await state.mpv.set_pause(not paused)
    return {"paused": not paused}


@app.post("/api/volume")
async def api_volume(payload: dict = Body(...)):
    if config.ADMIN_TOKEN and payload.get("token") != config.ADMIN_TOKEN:
        raise HTTPException(403, "Volume is host-only.")
    try:
        await state.mpv.set_volume(int(payload.get("value", 85)))
    except (MpvError, ValueError) as exc:
        raise HTTPException(500, "Could not change the volume.") from exc
    return {"volume": state.mpv.volume}

"""Async controller for a long-running mpv process, driven over its JSON IPC socket."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os

log = logging.getLogger(__name__)


class MpvError(RuntimeError):
    pass


class Mpv:
    def __init__(self, socket_path: str, volume: int = 85, extra_args: tuple[str, ...] = ()):
        self.socket_path = socket_path
        self.volume = volume
        self.extra_args = extra_args

        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._end = asyncio.Event()
        self._end_reason: str | None = None
        self._reader_task: asyncio.Task | None = None

        # Updated from observed properties.
        self.position: float = 0.0
        self.duration: float = 0.0

    async def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        args = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--audio-display=no",
            "--gapless-audio=yes",
            # Buffer generously: 2.4GHz wifi on a Pi 3B drops out.
            "--cache=yes",
            "--demuxer-max-bytes=32MiB",
            "--demuxer-readahead-secs=30",
            f"--volume={self.volume}",
            f"--input-ipc-server={self.socket_path}",
            *self.extra_args,
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        for _ in range(100):
            if os.path.exists(self.socket_path):
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
                    break
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    pass
            await asyncio.sleep(0.1)
        else:
            raise MpvError("mpv did not open its IPC socket within 10s")

        self._reader_task = asyncio.create_task(self._read_loop(), name="mpv-reader")
        await self.command("observe_property", 1, "time-pos")
        await self.command("observe_property", 2, "duration")
        log.info("mpv ready on %s", self.socket_path)

    async def stop_process(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _read_loop(self) -> None:
        assert self._reader
        while True:
            try:
                line = await self._reader.readline()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                break
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "request_id" in msg:
                fut = self._pending.pop(msg["request_id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("event") == "property-change":
                if msg.get("name") == "time-pos":
                    self.position = msg.get("data") or 0.0
                elif msg.get("name") == "duration":
                    self.duration = msg.get("data") or 0.0
            elif msg.get("event") == "end-file":
                self._end_reason = msg.get("reason")
                self._end.set()
        log.warning("mpv IPC connection closed")

    async def command(self, *args, timeout: float = 10.0):
        if not self._writer:
            raise MpvError("mpv is not running")
        rid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
        self._writer.write(payload.encode())
        await self._writer.drain()
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise MpvError(f"mpv command timed out: {args[0]}")
        if resp.get("error") != "success":
            raise MpvError(f"mpv rejected {args[0]}: {resp.get('error')}")
        return resp.get("data")

    async def play(self, url: str) -> None:
        """Start a file. Clears the end flag first so a stale event can't leak through."""
        self._end.clear()
        self._end_reason = None
        self.position = 0.0
        self.duration = 0.0
        await self.command("loadfile", url, "replace")

    async def wait_for_end(self) -> str:
        await self._end.wait()
        return self._end_reason or "unknown"

    async def skip(self) -> None:
        # Fires end-file with reason "stop", which releases wait_for_end().
        await self.command("stop")

    async def set_pause(self, paused: bool) -> None:
        await self.command("set_property", "pause", paused)

    async def is_paused(self) -> bool:
        try:
            return bool(await self.command("get_property", "pause"))
        except MpvError:
            return False

    async def set_volume(self, value: int) -> None:
        value = max(0, min(130, int(value)))
        await self.command("set_property", "volume", value)
        self.volume = value

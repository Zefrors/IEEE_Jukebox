"""The play queue.

Ordering is round-robin by submitter rather than first-in-first-out: everybody's
first pick plays before anybody's second. A person who joins late starts level
with whoever currently has the fewest plays among people still in the queue, so
they get a turn soon without leapfrogging the whole room.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field


class QueueError(Exception):
    """Raised for conditions the submitter should see as a message, not a 500."""


@dataclass
class Track:
    video_id: str
    title: str
    artist: str
    duration: int
    thumbnail: str | None
    submitter: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    added_at: float = field(default_factory=time.time)
    stream_url: str | None = None
    stream_expires: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "video_id": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "submitter": self.submitter,
        }


class PlayQueue:
    def __init__(self, max_per_person: int = 3, max_total: int = 60):
        self.max_per_person = max_per_person
        self.max_total = max_total
        self._pending: list[Track] = []
        self._plays: dict[str, int] = {}
        self._cond = asyncio.Condition()

    def snapshot(self) -> list[dict]:
        """Queue in the order it will actually play."""
        return [t.as_dict() for t in self._ordered()]

    def _ordered(self) -> list[Track]:
        by_person: dict[str, list[Track]] = {}
        for t in self._pending:
            by_person.setdefault(t.submitter, []).append(t)

        order: list[Track] = []
        counts = dict(self._plays)
        remaining = {k: list(v) for k, v in by_person.items()}
        while remaining:
            person = min(
                remaining,
                key=lambda p: (counts.get(p, 0), remaining[p][0].added_at),
            )
            order.append(remaining[person].pop(0))
            counts[person] = counts.get(person, 0) + 1
            if not remaining[person]:
                del remaining[person]
        return order

    async def add(self, track: Track) -> Track:
        async with self._cond:
            if len(self._pending) >= self.max_total:
                raise QueueError("The queue is full. Try again after a few songs play.")
            mine = [t for t in self._pending if t.submitter == track.submitter]
            if len(mine) >= self.max_per_person:
                raise QueueError(
                    f"You already have {self.max_per_person} songs waiting. "
                    "Remove one to add another."
                )
            if any(t.video_id == track.video_id for t in self._pending):
                raise QueueError("That song is already in the queue.")

            if track.submitter not in self._plays:
                active = [self._plays.get(t.submitter, 0) for t in self._pending]
                self._plays[track.submitter] = min(active, default=0)

            self._pending.append(track)
            self._cond.notify_all()
            return track

    async def remove(self, entry_id: str, requester: str, force: bool = False) -> bool:
        async with self._cond:
            for i, t in enumerate(self._pending):
                if t.id == entry_id:
                    if not force and t.submitter != requester:
                        raise QueueError("You can only remove songs you added.")
                    del self._pending[i]
                    return True
            return False

    async def next_track(self) -> Track:
        """Block until a track is available, then pop the fair-next one."""
        async with self._cond:
            while not self._pending:
                await self._cond.wait()
            track = self._ordered()[0]
            self._pending.remove(track)
            self._plays[track.submitter] = self._plays.get(track.submitter, 0) + 1
            return track

    def peek_next(self) -> Track | None:
        order = self._ordered()
        return order[0] if order else None

    async def clear(self) -> None:
        async with self._cond:
            self._pending.clear()
            self._plays.clear()

"""In-memory session store for multi-step download flows."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bot.config import SESSION_TTL


@dataclass
class DownloadSession:
    """Holds media metadata and user choices for a pending download."""

    session_id: str
    user_id: int
    chat_id: int
    url: str
    created_at: float = field(default_factory=time.time)

    # Filled after info extraction
    title: str = ""
    platform: str = ""
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    view_count: int | None = None
    description: str | None = None
    is_live: bool = False
    is_playlist: bool = False
    playlist_count: int = 0

    # Available options detected from formats
    has_video: bool = False
    has_audio: bool = False
    has_image: bool = False
    has_subtitles: bool = False
    subtitle_langs: list[str] = field(default_factory=list)
    available_heights: list[int] = field(default_factory=list)
    available_image_sizes: list[tuple[int, int]] = field(default_factory=list)
    estimated_sizes: dict[str, int] = field(default_factory=dict)  # quality -> bytes
    extractor: str = ""
    raw_info: dict[str, Any] = field(default_factory=dict)

    # User selections
    mode: str | None = None  # video | video_subs | audio | image
    quality: str | None = None  # 480 | 720 | 1080 | max
    subtitle_lang: str | None = None
    audio_format: str = "mp3"  # mp3 | m4a | opus
    image_index: int = 0  # for carousels

    # UI message tracking
    prompt_message_id: int | None = None
    status_message_id: int | None = None

    def expired(self, ttl: int = SESSION_TTL) -> bool:
        return (time.time() - self.created_at) > ttl


class SessionStore:
    """Thread-safe enough for asyncio single-loop usage."""

    def __init__(self) -> None:
        self._sessions: dict[str, DownloadSession] = {}
        self._by_user: dict[int, str] = {}

    def put(self, session: DownloadSession) -> None:
        old = self._by_user.get(session.user_id)
        if old and old in self._sessions and old != session.session_id:
            del self._sessions[old]
        self._sessions[session.session_id] = session
        self._by_user[session.user_id] = session.session_id

    def get(self, session_id: str) -> DownloadSession | None:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        if s.expired():
            self.remove(session_id)
            return None
        return s

    def get_for_user(self, user_id: int) -> DownloadSession | None:
        sid = self._by_user.get(user_id)
        if not sid:
            return None
        return self.get(sid)

    def remove(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s and self._by_user.get(s.user_id) == session_id:
            del self._by_user[s.user_id]

    def cleanup_expired(self) -> int:
        dead = [sid for sid, s in self._sessions.items() if s.expired()]
        for sid in dead:
            self.remove(sid)
        return len(dead)


sessions = SessionStore()

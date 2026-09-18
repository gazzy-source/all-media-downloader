"""Pytest shared fixtures and environment setup."""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Ensure project root is importable no matter where pytest is invoked from
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# bot.config reads env at import time — set BEFORE any bot.* import.
# os.environ wins over .env (load_dotenv does not override existing vars).
_SANDBOX = Path(tempfile.mkdtemp(prefix="amb_test_"))
os.environ["TEMP_DIR"] = str(_SANDBOX / "temp")
os.environ["DOWNLOAD_DIR"] = str(_SANDBOX / "downloads")
os.environ["AUTO_DOWNLOAD_ALWAYS"] = "0"
os.environ["DM_FAST_AUTO"] = "0"
os.environ["AUTO_DOWNLOAD_GROUPS"] = "1"
os.environ.pop("COOKIES_FILE", None)  # keep cookies out of the test run


# ---------------------------------------------------------------------------
# Duck-typed Telegram fakes (real telegram.Update is a pydantic model and
# would reject non-Message objects; handlers only use attributes, so plain
# objects work fine).
# ---------------------------------------------------------------------------
@dataclass
class FakeUser:
    id: int = 42
    username: str = "tester"


@dataclass
class FakeChat:
    id: int = 100
    type: str = "private"
    title: str | None = None


@dataclass
class FakeMessage:
    text: str | None = None
    caption: str | None = None
    from_user: Any = None
    chat: FakeChat = field(default_factory=FakeChat)
    message_id: int = 555
    replies: list = field(default_factory=list)
    edits: list = field(default_factory=list)
    children: list = field(default_factory=list)
    deleted: bool = False
    _next_id: int = 1000

    async def reply_text(self, text, **kw):
        m = FakeMessage(text=text, chat=self.chat, from_user=self.from_user,
                        message_id=self._next_id)
        self._next_id += 1
        m.replies = self.replies
        self.replies.append((text, kw))
        self.children.append(m)
        return m

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))
        return self

    async def delete(self, **kw):
        self.deleted = True
        return True


@dataclass
class FakeCallbackQuery:
    data: str | None = "go:abc"
    from_user: FakeUser = field(default_factory=FakeUser)
    message: FakeMessage | None = field(default_factory=FakeMessage)
    answers: list = field(default_factory=list)
    edits: list = field(default_factory=list)

    async def answer(self, text=None, **kw):
        self.answers.append((text, kw))
        return True

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))
        return True


class FakeUpdate:
    """Duck-typed Update: only attributes the handlers actually use."""

    def __init__(self, message: FakeMessage | None = None,
                 callback_query: FakeCallbackQuery | None = None,
                 effective_user: FakeUser | None = None):
        if message is not None and message.from_user is None:
            message.from_user = effective_user or FakeUser()
        self.effective_message = message
        self.callback_query = callback_query
        self.effective_user = effective_user or (
            message.from_user if message is not None else
            (callback_query.from_user if callback_query else None)
        )
        self.effective_chat = message.chat if message is not None else (
            callback_query.message.chat if callback_query and callback_query.message else None
        )


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[str, tuple, dict]] = []
        self.chat_actions: list[tuple[int, str]] = []
        self.edits: list = []
        self.media_edits: list = []
        self.deletes: list = []

    async def send_chat_action(self, chat_id, action, **kw):
        self.chat_actions.append((chat_id, action))
        return True

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("send_message", (chat_id, text), kw))
        return FakeMessage(text=text, chat=FakeChat(id=chat_id))

    async def send_video(self, chat_id, video=None, **kw):
        self.sent.append(("send_video", (chat_id,), kw))
        return FakeMessage(chat=FakeChat(id=chat_id))

    async def send_audio(self, chat_id, audio=None, **kw):
        self.sent.append(("send_audio", (chat_id,), kw))
        return FakeMessage(chat=FakeChat(id=chat_id))

    async def send_photo(self, chat_id, photo=None, **kw):
        self.sent.append(("send_photo", (chat_id,), kw))
        return FakeMessage(chat=FakeChat(id=chat_id))

    async def send_document(self, chat_id, document=None, **kw):
        self.sent.append(("send_document", (chat_id,), kw))
        return FakeMessage(chat=FakeChat(id=chat_id))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append((text, chat_id, message_id, kw))
        return True

    # Kept in step with telegram.Bot: a missing method here fails as an
    # AttributeError deep inside a handler instead of exercising the code.
    async def edit_message_media(self, chat_id=None, message_id=None, media=None, **kw):
        self.media_edits.append((chat_id, message_id, media, kw))
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self.deletes.append((chat_id, message_id))
        return True


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


def make_update(message=None, callback_query=None, effective_user=None):
    return FakeUpdate(message=message, callback_query=callback_query,
                      effective_user=effective_user)


@pytest.fixture
def fx():
    """Bundle of fakes for handler tests."""
    return SimpleBundle()


class SimpleBundle:
    def __init__(self):
        self.user = FakeUser()
        self.chat = FakeChat()
        self.ctx = FakeContext()

    def msg(self, text=""):
        return FakeMessage(text=text, chat=self.chat, from_user=self.user)

    def update(self, message=None, callback_query=None):
        return make_update(message=message, callback_query=callback_query,
                           effective_user=self.user)


@pytest.fixture(autouse=True)
def _neutral_network_config(monkeypatch):
    """
    Pin PROXY/PROXY_HOSTS to "no proxy configured" for every test.

    These are read from the deployment's .env, so without this a server that
    actually configures a proxy runs a different code path than CI and tests
    pass locally while failing in production (exactly what happened when
    PROXY_HOSTS was introduced). Tests that exercise proxying set both
    explicitly.
    """
    import bot.services.downloader as dl

    monkeypatch.setattr(dl, "PROXY", None)
    monkeypatch.setattr(dl, "PROXY_HOSTS", ())


@pytest.fixture(autouse=True)
def _no_pot_probe(monkeypatch):
    """
    Pin the PO-token probe to "no provider" so tests never hit the network or
    change behaviour based on whether a bgutil server happens to run locally.
    Tests that exercise the probe itself reset these two globals themselves.
    """
    import bot.services.downloader as dl

    monkeypatch.setattr(dl, "_POT_RESOLVED", True)
    monkeypatch.setattr(dl, "_POT_ARGS", {})


@pytest.fixture(autouse=True)
def _clean_global_state():
    """Keep global stores isolated between tests."""
    from bot.services.rate_limit import rate_limiter
    from bot.services.session import sessions

    rate_limiter._hits.clear()
    sessions._sessions.clear()
    sessions._by_user.clear()
    yield
    rate_limiter._hits.clear()
    sessions._sessions.clear()
    sessions._by_user.clear()

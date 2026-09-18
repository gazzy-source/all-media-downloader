"""
Telegram's editMessageText accepts an InlineKeyboardMarkup ONLY.

Passing the persistent ReplyKeyboardMarkup raises
BadRequest("Inline keyboard expected"), which escapes to the global error
handler and shows the user "Something went wrong. Please try again or /cancel."
instead of the real reason. That is exactly what happened on every link the bot
could not read, so this contract is enforced structurally.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

import bot.keyboards.menus as menus

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"
EDIT_METHODS = {"edit_text", "edit_message_text", "edit_message_reply_markup"}


def _reply_keyboard_factories() -> set[str]:
    """Names in menus.py that return a ReplyKeyboardMarkup."""
    out = set()
    for name in dir(menus):
        fn = getattr(menus, name)
        if not callable(fn) or not name.endswith("keyboard"):
            continue
        try:
            result = fn()
        except TypeError:
            continue  # needs a session argument; those are inline builders
        if isinstance(result, ReplyKeyboardMarkup):
            out.add(name)
    return out


def _edit_calls_with_reply_markup():
    for path in sorted(BOT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in EDIT_METHODS:
                continue
            for kw in node.keywords:
                if kw.arg == "reply_markup":
                    yield path, node.lineno, name, kw.value


class TestEditMessageKeyboardContract:
    def test_reply_keyboard_factory_detected(self):
        """Guard the guard: the scan is meaningless if this set is empty."""
        assert "main_reply_keyboard" in _reply_keyboard_factories()

    def test_no_edit_call_passes_a_reply_keyboard(self):
        bad = []
        reply_factories = _reply_keyboard_factories()
        for path, lineno, method, value in _edit_calls_with_reply_markup():
            called = (
                value.func.id
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                else None
            )
            if called in reply_factories:
                rel = path.relative_to(BOT_DIR.parent)
                bad.append(f"{rel}:{lineno} {method}(reply_markup={called}())")
        assert not bad, (
            "editMessageText only accepts an inline keyboard; these raise "
            'BadRequest("Inline keyboard expected"): ' + "; ".join(bad)
        )


class TestKeyboardTypes:
    def test_main_reply_keyboard_is_a_reply_keyboard(self):
        assert isinstance(menus.main_reply_keyboard(), ReplyKeyboardMarkup)

    @pytest.mark.parametrize("name", ["after_download_keyboard"])
    def test_inline_builders_return_inline(self, name):
        kb = getattr(menus, name)("https://example.com/x", user_id=1)
        assert isinstance(kb, InlineKeyboardMarkup)


class TestFailedLinkShowsRealReason:
    """
    The live symptom: a bot-walled YouTube link produced
    "Something went wrong. Please try again or /cancel." because the error
    branch itself raised. It must report the actual reason instead.

    NOTE: patch `type(msg)`, not an imported conftest class — pytest loads
    conftest under its own module name, so `tests.conftest.FakeMessage` is a
    different object from the one the `fx` fixture builds and patching it
    silently does nothing.
    """

    URL = "https://www.youtube.com/watch?v=rhC654cK6LM"

    @staticmethod
    def _shown(msg) -> str:
        out = " ".join(t for t, _ in msg.replies)
        for child in msg.children:
            out += " " + " ".join(t for t, _ in child.edits)
        return out

    async def _arrange(self, fx, monkeypatch):
        import bot.handlers.download as hd

        async def boom(url):
            raise RuntimeError(
                "ERROR: [youtube] rhC654cK6LM: Sign in to confirm you're not a bot."
            )

        monkeypatch.setattr(hd.download_manager, "extract_info", boom)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        return hd, fx.msg(self.URL)

    async def test_reports_reason_not_generic_crash(self, fx, monkeypatch):
        """Must not raise, and must tell the user what actually happened."""
        hd, msg = await self._arrange(fx, monkeypatch)
        await hd.start_url_flow(fx.update(msg), fx.ctx, self.URL)
        shown = self._shown(msg)
        assert "Could not read this link" in shown, shown[:400]
        assert "bot-walled" in shown or "PO-token" in shown, shown[:400]
        assert "Something went wrong" not in shown

    async def test_falls_back_to_a_new_message_if_edit_fails(self, fx, monkeypatch):
        """Even when Telegram rejects the edit, the reason still reaches the user."""
        from telegram.error import BadRequest

        hd, msg = await self._arrange(fx, monkeypatch)
        calls = {"n": 0}

        async def bad_edit(self, text, **kw):
            calls["n"] += 1
            raise BadRequest("Inline keyboard expected")

        monkeypatch.setattr(type(msg), "edit_text", bad_edit)
        await hd.start_url_flow(fx.update(msg), fx.ctx, self.URL)

        assert calls["n"] > 0, "the edit path must actually have been exercised"
        replies = " ".join(t for t, _ in msg.replies)
        assert "Could not read this link" in replies, replies[:400]

    async def test_edit_is_never_given_a_reply_keyboard(self, fx, monkeypatch):
        hd, msg = await self._arrange(fx, monkeypatch)
        seen = []

        async def spy(self, text, **kw):
            seen.append(kw.get("reply_markup"))
            return self

        monkeypatch.setattr(type(msg), "edit_text", spy)
        await hd.start_url_flow(fx.update(msg), fx.ctx, self.URL)

        assert seen, "the error branch must edit the status message"
        assert all(not isinstance(m, ReplyKeyboardMarkup) for m in seen), (
            "edit_text was handed a ReplyKeyboardMarkup again"
        )

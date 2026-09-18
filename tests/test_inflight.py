"""
A restart used to strand the user: the "Downloading…" message froze forever.

Four bot restarts in two hours during live testing produced exactly that, with
no log trace — an interrupted job logs nothing.
"""
from __future__ import annotations

import json

import pytest

import bot.services.inflight as inflight


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight, "_PATH", tmp_path / "inflight.json")
    yield


class TestRegistry:
    def test_add_then_drain_returns_the_job(self):
        inflight.add(-100, 55)
        assert inflight.drain() == [{"chat_id": -100, "message_id": 55}]

    def test_drain_clears_so_a_job_is_flagged_once(self):
        inflight.add(-100, 55)
        inflight.drain()
        assert inflight.drain() == [], "a second restart must not re-flag it"

    def test_completed_job_is_not_reported(self):
        inflight.add(1, 10)
        inflight.add(1, 11)
        inflight.remove(1, 10)
        assert inflight.drain() == [{"chat_id": 1, "message_id": 11}]

    def test_add_is_idempotent(self):
        inflight.add(1, 10)
        inflight.add(1, 10)
        assert len(inflight.drain()) == 1

    def test_remove_of_unknown_job_is_harmless(self):
        inflight.remove(999, 999)
        assert inflight.drain() == []

    def test_survives_a_corrupt_file(self, tmp_path):
        """A truncated write must not take the whole bot down on startup."""
        inflight._PATH.write_text("{ this is not json", encoding="utf-8")
        assert inflight.drain() == []
        inflight.add(1, 2)
        assert inflight.drain() == [{"chat_id": 1, "message_id": 2}]

    def test_file_is_bounded(self):
        for i in range(260):
            inflight.add(1, i)
        assert len(inflight.drain()) <= 200

    def test_written_atomically(self):
        """Rename-into-place: no .tmp left behind, file is valid JSON."""
        inflight.add(7, 8)
        assert not list(inflight._PATH.parent.glob("*.tmp"))
        assert json.loads(inflight._PATH.read_text(encoding="utf-8"))


class TestStartupRescue:
    async def test_notifies_each_interrupted_chat_then_clears(self, monkeypatch):
        import bot.main as bm

        inflight.add(-100, 5)
        inflight.add(-200, 6)
        sent = []

        class Bot:
            async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
                sent.append((chat_id, message_id, text))

        class App:
            bot = Bot()

        await bm._rescue_interrupted_jobs(App())
        assert {c for c, _, _ in sent} == {-100, -200}
        assert all("Interrupted" in t for _, _, t in sent)
        assert all("send the link again" in t.lower() for _, _, t in sent)
        assert inflight.drain() == [], "registry must be cleared after notifying"

    async def test_a_failing_edit_never_blocks_startup(self, monkeypatch):
        """Deleted message / lost rights must not stop the bot from booting."""
        import bot.main as bm

        inflight.add(-100, 5)

        class Bot:
            async def edit_message_text(self, *a, **kw):
                raise RuntimeError("message to edit not found")

        class App:
            bot = Bot()

        await bm._rescue_interrupted_jobs(App())  # must not raise

    async def test_no_jobs_is_a_no_op(self):
        import bot.main as bm

        class App:
            bot = None  # touching it would raise, proving we returned early

        await bm._rescue_interrupted_jobs(App())


class TestFailurePathsClearTheRegistry:
    """
    The audit caught this: inflight_remove sat only in the final `finally`, but
    three failure paths return before reaching it. A job that already failed
    AND told the user would then be resurrected as "⚠️ Interrupted" on the next
    restart — a second, contradictory message about a job that was never lost.
    """

    def test_every_return_after_inflight_add_is_covered(self):
        """Structural: no `return` between inflight_add and its finally may
        skip the matching inflight_remove."""
        import ast
        import pathlib

        src = pathlib.Path("bot/handlers/download.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        def check(fn_name):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                      and n.name == fn_name)
            add_line = min(
                n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "inflight_add"
            )
            removes = {n.lineno for n in ast.walk(fn)
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "id", "") == "inflight_remove"}
            lines = src.split("\n")
            unguarded = []
            for node in ast.walk(fn):
                if not isinstance(node, ast.Return) or node.lineno <= add_line:
                    continue
                # the remove must be one of the few lines immediately before
                if not any(r in range(node.lineno - 3, node.lineno) for r in removes):
                    unguarded.append((node.lineno, lines[node.lineno - 1].strip()))
            return unguarded

        bad = check("auto_download_flow")
        assert not bad, f"returns that leak an in-flight entry: {bad}"

    def test_registry_has_a_remove_for_every_add(self):
        import pathlib
        src = pathlib.Path("bot/handlers/download.py").read_text(encoding="utf-8")
        adds = src.count("inflight_add(")
        removes = src.count("inflight_remove(")
        assert removes >= adds, (
            f"{adds} add sites but only {removes} remove sites — a path leaks"
        )

"""
Guards against tests that silently stop running.

A second `class TestYtStrategies` in test_downloader.py shadowed the first one,
disabling four real assertions without any warning — pytest only collects the
last binding of a name in a module.
"""
from __future__ import annotations

import ast
import pathlib
from collections import Counter

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _test_files():
    return sorted(p for p in TESTS_DIR.glob("test_*.py"))


class TestNoShadowedTests:
    def test_no_duplicate_test_class_names_within_a_module(self):
        offenders = []
        for path in _test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [
                n.name for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name.startswith("Test")
            ]
            for name, count in Counter(names).items():
                if count > 1:
                    offenders.append(f"{path.name}: {name} defined {count}x")
        assert not offenders, (
            "later definitions shadow earlier ones and silently disable those "
            "tests: " + "; ".join(offenders)
        )

    def test_no_duplicate_test_function_names_within_a_class(self):
        offenders = []
        for path in _test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                names = [
                    f.name for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and f.name.startswith("test_")
                ]
                for name, count in Counter(names).items():
                    if count > 1:
                        offenders.append(f"{path.name}::{node.name}::{name} x{count}")
        assert not offenders, "shadowed test methods: " + "; ".join(offenders)

    def test_every_test_module_actually_contains_tests(self):
        """A module with no collectable tests is usually an accident."""
        empty = []
        for path in _test_files():
            src = path.read_text(encoding="utf-8")
            if "def test_" not in src:
                empty.append(path.name)
        assert not empty, f"test modules with no tests: {empty}"


class TestTempCleanupKeepsGitkeep:
    """
    The periodic cleanup swept temp/.gitkeep — a tracked file — leaving every
    deployment with a dirty working tree and breaking `git pull --ff-only`.
    """

    async def test_gitkeep_survives_cleanup(self, tmp_path, monkeypatch):
        import time as _t
        import bot.main as bm

        monkeypatch.setattr(bm, "TEMP_DIR", tmp_path)
        old = _t.time() - 10 * 3600

        keep = tmp_path / ".gitkeep"
        keep.write_text("")
        junk = tmp_path / "dl_abc.mp4"
        junk.write_bytes(b"x")
        nested = tmp_path / "dl_dir"
        nested.mkdir()
        nested_junk = nested / "old.part"
        nested_junk.write_bytes(b"x")
        for f in (keep, junk, nested_junk):
            import os
            os.utime(f, (old, old))

        class Ctx:
            pass

        await bm.cleanup_job(Ctx())

        assert keep.exists(), "temp/.gitkeep must not be deleted"
        assert not junk.exists(), "stale downloads must still be cleaned"
        assert not nested_junk.exists(), "nested stale files must still be cleaned"

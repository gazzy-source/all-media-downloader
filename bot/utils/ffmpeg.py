"""Locate FFmpeg on Windows/Linux even when not on PATH (common with winget)."""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _candidate_bins() -> list[Path]:
    """Build a list of directories that might contain ffmpeg.exe / ffmpeg."""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))

    dirs: list[Path] = []

    # Explicit env (file or directory)
    env = os.getenv("FFMPEG_LOCATION") or os.getenv("FFMPEG_PATH")
    if env:
        p = Path(env)
        if p.is_file():
            dirs.append(p.parent)
        else:
            dirs.append(p)

    # WinGet Gyan.FFmpeg package (Windows)
    winget_pkg = local / "Microsoft" / "WinGet" / "Packages"
    if winget_pkg.is_dir():
        for pkg in winget_pkg.glob("Gyan.FFmpeg*"):
            for bin_dir in pkg.glob("**/bin"):
                if (bin_dir / "ffmpeg.exe").exists() or (bin_dir / "ffmpeg").exists():
                    dirs.append(bin_dir)
        # Scoop / other layouts
        for exe in winget_pkg.glob("**/ffmpeg.exe"):
            dirs.append(exe.parent)

    # Common install roots
    for root in (
        pf / "ffmpeg" / "bin",
        pf86 / "ffmpeg" / "bin",
        Path(r"C:\ffmpeg\bin"),
        home / "ffmpeg" / "bin",
        home / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
        home / "scoop" / "shims",
        Path(r"C:\tools\ffmpeg\bin"),
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ):
        dirs.append(root)

    # WinGet Links folder
    links = local / "Microsoft" / "WinGet" / "Links"
    if links.is_dir():
        dirs.append(links)

    return dirs


@lru_cache(maxsize=1)
def find_ffmpeg() -> Path | None:
    """
    Return path to the ffmpeg binary, or None if not found.
    Also ensures the bin directory is on PATH for subprocesses yt-dlp may spawn.
    """
    # 1. PATH
    which = shutil.which("ffmpeg")
    if which:
        path = Path(which)
        _ensure_path(path.parent)
        logger.info("FFmpeg found on PATH: %s", path)
        return path

    exe_names = ("ffmpeg.exe", "ffmpeg")
    for d in _candidate_bins():
        try:
            if not d.exists():
                continue
            for name in exe_names:
                candidate = d / name
                if candidate.is_file():
                    _ensure_path(d)
                    logger.info("FFmpeg found: %s", candidate)
                    return candidate
        except OSError:
            continue

    logger.warning(
        "FFmpeg not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_LOCATION."
    )
    return None


def ffmpeg_location_dir() -> str | None:
    """Directory containing ffmpeg — for yt-dlp's ffmpeg_location option."""
    ff = find_ffmpeg()
    return str(ff.parent) if ff else None


def _ensure_path(directory: Path) -> None:
    """Prepend directory to process PATH so child processes see ffmpeg."""
    s = str(directory)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    if s not in parts:
        os.environ["PATH"] = s + os.pathsep + current

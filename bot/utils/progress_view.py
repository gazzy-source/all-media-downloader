"""
One clear progress display for every download.

Before: a bare percentage bar that ran to 100%, a moving "Resolving…" line and
no sense of time — users couldn't tell when a download started, what it was
doing, or when it would finish. This turns the downloader's progress events
into four plain steps with their own timings:

    ✅ Finding source · 2s
    ⏳ Downloading · 45%
       ▰▰▰▰▱▱▱▱▱▱  3.2 MB of 7.1 MB · ~2s left
    ◻️ Finishing
    ◻️ Sending
    ⏱ 6s

…and a one-line version for an inline card's button ("⬇ 45% · ~2s left").
Rendering is pure — callers decide when to edit (on a step change, or every
few seconds), which keeps Telegram edits well under its limits.
"""

from __future__ import annotations

import re
import time

STEPS = (("find", "Finding source"), ("download", "Downloading"),
         ("process", "Finishing"), ("send", "Sending"))
_ORDER = {key: i for i, (key, _) in enumerate(STEPS)}
_PROCESS_HINTS = ("Joining", "Converting", "cover art", "title & artist", "Finishing",
                  "subtitles")
_ETA_RE = re.compile(r"~\s*(\d+)\s*s left")
_QUEUE_RE = re.compile(r"#\s*(\d+)")


def _secs(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "▰" * filled + "▱" * (width - filled)


class ProgressView:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.stage = "find"
        self.started: dict[str, float] = {"find": self.t0}
        self.ended: dict[str, float] = {}
        self.pct = 0.0
        self.detail = ""
        self.queue: int | None = None

    # ------------------------------------------------------------ updates
    def _enter(self, stage: str) -> bool:
        if stage == self.stage or _ORDER[stage] < _ORDER[self.stage]:
            return False  # steps only move forward
        now = time.monotonic()
        for key, _ in STEPS:
            if _ORDER[key] < _ORDER[stage]:
                self.started.setdefault(key, now)
                self.ended.setdefault(key, now)
        self.started[stage] = now
        self.stage = stage
        return True

    def update(self, pct: float, msg: str) -> bool:
        """Feed one downloader event. True when the visible step changed."""
        m = (msg or "").strip()
        if m.startswith("Queued"):
            n = _QUEUE_RE.search(m)
            changed = self.queue != (int(n.group(1)) if n else 0)
            self.queue = int(n.group(1)) if n else 0
            return changed
        was_queued, self.queue = self.queue is not None, None
        if was_queued:  # the wait is over: the job really starts now
            self.t0 = self.started["find"] = time.monotonic()
        if m.startswith("⬇"):
            changed = self._enter("download")
            self.pct = max(self.pct, float(pct))
            self.detail = m.lstrip("⬇").strip()
            if self.detail.lower().startswith("audio "):
                self.detail = self.detail[6:]
            # "45% · 3.2 MB of 7.1 MB · …" — the percent is shown on its own.
            self.detail = re.sub(r"^\d+%\s*·?\s*", "", self.detail)
            return changed or was_queued
        if pct >= 99 or any(h in m for h in _PROCESS_HINTS):
            changed = self._enter("process")
            self.detail = m
            return changed or was_queued
        self.detail = m if self.stage == "find" else self.detail
        return was_queued

    def sending(self, size_text: str = "") -> None:
        self._enter("send")
        self.detail = size_text

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    # ---------------------------------------------------------- rendering
    def render(self, header: str) -> str:
        """Full text for a chat message (HTML)."""
        lines = [header, ""]
        if self.queue is not None:
            lines.append(f"⏳ <b>In the queue</b> — you're #{self.queue}" if self.queue
                         else "⏳ <b>In the queue</b>")
            lines.append("<i>Starts as soon as a slot is free.</i>")
            return "\n".join(lines)
        now = time.monotonic()
        for key, label in STEPS:
            if key in self.ended:
                took = self.ended[key] - self.started.get(key, self.ended[key])
                lines.append(f"✅ {label} · {_secs(took)}")
            elif key == self.stage:
                if key == "download":
                    lines.append(f"⏳ <b>{label}</b> · {self.pct:.0f}%")
                    extra = f"  {self.detail}" if self.detail else ""
                    lines.append(f"   {bar(self.pct)}{extra}")
                elif key == "process" and self.detail:
                    lines.append(f"⏳ <b>{label}</b> · {self.detail}")
                elif key == "send" and self.detail:
                    lines.append(f"⏳ <b>{label}</b> · {self.detail}")
                else:
                    lines.append(f"⏳ <b>{label}</b> · {_secs(now - self.started[key])}")
            else:
                lines.append(f"◻️ {label}")
        lines.append(f"⏱ {_secs(self.elapsed())}")
        return "\n".join(lines)

    def short(self) -> str:
        """One line for an inline card's button."""
        if self.queue is not None:
            return f"⏳ Queued #{self.queue}" if self.queue else "⏳ Queued"
        el = _secs(self.elapsed())
        if self.stage == "download":
            eta = _ETA_RE.search(self.detail)
            return f"⬇ {self.pct:.0f}%" + (f" · ~{eta.group(1)}s left" if eta else f" · {el}")
        if self.stage == "process":
            return f"{(self.detail or '⚙️ Finishing…')[:40]} · {el}"
        if self.stage == "send":
            return f"📤 Sending · {el}"
        return f"🔎 Finding source · {el}"

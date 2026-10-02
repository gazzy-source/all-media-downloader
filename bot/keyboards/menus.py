"""Inline keyboards for the download flow."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from bot.config import PREMIUM_ENABLED
from bot.services.downloader import quality_buttons_meta
from bot.services.session import DownloadSession


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📥 New Download"), KeyboardButton("🕘 History")],
            [KeyboardButton("🌐 Platforms"), KeyboardButton("❓ Help")],
            [KeyboardButton("⭐ Premium" if PREMIUM_ENABLED else "📊 Stats"),
             KeyboardButton("⚙️ Settings")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def mode_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    """Choose download type based on what's available."""
    rows: list[list[InlineKeyboardButton]] = []
    sid = session.session_id

    if session.has_video:
        rows.append(
            [
                InlineKeyboardButton(
                    "🎥 Video", callback_data=f"mode:{sid}:video"
                ),
                InlineKeyboardButton(
                    "🎞 Video + Subtitles",
                    callback_data=f"mode:{sid}:video_subs",
                ),
            ]
        )
    if session.has_audio or session.has_video:
        rows.append(
            [
                InlineKeyboardButton(
                    "🎵 Audio Only", callback_data=f"mode:{sid}:audio"
                )
            ]
        )
    if session.has_image or (not session.has_video and not session.has_audio):
        rows.append(
            [
                InlineKeyboardButton(
                    "🖼 Image", callback_data=f"mode:{sid}:image"
                )
            ]
        )
    # Always allow force options if detection is incomplete
    if not rows:
        rows = [
            [
                InlineKeyboardButton("🎥 Video", callback_data=f"mode:{sid}:video"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"mode:{sid}:audio"),
            ],
            [
                InlineKeyboardButton("🖼 Image", callback_data=f"mode:{sid}:image"),
            ],
        ]

    rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}")]
    )
    return InlineKeyboardMarkup(rows)


def quality_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    sid = session.session_id
    metas = quality_buttons_meta(session.available_heights, session.estimated_sizes)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for m in metas:
        row.append(
            InlineKeyboardButton(
                m["label"], callback_data=f"quality:{sid}:{m['key']}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"back_mode:{sid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def audio_format_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    sid = session.session_id
    formats = [
        ("mp3", "MP3 (universal)"),
        ("m4a", "M4A (AAC)"),
        ("opus", "Opus (small)"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                label, callback_data=f"aformat:{sid}:{fmt}"
            )
        ]
        for fmt, label in formats
    ]
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"back_mode:{sid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def subtitle_lang_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    sid = session.session_id
    langs = session.subtitle_langs[:12] or ["en"]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for lang in langs:
        row.append(
            InlineKeyboardButton(
                lang, callback_data=f"sublang:{sid}:{lang}"
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                "⚡ Auto (best English)", callback_data=f"sublang:{sid}:en.*"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"back_quality:{sid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def image_size_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    """Offer original + scaled options when multiple sizes exist."""
    sid = session.session_id
    sizes = session.available_image_sizes[:6]
    rows: list[list[InlineKeyboardButton]] = []
    if sizes:
        for i, (w, h) in enumerate(sizes):
            label = f"{w}×{h}"
            if i == 0:
                label = f"⭐ Original {label}"
            rows.append(
                [
                    InlineKeyboardButton(
                        label, callback_data=f"imgsize:{sid}:{i}"
                    )
                ]
            )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "⭐ Best Available", callback_data=f"imgsize:{sid}:0"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"back_mode:{sid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


# NOTE: there is no confirm_keyboard any more. Picking a quality / audio format
# / image size / subtitle language starts the download straight away, so the
# extra "Download Now" screen only cost a tap and a round-trip. The `go:`
# callback is still handled for buttons sitting in older chats.


def after_download_keyboard(url: str, user_id: int = 0, *, private: bool = True) -> InlineKeyboardMarkup:
    """
    Telegram callback_data max is 64 BYTES. Never put full URLs here.
    Store URL under a short token instead.
    """
    from bot.services.url_tokens import put_url

    token = put_url(url, user_id)
    # "again:" + 12 hex = 18 bytes — well under 64
    cb = f"again:{token}"
    assert len(cb.encode("utf-8")) <= 64
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Download Again", callback_data=cb
                )
            ],
        ] + ([[InlineKeyboardButton("📥 New Link", callback_data="new")]] if private else [])
    )


def settings_keyboard(prefs: dict, *, premium_enabled: bool = True) -> InlineKeyboardMarkup:
    """
    Tap-to-set defaults. With a type plus its quality/format chosen, a pasted
    link downloads straight away — no wizard. "Ask" brings the wizard back.
    callback_data: "pref:<key>:<value>" (short, allowlisted on receipt).
    """
    def row(key: str, options: list[tuple[str, str]]) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(
                ("✅ " if prefs.get(key) == value else "") + label,
                callback_data=f"pref:{key}:{value}",
            )
            for value, label in options
        ]

    rows = [
        row("mode", [("video", "🎥 Video"), ("audio", "🎵 Audio"), ("ask", "❓ Ask")]),
        row("quality", [("480", "480p"), ("720", "720p"), ("1080", "1080p"),
                        ("max", "Max"), ("ask", "Ask")]),
        row("audio", [("mp3", "MP3"), ("m4a", "M4A"), ("opus", "Opus"), ("ask", "Ask")]),
    ]
    if premium_enabled:
        rows.append([InlineKeyboardButton("💎 Premium", callback_data="prem:open")])
    return InlineKeyboardMarkup(rows)

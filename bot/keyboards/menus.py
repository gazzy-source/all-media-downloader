"""Inline keyboards for the download flow."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from bot.services.downloader import quality_buttons_meta
from bot.services.session import DownloadSession


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📥 New Download"), KeyboardButton("🕘 History")],
            [KeyboardButton("🌐 Platforms"), KeyboardButton("❓ Help")],
            [KeyboardButton("📊 Stats"), KeyboardButton("⚙️ Settings")],
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


def confirm_keyboard(session: DownloadSession) -> InlineKeyboardMarkup:
    sid = session.session_id
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Download Now", callback_data=f"go:{sid}"
                )
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"back_mode:{sid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{sid}"),
            ],
        ]
    )


def after_download_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Download Again", callback_data=f"again:{url[:180]}"
                )
            ],
            [InlineKeyboardButton("📥 New Link", callback_data="new")],
        ]
    )

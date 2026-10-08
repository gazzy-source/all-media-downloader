"""Conservative song classification and source-preserving delivery policy."""

from dataclasses import dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True)
class MusicClassification:
    is_music: bool
    confidence: str


def classify_music(info, url=""):
    title = str(info.get("title") or "").lower()
    negative = re.search(r"\b(podcast|interview|lecture|tutorial|gameplay|news|trailer)\b", title)
    if negative:
        return MusicClassification(False, "low")
    structured = bool(info.get("track") and (info.get("artist") or info.get("artists") or info.get("album_artist")))
    if structured:
        return MusicClassification(True, "high")
    short = info.get("is_short") or "/shorts/" in url
    if short:
        return MusicClassification(False, "low")
    if urlparse(url).hostname == "music.youtube.com":
        return MusicClassification(True, "high")
    raw_categories = info.get("categories") or []
    categories = {str(c).lower() for c in ([raw_categories] if isinstance(raw_categories, str) else raw_categories)}
    if "music" in categories or info.get("media_type") == "music":
        return MusicClassification(True, "medium")
    support = re.search(r"\b(official audio|official music video|lyric video|lyrics|visualizer)\b", title)
    provenance = (str(info.get("uploader") or "").endswith(" - Topic")
                  or "provided to youtube by" in str(info.get("description") or "").lower())
    if support and (provenance or info.get("artist") or info.get("album")):
        return MusicClassification(True, "medium")
    return MusicClassification(False, "low")


def codec_of(fmt):
    codec = str(fmt.get("acodec") or "").lower()
    if codec.startswith("mp4a"):
        return "aac"
    return codec


def _number(value):
    try:
        return max(0, float(value or 0))
    except (TypeError, ValueError):
        return 0


def select_best_music_audio(formats, delivery_target="telegram", max_bytes=None, duration=None):
    """Prefer playable native AAC/MP3; compare bitrate only within codec families.

    Telegram sendAudio documents MP3/M4A. Opus/FLAC are preserved for original
    delivery, but cannot displace a native music-player-compatible stream.
    Unknown sizes remain subject to the final upload guard. Known oversized
    candidates are excluded, without reducing below 75% of the best bitrate
    within the same codec family merely to fit.
    """
    if delivery_target not in {"telegram", "original"}:
        raise ValueError("unsupported delivery target")
    candidates = [f for f in formats if f.get("vcodec") == "none"
                  and codec_of(f) not in {"", "none"} and f.get("format_id")
                  and not f.get("has_drm")]
    native = [f for f in candidates if codec_of(f) in {"aac", "mp3", "alac", "flac"}
              or codec_of(f).startswith("pcm_")]
    pool = native if delivery_target == "telegram" and native else candidates
    def bitrate(f):
        return _number(f.get("abr") or f.get("tbr"))
    peaks = {}
    for f in pool:
        c = codec_of(f)
        peaks[c] = max(peaks.get(c, 0), bitrate(f))
    def fits(f):
        size = _number(f.get("filesize") or f.get("filesize_approx"))
        if not size and _number(duration) and bitrate(f):
            size = bitrate(f) * 1000 * _number(duration) / 8
        return not max_bytes or not size or size <= max_bytes
    pool = [f for f in pool if fits(f) and bitrate(f) >= peaks[codec_of(f)] * .75]
    if not pool:
        raise ValueError("No useful source audio fits the Telegram size limit")
    # Lossless wins when deliverable. AAC wins over MP3 when both native streams
    # exist; Opus wins over AAC only for original delivery. No cross-codec
    # bitrate equivalence or quality claim is inferred.
    preference = {"alac": 6, "flac": 6, "pcm_s16le": 6, "opus": 4,
                  "aac": 3, "vorbis": 2, "mp3": 1}
    def useful(f):
        # A severely constrained AAC stream must not beat a normal MP3 merely
        # because of its codec. Thresholds are a delivery heuristic, not a
        # cross-codec quality equivalence claim; unknown values stay eligible.
        rate = bitrate(f)
        mono = f.get("audio_channels") == 1
        minimum = {"aac": 64 if mono else 96, "mp3": 96 if mono else 128}
        return not rate or rate >= minimum.get(codec_of(f), 0)
    return max(pool, key=lambda f: (useful(f), 6 if codec_of(f).startswith("pcm_") else preference.get(codec_of(f), 0),
               bitrate(f), _number(f.get("audio_channels")),
               _number(f.get("asr")), _number(f.get("filesize"))))


def music_metadata(info, classification):
    """Structured tags only; never infer artist from uploader/channel."""
    result = dict(info)
    result["artist"] = (info.get("artist") or ", ".join(info.get("artists") or [])) if classification.is_music else None
    result["creator"] = None
    # FFmpegMetadata otherwise falls back to uploader/uploader_id for artist.
    result["meta_artist"] = result["artist"] or ""
    result["meta_album"] = (info.get("album") or "") if classification.is_music else ""
    result["meta_genre"] = (info.get("genre") or "") if classification.is_music else ""
    result["meta_date"] = (info.get("release_date") or info.get("release_year") or "") if classification.is_music else ""
    result["meta_date"] = str(result["meta_date"])
    if classification.is_music and info.get("track"):
        result["title"] = info["track"]
    elif classification.is_music:
        title = str(info.get("title") or "")
        cleaned = re.sub(r"\s*[\[(](?:official (?:audio|music video|video)|lyric video|lyrics|visualizer)[\])]\s*$",
                         "", title, flags=re.I).strip()
        result["title"] = cleaned or title
    if not classification.is_music:
        for key in ("album", "album_artist", "track", "track_number", "genre"):
            result.pop(key, None)
    return result

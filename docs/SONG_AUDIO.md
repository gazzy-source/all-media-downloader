# Song Best Audio

Best Audio means the best useful audio obtainable from the source while avoiding unnecessary quality loss. It does not manufacture lossless quality from a lossy source.

## Previous behavior [C]

Audio selected `bestaudio[ext=m4a]/bestaudio/best` and used
`FFmpegExtractAudio` with the requested codec and a 192 kbps setting. Inline
always requested M4A and shared `audio@m4a` with explicit M4A downloads.
The installed yt-dlp copies AAC when extracting to M4A and skips already
compatible files; conversion was unnecessary when an incompatible selected
source displaced an available native source. Metadata could infer artist from
uploader/channel, and there was no song classification.

## Classification [C]

Structured track plus artist, YouTube Music links, and Music category/type
qualify. Official audio/video, lyrics and visualizer titles require supporting
artist/album metadata or Topic/Provided-to-YouTube provenance. Keywords alone
do not qualify. Podcast/interview/lecture/tutorial/gameplay/news/trailer titles
are rejected without actual structured track/artist metadata. Structured song
tags take precedence over ambiguous title words (for example, a song called
"News"). Shorts require actual structured track plus artist metadata.
Uncertain content retains the generic M4A extraction policy.

## Selection and delivery [C]

Inline keeps the simple Video / Audio labels; its audio contract is now
`audio@best-v2`. Selection is resolved against the fresh formats of each download
attempt (including analysis reuse), so retries do not depend on stale format IDs.
Only audio-only, non-DRM formats qualify. Genuine lossless sources are preserved
when deliverable, including FLAC/WAV document delivery. Otherwise native AAC/MP3
sources are preferred for Telegram. Within a codec family bitrate, channels, sample rate and size break
ties. Deliverable lossless wins, then useful AAC over MP3; severely constrained
AAC cannot displace a normal MP3 solely by codec name. This is an explicit
delivery heuristic, not a claim that nominal bitrates measure quality across
codecs. The original selector also applies a conservative Opus bitrate floor
(96 kbps stereo / 64 kbps mono), so severely constrained Opus cannot displace
usable AAC by codec name alone. These floors are delivery heuristics, not
perceptual measurements. Unknown quality/size remains eligible and subject to existing final guards.

Telegram's [sendAudio contract](https://core.telegram.org/bots/api#sendaudio)
documents MP3/M4A for music-player presentation. Native AAC is copied/remuxed,
native MP3 is copied, and ALAC is remuxed to M4A. Opus is not a default when a
usable native compatible stream exists: converting it would add quality loss.
When only incompatible lossy sources exist, one AAC compatibility conversion
is permitted. The advanced/original selector can preserve Opus, but the inline
UI has no codec menu. Genuine FLAC/WAV-only sources remain lossless and are sent
as documents, with limited music-player presentation. No lossy source is converted
to a lossless output as a purported upgrade.

Explicit MP3 selects best available source audio and uses LAME VBR quality 0;
yt-dlp copies already-MP3 input. A 320 kbps label is not considered an upgrade
over native audio. Known oversized sources may fall back within the same codec
family only to at least 75% of its best bitrate. Otherwise the request explains
that no useful source fits. Lossless compression bitrate is not treated as
quality: selection uses source sample rate/channels/bit depth and a smaller
equivalent file may fit without degradation. Equivalent lossless sources prefer
native ALAC/M4A music-player delivery before file-size ties. Upload limits and
final size guards are unchanged.

## Metadata, artwork and cache [C]

Structured track replaces decorated video title. Structured artist, album,
album artist, track number and release date/year are retained; artist is never
inferred from uploader/channel. Genre must be explicit, rather than inferred
from categories/tags. Missing tags are left absent. Artwork is optional;
embedding failure is non-fatal and the existing square JPEG sidecar is used
for Telegram thumbnails.

Legacy `audio@m4a` entries remain readable by explicit M4A requests; there is
no migration or wipe. They cannot satisfy the new inline `audio@best-v2` contract.
Repeat inline requests hit that distinct slot, including generic inline audio;
the contract means classify and use best for songs, generic otherwise. Invalid
file IDs retain the existing forget-and-redownload behavior. Video cache keys
and delivery semantics are unchanged.

Queue, single-flight, upload streaming, UploadGate, worker bounds, image/video/
subtitle behavior, upload ceiling and YouTube/WARP strategy policy are preserved.
No dependencies or production configuration changes are required.

## Validation

Offline tests cover classification, native versus original delivery selection,
metadata, cache separation, size constraints, and real tiny FFmpeg/ffprobe
fixtures for native M4A/MP3 copying. CI does not use live YouTube. Production
client playback and artwork verification are separate release checks; local
codec-copy tests alone do not establish mobile/desktop playback quality.

Audio cache profiles are `audio@best-v2` for ordinary links, `audio@best-v2-music` for YouTube Music, and `audio@best-v2-short` for Shorts. Canonical aliases cannot bypass classification. Legacy `audio@m4a` and earlier `audio@best` entries remain stored but do not satisfy the revised contract. Repeat requests within each context reuse the cache. Video cache keys remain unchanged.

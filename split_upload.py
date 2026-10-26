"""
Split-upload for videos over Telegram's per-file limit (MAX_FILE_SIZE,
2GB by default).

Before this module existed, both the manual /download flow (main.py) and
the auto-scraper (auto_scraper.py) had no good answer for a video bigger
than that: the auto-scraper flagged it "skipped_size_limit" in the DB and
never touched it again (permanently, silently skipped — is_video_uploaded()
returns True for it forever), and the manual flow just let Telegram's own
upload call blow up with an unhandled error.

split_video_file() uses ffmpeg's segment muxer with stream copy (-c copy —
no re-encode, so splitting a multi-GB file takes seconds, not minutes) to
cut the source into playable .mp4 parts sized off the file's average
bitrate, then corrects any individual part that still landed over budget
(a higher-bitrate stretch of the video) with one targeted re-split pass on
just that part.

Uploading the resulting parts is left to each caller (auto_scraper.py,
main.py) since both already have their own per-part progress-bubble UI
("Uploading Part i/N...") that a generic uploader here couldn't hook into
cleanly — this module only owns the splitting.
"""

import glob
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _probe(path: str):
    """Returns (duration_seconds, width, height) for one video file, or
    (0.0, None, None) on any failure. Blocking — call via asyncio.to_thread."""
    try:
        import json
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = int(stream.get("width") or 0) or None
        height = int(stream.get("height") or 0) or None
        duration = float(data["format"]["duration"])
        return duration, width, height
    except Exception:
        return 0.0, None, None


def _ffmpeg_segment(src_path: str, out_pattern: str, segment_secs: int, timeout: int = 3600) -> list:
    """Blocking. Runs ffmpeg's segment muxer with stream copy and returns
    the resulting part paths in order. Returns [] if ffmpeg produced
    nothing (e.g. an unsupported/corrupt source)."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(segment_secs),
             "-reset_timestamps", "1", out_pattern],
            capture_output=True, timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"split_upload: ffmpeg segment failed for {src_path}: {e}")
        return []
    base, ext = out_pattern.split("%")[0], out_pattern.rsplit(".", 1)[-1]
    return sorted(glob.glob(f"{base}*.{ext}"))


def split_video_file(src_path: str, work_dir: str, base_name: str, max_part_bytes: int) -> list:
    """Splits src_path into playable .mp4 parts, each intended to land
    under max_part_bytes. Returns the part paths in playback order and
    deletes src_path once split successfully (mirrors the caller's
    existing single-file cleanup — nothing double-removes it). On any
    failure to split at all, returns [src_path] unchanged so the caller
    can fall back to its old behavior."""
    file_size = os.path.getsize(src_path)
    duration, _, _ = _probe(src_path)
    if duration <= 0:
        logger.warning(f"split_upload: couldn't read duration for {src_path}, can't split")
        return [src_path]

    avg_bytes_per_sec = file_size / duration
    segment_secs = max(30, int(max_part_bytes / avg_bytes_per_sec))
    out_pattern = os.path.join(work_dir, f"{base_name}_part%03d.mp4")
    parts = _ffmpeg_segment(src_path, out_pattern, segment_secs)
    if not parts:
        return [src_path]

    # Correct any part that landed over budget (a higher-bitrate stretch
    # vs. the file's average) with one targeted re-split of just that
    # part, scaled to its own local bitrate.
    fixed_parts = []
    for part in parts:
        part_size = os.path.getsize(part)
        if part_size <= max_part_bytes:
            fixed_parts.append(part)
            continue
        part_duration, _, _ = _probe(part)
        if part_duration <= 0:
            fixed_parts.append(part)  # nothing more we can do — ship as-is
            continue
        tighter_secs = max(10, int(part_duration * (max_part_bytes / part_size) * 0.9))
        sub_pattern = part[:-4] + "_sub%03d.mp4"
        sub_parts = _ffmpeg_segment(part, sub_pattern, tighter_secs)
        if sub_parts and all(os.path.getsize(p) <= max_part_bytes for p in sub_parts):
            try:
                os.remove(part)
            except OSError:
                pass
            fixed_parts.extend(sub_parts)
        else:
            for p in sub_parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            fixed_parts.append(part)  # re-split didn't help — ship it anyway

    try:
        os.remove(src_path)
    except OSError:
        pass
    return fixed_parts

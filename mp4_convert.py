"""
Any video container -> real .mp4 (so Telegram shows it as a playable video instead of a plain document).

  .ts .m2ts .mts .mkv .avi .flv .webm .wmv .mpg .mpeg .mov .m4v .3gp .vob .ogv .asf .f4v .divx  ->  .mp4

How (fastest + lossless first, heavy re-encode only when it has to be):
  1. remux          stream copy, video + audio untouched                (seconds, no quality loss)
  2. remux + aac    video copied, audio re-encoded to AAC               (when the audio can't live in mp4)
  3. re-encode      libx264 + aac, yuv420p                              (VP9/AV1/MPEG-2/WMV/10-bit... or when 1-2 fail)
Plan 1-2 are only tried when the video codec is mp4-safe (h264 / hevc / mpeg4, 8-bit); otherwise it goes straight to 3.
Every result is sanity-checked (size + duration) and if a plan produced a short/broken file the next plan is tried.

On any failure the ORIGINAL file is returned untouched, so the upload still happens exactly like before.

Speed (the re-encode is what takes minutes - typical for anime: 10-bit H.264/HEVC + several audio tracks):
  * re-encodes at most MP4_MAX_HEIGHT (default 720p; a 1080p source has 2.25x the pixels), preset superfast, ONE audio track
    (the first - Telegram plays only that one anyway) -> about 2.5x faster than the old veryfast/1080p/all-tracks setting
  * status shows the ETA; MP4_COPY_10BIT=1 skips re-encoding 10-bit HEVC altogether (instant, see below)

Env:  MP4_REENCODE=0          never re-encode (remux only - for weak servers)
      MP4_CONVERT_TIMEOUT=N   seconds per conversion before giving up (default 7200)
      MP4_PRESET=superfast    x264 preset: ultrafast (fastest, biggest file) ... veryfast ... medium (slowest, smallest)
      MP4_CRF=24              x264 quality (lower = better/bigger; 23-28 is the usual range)
      MP4_MAX_HEIGHT=720      downscale taller videos while re-encoding; 0 = keep the source resolution
      MP4_COPY_10BIT=1        10-bit HEVC (x265 anime) is stream-copied into the mp4 (tag hvc1) instead of re-encoded to
                              8-bit H.264: instant and lossless, but plays only where the phone can decode 10-bit HEVC
                              (most phones from ~2018 on; some old Telegram/desktop players can't). Default 0.

FAST MODE (ensure_mp4(..., fast=True) - used for anime links: AnimaHD, Toonstream, AnimeDekho, ...):
  Anime releases are mostly 10-bit (HEVC Main10 or H.264 Hi10P) - the one case a plain remux can't handle, so those
  episodes used to go through a full re-encode (minutes) while a normal 8-bit movie only took seconds. Fast mode:
    * 10-bit HEVC is stream-copied (instant)      MP4_FAST_COPY_10BIT=0 turns that off
    * everything else that must be re-encoded uses  MP4_FAST_PRESET (default ultrafast), MP4_FAST_CRF (27) and
      MP4_FAST_MAX_HEIGHT (default 720; 480 is faster still)  - roughly 2x faster than the normal re-encode
"""

import asyncio
import html
import json
import logging
import os
import shutil
import time

logger = logging.getLogger("sonyliv_bot")

CONVERT_EXTS = (
    ".ts", ".m2ts", ".mts", ".mkv", ".avi", ".flv", ".webm", ".wmv", ".mpg", ".mpeg",
    ".mov", ".m4v", ".3gp", ".vob", ".ogv", ".asf", ".f4v", ".divx",
)
MP4_REENCODE = os.environ.get("MP4_REENCODE", "1") != "0"
CONVERT_TIMEOUT = int(os.environ.get("MP4_CONVERT_TIMEOUT", "7200"))
MP4_PRESET = (os.environ.get("MP4_PRESET", "superfast").strip() or "superfast")
MP4_CRF = (os.environ.get("MP4_CRF", "24").strip() or "24")
try:
    MP4_MAX_HEIGHT = max(0, int(os.environ.get("MP4_MAX_HEIGHT", "720")))
except ValueError:
    MP4_MAX_HEIGHT = 720
MP4_COPY_10BIT = os.environ.get("MP4_COPY_10BIT", "0") == "1"
MP4_FAST_COPY_10BIT = os.environ.get("MP4_FAST_COPY_10BIT", "1") != "0"
MP4_FAST_PRESET = (os.environ.get("MP4_FAST_PRESET", "ultrafast").strip() or "ultrafast")
MP4_FAST_CRF = (os.environ.get("MP4_FAST_CRF", "27").strip() or "27")
try:
    MP4_FAST_MAX_HEIGHT = max(0, int(os.environ.get("MP4_FAST_MAX_HEIGHT", "720")))
except ValueError:
    MP4_FAST_MAX_HEIGHT = 720
_SAFE_VIDEO = {"h264", "hevc", "mpeg4"}
_SAFE_AUDIO = {"aac", "mp3"}
_SAFE_PIX = {None, "yuv420p", "yuvj420p"}


def needs_convert(path: str) -> bool:
    return (path or "").lower().endswith(CONVERT_EXTS) and bool(shutil.which("ffmpeg"))


async def _probe(path: str) -> dict:
    """-> {"dur": float, "vcodec": str|None, "pix": str|None, "acodec": str|None, "has_video": bool, "height": int}"""
    info = {"dur": 0.0, "vcodec": None, "pix": None, "acodec": None, "has_video": True, "height": 0}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,height:format=duration",
            "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads(out or b"{}")
        try:
            info["dur"] = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            pass
        streams = data.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        info["has_video"] = v is not None
        if v:
            info["vcodec"], info["pix"] = v.get("codec_name"), v.get("pix_fmt")
            try:
                info["height"] = int(v.get("height") or 0)
            except (TypeError, ValueError):
                pass
        if a:
            info["acodec"] = a.get("codec_name")
    except Exception as e:
        logger.warning(f"[mp4] ffprobe failed for {path}: {e}")
    return info


def _plans(info: dict, fast: bool = False) -> list:
    """-> [(plan name, ffmpeg codec args, audio -map)]. Cheapest plan first; the copy plans keep every audio track, the
    plans that re-encode audio keep only the first one (less work, smaller file, and it is the one Telegram plays)."""
    vc, ac, pix = info.get("vcodec"), info.get("acodec"), info.get("pix")
    probe_failed = vc is None and info.get("has_video", True)
    copy_10bit = MP4_COPY_10BIT or (fast and MP4_FAST_COPY_10BIT)
    video_copy_ok = probe_failed or (vc in _SAFE_VIDEO and pix in _SAFE_PIX) or (copy_10bit and vc == "hevc")
    plans = []
    if video_copy_ok:
        vtag = ["-tag:v", "hvc1"] if vc == "hevc" else []
        if ac is None or ac in _SAFE_AUDIO or probe_failed:
            bsf = ["-bsf:a", "aac_adtstoasc"] if ac == "aac" else []
            plans.append(("remux", ["-c", "copy", *vtag, *bsf], "0:a?"))
        plans.append(("remux + aac", ["-c:v", "copy", *vtag, "-c:a", "aac", "-b:a", "128k"], "0:a:0?"))
    if MP4_REENCODE:
        h = info.get("height") or 0
        max_h, preset, crf = ((MP4_FAST_MAX_HEIGHT, MP4_FAST_PRESET, MP4_FAST_CRF) if fast
                              else (MP4_MAX_HEIGHT, MP4_PRESET, MP4_CRF))
        down = bool(max_h and h > max_h)
        vf = (f"scale=-2:{max_h}:flags=fast_bilinear," if down else "") + "format=yuv420p"
        label = (f"fast re-encode {max_h}p" if fast else f"re-encode {max_h}p") if down else ("fast re-encode" if fast else "re-encode")
        plans.append((label, ["-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", crf, "-threads", "0",
                              "-c:a", "aac", "-b:a", "128k", "-ac", "2"], "0:a:0?"))
    return plans


def _rm(fp: str):
    try:
        os.remove(fp)
    except OSError:
        pass


async def ensure_mp4(path: str, on_progress=None, fast: bool = False) -> str:
    """Convert `path` to .mp4 if it is another video container. Returns the new path (original removed), or the
    original path if no conversion was needed / possible. on_progress(frac 0..1, plan_name) is awaited, if given.
    asyncio cancellation kills ffmpeg and removes the partial output."""
    if not needs_convert(path) or not os.path.exists(path):
        return path
    info = await _probe(path)
    if not info["has_video"]:
        return path                                    # audio-only file: leave it alone
    base = os.path.splitext(path)[0]
    final, tmp = base + ".mp4", base + ".converting.mp4"
    insize = os.path.getsize(path)

    for plan_name, args, amap in _plans(info, fast):
        _rm(tmp)
        if on_progress:
            await on_progress(0.0, plan_name)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats",
               "-fflags", "+genpts", "-i", path, "-map", "0:v:0", "-map", amap, "-sn", "-dn",
               "-max_muxing_queue_size", "1024", *args, "-movflags", "+faststart", "-f", "mp4", tmp]
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            logger.warning(f"[mp4] ffmpeg spawn failed: {e}")
            return path
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
                except asyncio.TimeoutError:
                    line = None
                if line == b"":
                    break
                if time.time() - t0 > CONVERT_TIMEOUT:
                    logger.warning(f"[mp4] {plan_name} timed out after {CONVERT_TIMEOUT}s for {path}")
                    proc.kill()
                    break
                if line and on_progress:
                    txt = line.decode(errors="ignore").strip()
                    if txt.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            sec = int(txt.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        if info["dur"] > 0:
                            await on_progress(min(sec / info["dur"], 0.99), plan_name)
                        elif insize and os.path.exists(tmp):          # unknown duration: output grows ~ input size
                            await on_progress(min(os.path.getsize(tmp) / insize, 0.99), plan_name)
            await proc.wait()
        except BaseException:                          # task cancelled / error -> stop ffmpeg, drop partial output
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            err_task.cancel()
            _rm(tmp)
            raise
        try:
            err = (await err_task).decode(errors="ignore").strip().splitlines()
        except Exception:
            err = []

        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 10 * 1024:
            out_info = await _probe(tmp)
            if info["dur"] > 0 and out_info["dur"] > 0 and out_info["dur"] < info["dur"] * 0.85:
                logger.warning(f"[mp4] {plan_name} gave a short file ({out_info['dur']:.0f}s of {info['dur']:.0f}s) - trying next plan")
                _rm(tmp)
                continue
            try:
                os.replace(tmp, final)
            except OSError as e:
                logger.warning(f"[mp4] couldn't move result into place: {e}")
                _rm(tmp)
                return path
            if final != path:
                _rm(path)
            if on_progress:
                await on_progress(1.0, plan_name)
            logger.info(f"[mp4] {os.path.basename(path)} -> mp4 via {plan_name} in {time.time() - t0:.0f}s")
            return final
        logger.warning(f"[mp4] {plan_name} failed (exit {proc.returncode}): {(err[-1] if err else '')[:200]}")
        _rm(tmp)
    logger.warning(f"[mp4] could not convert {path} - sending the original")
    return path


def status_progress(status, name: str, sc=None, interval: float = 4.0):
    """on_progress callback that edits a Telegram status message (throttled; edit errors are ignored)."""
    from pyrogram.enums import ParseMode
    last = [0.0]
    started = [None]       # when the current plan began (frac == 0.0) -> ETA

    def _fmt(sec: float) -> str:
        sec = int(max(0, sec))
        return f"{sec // 3600}h {sec % 3600 // 60}m" if sec >= 3600 else f"{sec // 60}m {sec % 60:02d}s"

    async def cb(frac: float, plan: str):
        now = time.time()
        if frac == 0.0:
            started[0] = now
        if frac not in (0.0, 1.0) and now - last[0] < interval:
            return
        last[0] = now
        pct = max(0.0, min(frac * 100, 100.0))
        filled = min(10, int(10 * pct / 100))
        bar = "⬢" * filled + "⬡" * (10 - filled)             # same look as the download progress bar in main.py
        eta = ""
        if started[0] and 0.02 <= frac < 1.0:
            eta = f"  •  ⏱ {_fmt((now - started[0]) / frac * (1 - frac))} left"
        txt = (f"<b>🔄 Converting to MP4 ({plan})...</b>\n"
               f"[{bar}] {pct:.0f}%{eta}\n"
               f"<code>{html.escape((name or '')[:60])}</code>")
        try:
            await status.edit_text(sc(txt) if sc else txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return cb

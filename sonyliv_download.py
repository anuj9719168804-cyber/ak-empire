"""SonyLIV download engine for the bot (ported from sonyliv_gui.py, async).

Pipeline (same as the desktop GUI):
  1. MPD fetch with DRM headers (x-did + x-playback-session-id)  -> quality list (video / audio / subs)
  2. Widevine key via pywidevine + device.wvd (PSSH from MPD, or synthetic PSSH from the API's drm_video_kid)
  3. N_m3u8DL-RE with --key and the same DRM headers
  4. subtitles from the API (plain VTT) + mkvmerge into one .mkv
  5. optional split with mkvmerge for Telegram's size limit

Env (all optional):
  N_M3U8DL_EXE=N_m3u8DL-RE   MKVMERGE_EXE=mkvmerge   MP4DECRYPT_EXE=mp4decrypt
  WVD_FILE=/path/device.wvd  DL_THREADS=32
"""
import asyncio
import base64
import glob
import logging
import os
import re
import struct
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional

import aiohttp

import sonyliv_api as sony

log = logging.getLogger("sonyliv_download")

N_M3U8DL_EXE = os.getenv("N_M3U8DL_EXE", "N_m3u8DL-RE")
MKVMERGE_EXE = os.getenv("MKVMERGE_EXE", "mkvmerge")
MP4DECRYPT_EXE = os.getenv("MP4DECRYPT_EXE", "mp4decrypt")
WVD_FILE = os.getenv("WVD_FILE", "")
DL_THREADS = int(os.getenv("DL_THREADS", "32") or 32)

_UA = sony._UA
_WV_UUID = "edef8ba979d64acea3c827dcd51d21ed"


class DownloadError(Exception):
    pass


# --------------------------------------------------------------------------- MPD
def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_mpd_streams(mpd_xml: str) -> Dict[str, list]:
    """MPD text -> {"video": [...], "audio": [...], "subs": [...]} (video/audio sorted by bitrate desc)."""
    video, audio, subs = [], [], []
    seen_v, seen_a, seen_s = set(), set(), set()
    try:
        root = ET.fromstring(mpd_xml)
    except Exception as e:
        log.error(f"MPD parse error: {e}")
        return {"video": [], "audio": [], "subs": []}

    for adapt in root.iter():
        if _strip_ns(adapt.tag) != "AdaptationSet":
            continue
        mime = adapt.get("mimeType", "") or adapt.get("contentType", "")
        lang = adapt.get("lang", "") or ""
        for rep in adapt:
            if _strip_ns(rep.tag) != "Representation":
                continue
            rid = rep.get("id", "")
            try:
                bw = int(rep.get("bandwidth", 0)) // 1000
            except ValueError:
                bw = 0
            width, height = rep.get("width", ""), rep.get("height", "")
            if "text" in mime or "ttml" in mime or "vtt" in mime or ("application/mp4" in mime and lang):
                if rid in seen_s:
                    continue
                seen_s.add(rid)
                subs.append({"id": rid, "lang": lang or "und", "label": f"{(lang or 'und').upper()} subtitle"})
            elif "video" in mime or (width and "audio" not in mime):
                if rid in seen_v:
                    continue
                seen_v.add(rid)
                try:
                    h, w = int(height), int(width)
                except ValueError:
                    h = w = 0
                video.append({"id": rid, "height": h, "width": w, "kbps": bw,
                              "label": f"{h}p · {bw} Kbps"})
            elif "audio" in mime:
                if rid in seen_a:
                    continue
                seen_a.add(rid)
                lg = lang or rep.get("lang", "") or "und"
                audio.append({"id": rid, "lang": lg, "kbps": bw, "label": f"{lg.upper()} · {bw} Kbps"})

    video.sort(key=lambda x: -x["kbps"])
    audio.sort(key=lambda x: -x["kbps"])
    seen_l = set()
    subs = [s for s in subs if not (s["lang"] in seen_l or seen_l.add(s["lang"]))]
    return {"video": video, "audio": audio, "subs": subs}


def mpd_headers(playable: "sony.SonyPlayable", token: Optional[str]) -> Dict[str, str]:
    """DRM gateway: x-did + x-playback-session-id only. Other CDNs: plain headers (+ Bearer if we have one)."""
    url = playable.mpd_url or playable.stream_url or ""
    if sony.is_drm_domain(url) and playable.drm_headers:
        return dict(playable.drm_headers)
    h = {"User-Agent": _UA, "Referer": "https://www.sonyliv.com/", "Origin": "https://www.sonyliv.com", "Accept": "*/*"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch_mpd(session: aiohttp.ClientSession, playable: "sony.SonyPlayable", token: Optional[str] = None) -> str:
    url = playable.mpd_url
    if not url:
        raise DownloadError("No MPD URL for this title")
    try:
        async with session.get(url, headers=mpd_headers(playable, token), timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise DownloadError(f"MPD fetch HTTP {r.status} (x-did / device cookies missing or expired?)")
            return await r.text()
    except aiohttp.ClientError as e:
        raise DownloadError(f"MPD fetch failed: {e}")


async def get_streams(session: aiohttp.ClientSession, playable: "sony.SonyPlayable",
                      token: Optional[str] = None) -> Dict[str, list]:
    """Quality list for the picker. Adds the API's plain-VTT subtitles. Also stores the raw MPD on the playable."""
    xml = await fetch_mpd(session, playable, token)
    playable._mpd_xml = xml  # type: ignore[attr-defined]
    streams = parse_mpd_streams(xml)
    for s in playable.subtitles:
        if not any(x["lang"].lower() == s["lang"].lower() for x in streams["subs"]):
            streams["subs"].append({"id": s["lang"], "lang": s["lang"], "label": f"{s['name']} (API)", "url": s["url"]})
    return streams


# --------------------------------------------------------------------------- Widevine
def _synthetic_pssh(kid_hex: str) -> str:
    kid = bytes.fromhex(kid_hex)
    proto = b"\x12" + bytes([len(kid)]) + kid
    body = b"\x00\x00\x00\x00" + bytes.fromhex(_WV_UUID) + struct.pack(">I", len(proto)) + proto
    return base64.b64encode(struct.pack(">I", 8 + len(body)) + b"pssh" + body).decode()


def _pssh_from_mpd(mpd_xml: str) -> tuple:
    pssh, la = "", ""
    try:
        tree = ET.fromstring(mpd_xml)
    except Exception:
        return "", ""
    for elem in tree.iter():
        if "contentprotection" not in elem.tag.lower():
            continue
        scheme = elem.get("schemeIdUri", "").lower()
        if "edef8ba9" in scheme:
            for ch in elem:
                t = ch.tag.lower()
                if "pssh" in t and ch.text:
                    pssh = ch.text.strip()
                if "laurl" in t or "la_url" in t:
                    la = (ch.text or "").strip() or elem.get("laUrl", "")
            la = la or elem.get("laUrl", elem.get("la_url", ""))
    return pssh, la


def _fetch_key_sync(mpd_xml: str, license_url: str, kid: str, token: Optional[str], wvd: str) -> List[str]:
    """Blocking pywidevine flow. Returns ['kid:key', ...]."""
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH

    pssh_b64, la_mpd = _pssh_from_mpd(mpd_xml)
    lic = license_url or la_mpd or "https://wv-sony.service.expressplay.com/hms/wv/rights/"
    if not pssh_b64:
        if not kid:
            raise DownloadError("No Widevine PSSH in MPD and no KID from API")
        pssh_b64 = _synthetic_pssh(kid)

    cdm = Cdm.from_device(Device.load(wvd))
    sid = cdm.open()
    try:
        challenge = cdm.get_license_challenge(sid, PSSH(pssh_b64))
        h = {"User-Agent": _UA, "Content-Type": "application/octet-stream", "Referer": "https://www.sonyliv.com/"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(lic, data=challenge, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = r.read()
        cdm.parse_license(sid, resp)
        return [f"{k.kid.hex}:{k.key.hex()}" for k in cdm.get_keys(sid) if k.type == "CONTENT"]
    finally:
        cdm.close(sid)


async def fetch_keys(playable: "sony.SonyPlayable", token: Optional[str] = None) -> List[str]:
    if not WVD_FILE or not os.path.exists(WVD_FILE):
        raise DownloadError("DRM title but WVD_FILE (device.wvd) is not set / not found on the server")
    xml = getattr(playable, "_mpd_xml", "")
    try:
        keys = await asyncio.to_thread(_fetch_key_sync, xml, playable.license_url or "", playable.kid, token, WVD_FILE)
    except ImportError:
        raise DownloadError("pywidevine is not installed (pip install pywidevine)")
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"Widevine key fetch failed: {e}")
    if not keys:
        raise DownloadError("License server returned no CONTENT keys")
    return keys


# --------------------------------------------------------------------------- download
_PROG = re.compile(r"(\d+)/(\d+)\s+([\d.]+)%.*?(\d+\.\d+[KMG]?B(?:ps)?)\s+(\d{2}:\d{2}:\d{2})")


async def _run(cmd: List[str], on_line: Optional[Callable[[str], None]] = None) -> int:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]+", buf)
        buf = parts.pop()
        for p in parts:
            if p and on_line:
                on_line(p.decode("utf-8", "replace"))
    if buf and on_line:
        on_line(buf.decode("utf-8", "replace"))
    return await proc.wait()


async def _fetch_url(session: aiohttp.ClientSession, url: str, dest: str) -> bool:
    try:
        async with session.get(url, headers={"User-Agent": _UA, "Referer": "https://www.sonyliv.com/"},
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                return False
            with open(dest, "wb") as f:
                f.write(await r.read())
        return True
    except Exception:
        return False


def safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", title).strip()[:120] or "SonyLIV_Video"


async def download(session: aiohttp.ClientSession, playable: "sony.SonyPlayable", out_dir: str, *,
                   video_id: Optional[str] = None, audio_langs: Optional[List[str]] = None,
                   sub_langs: Optional[List[str]] = None, token: Optional[str] = None,
                   progress: Optional[Callable[[float, str, str], None]] = None) -> str:
    """Download + decrypt + mux. progress(pct, speed, eta). Returns the final .mkv path. Raises DownloadError."""
    os.makedirs(out_dir, exist_ok=True)
    media = playable.mpd_url or playable.stream_url
    if not media:
        raise DownloadError("No stream URL")
    name = safe_name(playable.title)
    cmd = [N_M3U8DL_EXE, media, "--save-dir", out_dir, "--save-name", name,
           "--binary-merge", "--concurrent-download", "--thread-count", str(DL_THREADS), "--log-level", "INFO"]

    cmd += ["--select-video", f"id={video_id}" if video_id and video_id != "best" else "best"]
    if audio_langs:
        codes = [c.strip().lower() for c in audio_langs if c.strip()]
        cmd += ["--select-audio", f"lang={codes[0]}:for=best" if len(codes) == 1
                else f"lang=({'|'.join(codes)}):for=best{len(codes)}"]
    else:
        cmd += ["--select-audio", "best"]
    if sub_langs:
        codes = [c.strip().lower() for c in sub_langs if c.strip()]
        if "all" in codes:
            cmd += ["--select-subtitle", "all"]
        elif codes:
            cmd += ["--select-subtitle", f"lang=({'|'.join(codes)})" if len(codes) > 1 else f"lang={codes[0]}"]

    # same header strategy as the GUI: DRM gateway -> x-did + x-playback-session-id only
    if playable.is_drm and playable.drm_headers:
        cmd += sony.n_m3u8dl_header_args(playable.drm_headers)
    else:
        cmd += ["--header", f"User-Agent:{_UA}", "--header", "Referer:https://www.sonyliv.com/",
                "--header", "Origin:https://www.sonyliv.com"]
        if token:
            cmd += ["--header", f"Authorization:Bearer {token}"]

    if playable.is_drm:
        if not getattr(playable, "_mpd_xml", ""):
            playable._mpd_xml = await fetch_mpd(session, playable, token)  # type: ignore[attr-defined]
        for k in await fetch_keys(playable, token):
            cmd += ["--key", k]
        cmd += ["--decryption-binary-path", MP4DECRYPT_EXE]

    last = {}

    def on_line(line: str):
        m = _PROG.search(line)
        if m and progress:
            _, _, pct, speed, eta = m.groups()
            last[line.split()[0]] = (float(pct), eta, speed)
            slow = min(last.values(), key=lambda x: x[0])  # slowest track limits the ETA
            progress(slow[0], slow[2], slow[1])

    rc = await _run(cmd, on_line)
    if rc != 0:
        raise DownloadError(f"N_m3u8DL-RE failed (exit {rc})")

    # API subtitles (plain VTT, not in MPD)
    api_subs = []
    for s in playable.subtitles:
        if sub_langs and "all" not in [c.lower() for c in sub_langs] and s["lang"] not in [c.lower() for c in sub_langs]:
            continue
        if not sub_langs:
            continue
        dest = os.path.join(out_dir, f"{name}.{s['lang']}{os.path.splitext(s['url'])[-1] or '.vtt'}")
        if await _fetch_url(session, s["url"], dest):
            api_subs.append(dest)

    mkv = os.path.join(out_dir, f"{name}.mkv")
    sub_ext, skip_ext = {".srt", ".ass", ".vtt"}, {".mkv", ".mpd", ".m3u8", ".tmp", ".json"}
    tracks, subs = [], []
    for f in sorted(glob.glob(os.path.join(out_dir, f"{glob.escape(name)}*"))):
        if not os.path.isfile(f) or f == mkv:
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in skip_ext:
            continue
        (subs if ext in sub_ext else tracks).append(f)
    subs += [s for s in api_subs if s not in subs and os.path.exists(s)]
    if not tracks:
        raise DownloadError("N_m3u8DL-RE finished but produced no tracks to mux")

    rc = await _run([MKVMERGE_EXE, "-o", mkv] + tracks + subs)
    if rc not in (0, 1):  # 1 = warnings only
        raise DownloadError(f"mkvmerge failed (exit {rc})")
    for f in tracks + subs + glob.glob(os.path.join(out_dir, "*_dec")):
        try:
            os.remove(f)
        except OSError:
            pass
    return mkv


async def split_for_telegram(path: str, max_bytes: int) -> List[str]:
    """Split with mkvmerge when the file is over max_bytes. Returns the list of part paths."""
    if os.path.getsize(path) <= max_bytes:
        return [path]
    base, _ = os.path.splitext(path)
    rc = await _run([MKVMERGE_EXE, "-o", f"{base}.part.mkv", "--split", f"size:{max_bytes // (1024 * 1024)}M", path])
    parts = sorted(glob.glob(f"{glob.escape(base)}.part-*.mkv"))
    if rc not in (0, 1) or not parts:
        raise DownloadError("Could not split the file for Telegram")
    os.remove(path)
    return parts

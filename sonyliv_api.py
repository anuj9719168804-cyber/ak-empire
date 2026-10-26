"""SonyLIV API client with cookie-based auth fallback (async/aiohttp).

Flow:
  1. Login: /login → phone OTP → CREATEOTP-V2 → CONFIRMOTP-V2 → bearer token
  2. Cookies: /setcookies → store cookies per user → use for API requests
  3. Search: token/cookies + query → SEARCH API → list results
  4. Resolve: token/cookies + content_id → (bundle→playable id) → VIDEOURL (cluster loop) → mpd / hls / license / kid
     using the SAME device headers as the desktop GUI (device_id, session_id, td_client_hints, x-via-device ...)
  5. Stream: M3U8 link playable in Telegram via HLS proxy
  6. Download: N_m3u8DL-RE (DRM) or yt-dlp (non-DRM)
"""
import asyncio
import base64
import hashlib
import json
import re
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("sonyliv_api")

# ---- constants ----
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
_HEADERS = {
    "content-type": "application/json",
    "user-agent": _UA,
    "origin": "https://www.sonyliv.com",
    "referer": "https://www.sonyliv.com/",
}


class SonyLIVError(Exception):
    pass


@dataclass
class SonyContent:
    """Represents a SonyLIV content item."""
    id: str
    title: str
    description: str = ""
    duration: int = 0  # seconds
    genre: str = ""
    poster_url: Optional[str] = None
    is_premium: bool = False
    resolved: float = field(default_factory=time.time)


@dataclass
class SonyPlayable:
    """Resolved playable content with stream/download URLs."""
    content_id: str
    title: str
    duration: int = 0
    stream_url: Optional[str] = None  # M3U8 for HLS
    download_url: Optional[str] = None  # MPD URL (DRM) or direct video URL
    mpd_url: Optional[str] = None  # For N_m3u8DL-RE (DRM)
    license_url: Optional[str] = None  # Widevine license URL
    is_drm: bool = False  # True if DRM-protected
    kid: str = ""  # drm_video_kid (hex) from the API, if any
    subtitles: List[Dict[str, str]] = field(default_factory=list)  # [{lang, name, url}] unencrypted VTT
    drm_headers: Dict[str, str] = field(default_factory=dict)  # headers for MPD fetch / N_m3u8DL-RE (x-did, x-playback-session-id ...)
    device_id: str = ""
    contact_id: str = ""
    cluster: str = ""


# ---- helpers ----
def _now_iso() -> str:
    """ISO 8601 timestamp with milliseconds (required by SonyLIV API)."""
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond//1000:03d}Z"


def _decode_jwt(token: str) -> dict:
    """Decode JWT payload without verification (trust in SSL)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # add padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        log.error(f"JWT decode failed: {e}")
        return {}


def _is_token_expired(token: str) -> bool:
    """Check if JWT token is expired."""
    payload = _decode_jwt(token)
    exp = payload.get("exp", 0)
    return exp <= time.time()


# ---- cookie parsing (like fbot FapHouse) ----
def parse_cookie_string(raw: str) -> List[Dict[str, Optional[str]]]:
    """'a=b; c=d' header text OR a Netscape cookies.txt export -> [{"domain": str|None, "name", "value"}].
    Netscape lines are kept only if their domain mentions sonyliv."""
    out = []
    raw = (raw or "").strip()
    if not raw:
        return out
    
    # Check if it's Netscape format (tab-separated)
    if "\t" in raw or raw.startswith("# Netscape") or raw.startswith("# HTTP Cookie"):
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 7 and "sonyliv" in f[0].lower() and f[5].strip():
                out.append({
                    "domain": f[0].lstrip(".").lower(), 
                    "name": f[5].strip(), 
                    "value": f[6].strip()
                })
        return out
    
    # Header format: a=b; c=d
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out.append({
                    "domain": None, 
                    "name": k.strip(), 
                    "value": v.strip()
                })
    return out


def validate_cookies(cookies: List[Dict[str, Optional[str]]]) -> bool:
    """Check if cookies list is valid."""
    return isinstance(cookies, list) and len(cookies) > 0 and all(
        isinstance(c, dict) and "name" in c and "value" in c 
        for c in cookies
    )


# ---- API calls (async) ----
async def send_otp(session: aiohttp.ClientSession, mobile: str) -> dict:
    """POST CREATEOTP-V2 → initiate OTP flow."""
    url = "https://apiv2.sonyliv.com/AGL/1.6/A/ENG/WEB/IN/MH/CREATEOTP-V2"
    payload = {
        "mobileNumber": mobile,
        "channelPartnerID": "MSMIND",
        "country": "IN",
        "timestamp": _now_iso(),
        "otpSize": 4,
        "loginType": "REGISTERORSIGNIN",
        "isMobileMandatory": True,
    }
    try:
        async with session.post(url, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()
    except Exception as e:
        log.error(f"send_otp failed: {e}")
        return {"error": str(e)}


async def verify_otp(session: aiohttp.ClientSession, mobile: str, otp: str) -> dict:
    """POST CONFIRMOTP-V2 → verify OTP, get accessToken."""
    url = "https://apiv2.sonyliv.com/AGL/2.4/A/ENG/WEB/IN/MH/CONFIRMOTP-V2"
    payload = {
        "channelPartnerID": "MSMIND",
        "mobileNumber": mobile,
        "country": "IN",
        "otp": otp,
        "dmaId": "IN",
        "ageConfirmation": True,
        "timestamp": _now_iso(),
        "isMobileMandatory": True,
        "deviceDetails": {
            "deviceName": "NA",
            "deviceType": "webClient",
            "gaUserId": f"GA1.2.{abs(hash(mobile)) % 2000000000}.{int(time.time())}",
            "modelNo": "Chrome Browser",
            "serialNo": f"{uuid.uuid4().hex}-{int(time.time()*1000)}",
        },
        "address": {"state": "MH"},
        "checkDeviceLimit": True,
    }
    try:
        async with session.post(url, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()
    except Exception as e:
        log.error(f"verify_otp failed: {e}")
        return {"error": str(e)}


async def search_with_cookies(session: aiohttp.ClientSession, cookies: List[Dict[str, Optional[str]]], query: str, page_size: int = 10) -> dict:
    """Search using cookies instead of bearer token."""
    url = "https://apiv2.sonyliv.com/AGL/1.0/A/ENG/WEB/IN/MH/SEARCH"
    params = {"searchString": query, "pageSize": page_size}
    
    headers = dict(_HEADERS)
    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")])
    if cookie_header:
        headers["cookie"] = cookie_header
    
    try:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()
    except Exception as e:
        log.error(f"search_with_cookies failed: {e}")
        return {"error": str(e), "results": []}


async def search(session: aiohttp.ClientSession, token: str, query: str, page_size: int = 10) -> dict:
    """GET SEARCH → search for content by query."""
    if _is_token_expired(token):
        return {"error": "token_expired", "results": []}
    
    headers = dict(_HEADERS, authorization=f"Bearer {token}")
    url = "https://apiv2.sonyliv.com/AGL/1.0/A/ENG/WEB/IN/MH/SEARCH"
    params = {"searchString": query, "pageSize": page_size}
    
    try:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()
    except Exception as e:
        log.error(f"search failed: {e}")
        return {"error": str(e), "results": []}


# =====================================================================================
#  DEVICE IDENTITY / HEADERS  (ported from sonyliv_gui.py so the bot looks like the GUI)
# =====================================================================================
APP_VERSION = "3.6.83"

# Region clusters. The API only answers on the cluster that matches the account's region,
# so we try them one by one (preferred first) and remember the one that worked.
_PREFERRED_CLUSTERS = ["MH", "KL", "TN", "TG", "AP", "KA", "DL"]
ALL_CLUSTERS = [
    "AP", "AR", "AS", "BR", "CH", "DL", "GA", "GJ", "HR", "HP",
    "JH", "JK", "KA", "KL", "LD", "MH", "ML", "MN", "MP", "MZ",
    "NL", "OD", "PB", "PY", "RJ", "SK", "TG", "TN", "TR", "UK",
    "UP", "WB",
]
_CLUSTER_CACHE: Dict[str, str] = {}  # user_key -> last working cluster


def cluster_order(preferred: Optional[str] = None) -> List[str]:
    """preferred (if any) -> common clusters -> all other clusters, no duplicates."""
    seen, out = set(), []
    for c in ([preferred] if preferred else []) + _PREFERRED_CLUSTERS + ALL_CLUSTERS:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def generate_device_id() -> str:
    """device_id = md5(uuid4) + '-' + timestamp_ms  (same shape the web player uses)."""
    return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest() + f"-{int(time.time() * 1000)}"


def generate_session_id() -> str:
    """session_id header = md5(uuid4) + '-' + timestamp_ms (different value from device_id)."""
    return generate_device_id()


def generate_playback_session_id() -> str:
    """x-playback-session-id for drm.sonyliv.com = 'slv' + md5(random) + timestamp_ms."""
    return "slv" + hashlib.md5(uuid.uuid4().bytes).hexdigest() + str(int(time.time() * 1000))


def cookies_to_dict(cookies: Optional[List[Dict[str, Optional[str]]]]) -> Dict[str, str]:
    """[{name, value}, ...] -> {name: value} (later duplicates win)."""
    out: Dict[str, str] = {}
    for c in cookies or []:
        n, v = c.get("name"), c.get("value")
        if n and v is not None:
            out[n] = v
    return out


def cookie_header(cookies: Optional[List[Dict[str, Optional[str]]]]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies_to_dict(cookies).items())


def get_device_cookies(cookies: Optional[List[Dict[str, Optional[str]]]]) -> Dict[str, str]:
    """Pull the two device-tracking cookies the GUI relies on: sl_ppid and sl_device_token."""
    d = cookies_to_dict(cookies)
    return {k: d[k] for k in ("sl_ppid", "sl_device_token") if d.get(k)}


def contact_id_from_token(token: Optional[str]) -> str:
    """contactId lives inside the bearer JWT (contactID / contactId)."""
    if not token:
        return ""
    p = _decode_jwt(token)
    cid = p.get("contactID") or p.get("contactId") or p.get("cp_customer_id") or ""
    return str(cid) if cid and str(cid) != "0" else ""


def pick_device_id(token: Optional[str] = None, cookies=None, stored: Optional[str] = None) -> str:
    """Priority: stored per-user id -> sl_device_token cookie -> deviceId inside JWT -> brand-new id."""
    if stored:
        return stored
    dc = get_device_cookies(cookies)
    if dc.get("sl_device_token"):
        return dc["sl_device_token"]
    if token:
        p = _decode_jwt(token)
        did = p.get("deviceId") or p.get("device_id") or ""
        if did:
            return str(did)
    return generate_device_id()


def _td_client_hints() -> str:
    return json.dumps({
        "os_name": "Windows", "os_version": "10", "device_make": "none", "device_model": "none",
        "display_res": "1920", "viewport_res": "868", "conn_type": "4g",
        "supp_codec": "H264,AV1,AAC", "client_throughput": "16000", "td_user_agent": _UA,
        "hdr_decoder": "UNKNOWN", "audio_decoder": "STEREO", "app_version": APP_VERSION,
    })


def build_device_headers(device_id: str, session_id: Optional[str] = None, token: Optional[str] = None,
                         cookies=None, json_body: bool = False) -> Dict[str, str]:
    """Headers the web player sends to apiv2/apiv3.sonyliv.com (needed for full-resolution streams)."""
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-GB,en-IN;q=0.9,en-US;q=0.8,en;q=0.7",
        "advertiserid": device_id,
        "app_version": APP_VERSION,
        "device_id": device_id,
        "origin": "https://www.sonyliv.com",
        "referer": "https://www.sonyliv.com/",
        "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "session_id": session_id or generate_session_id(),
        "td_client_hints": _td_client_hints(),
        "user-agent": _UA,
        "x-via-device": "true",
    }
    if json_body:
        h["content-type"] = "application/json"
    ck = cookie_header(cookies)
    if ck:
        h["cookie"] = ck
    if token:
        h["authorization"] = f"Bearer {token}"
    return h


def is_drm_domain(url: str) -> bool:
    return bool(url) and any(x in url for x in ("drm.sonyliv.com", "sonydrmvod", "drmvod"))


def build_drm_headers(cookies=None, device_id: str = "") -> Dict[str, str]:
    """Headers for the DRM DASH gateway (MPD fetch + N_m3u8DL-RE --header).

    x-did = sl_device_token (preferred) or sl_ppid cookie; falls back to our device_id.
    NO cookies and NO Authorization here - Akamai bot detection answers 403 if they are sent."""
    dc = get_device_cookies(cookies)
    did = dc.get("sl_device_token") or dc.get("sl_ppid") or device_id
    h = {
        "User-Agent": _UA,
        "Referer": "https://www.sonyliv.com/",
        "Origin": "https://www.sonyliv.com",
        "Accept": "*/*",
        "x-playback-session-id": generate_playback_session_id(),
    }
    if did:
        h["x-did"] = did
    return h


def n_m3u8dl_header_args(drm_headers: Dict[str, str]) -> List[str]:
    """['--header', 'k:v', ...] ready to append to the N_m3u8DL-RE command line."""
    out: List[str] = []
    for k, v in (drm_headers or {}).items():
        out += ["--header", f"{k}:{v}"]
    return out


# =====================================================================================
#  RESOLVE:  bundle id -> playable id -> contactId -> VIDEOURL (cluster loop)
# =====================================================================================
_T = aiohttp.ClientTimeout(total=20)


async def _get_json(session: aiohttp.ClientSession, url: str, headers: dict) -> dict:
    try:
        async with session.get(url, headers=headers, timeout=_T) as r:
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log.debug(f"GET {url[:90]} failed: {e}")
        return {}


def extract_content_id(url_or_id: str) -> str:
    """Last 7-12 digit number in a SonyLIV URL (or the id itself)."""
    s = (url_or_id or "").split("?")[0].rstrip("/")
    nums = re.findall(r"(?<!\d)(\d{7,12})(?!\d)", s)
    return nums[-1] if nums else (url_or_id or "").strip()


async def fetch_contact_id(session: aiohttp.ClientSession, headers: dict, preferred: Optional[str] = None) -> str:
    """GETPROFILE on each cluster until one returns a contactId. '0' if none."""
    for cl in cluster_order(preferred)[:12]:
        url = f"https://apiv2.sonyliv.com/AGL/2.9/A/ENG/WEB/IN/{cl}/GETPROFILE?channelPartnerID=MSMIND"
        data = await _get_json(session, url, headers)
        msg = (data.get("message") or data.get("errorDescription") or "")
        if "cluster" in msg.lower():
            continue
        res = data.get("resultObj") or {}
        cid = (res.get("contactId") or res.get("contactID") or res.get("userId")
               or res.get("userID") or res.get("id") or "")
        if not cid:
            prof = res.get("profile") or res.get("userProfile") or {}
            cid = prof.get("contactId") or prof.get("contactID") or prof.get("userId") or ""
        if cid and str(cid) != "0":
            return str(cid)
    return "0"


async def resolve_playable_id(session: aiohttp.ClientSession, headers: dict, bundle_id: str,
                              contact_id: str, cluster: str = "MH") -> tuple:
    """Page/bundle id (from the URL) -> real asset id. Returns (playable_id, title, duration_ms)."""
    contact_param = f"?contactId={contact_id}" if contact_id and contact_id != "0" else ""
    dur = [0]
    clusters = cluster_order(cluster)[:6]

    async def _detail_title(lookup_id: str) -> str:
        for cl in clusters[:4]:
            url = (f"https://apiv3.sonyliv.com/AGL/4.8/SR/ENG/WEB/IN/{cl}/DETAIL-V2/{lookup_id}"
                   f"?kids_safe=false&from=0&to=9&segment_id=AB_DetailPage_Disable")
            d = await _get_json(session, url, headers)
            for c in (d.get("resultObj") or {}).get("containers", []):
                meta = c.get("metadata", {})
                if meta.get("duration") and not dur[0]:
                    dur[0] = int(meta["duration"]) * 1000
                t = meta.get("title") or meta.get("episodeTitle") or meta.get("contentName") or ""
                if t:
                    return t
        return ""

    def _asset(text: str):
        m = re.search(r"sony://asset/(\d+)", text or "")
        return m.group(1) if m else None

    # 1) USER/SUGGESTION
    for cl in clusters:
        url = f"https://apiv2.sonyliv.com/AGL/3.4/SR/ENG/WEB/IN/{cl}/USER/SUGGESTION/{bundle_id}{contact_param}"
        data = await _get_json(session, url, headers)
        sg = (data.get("resultObj") or {}).get("suggestions", {}) or {}
        if sg.get("content_duration") and not dur[0]:
            dur[0] = int(sg["content_duration"])
        for cta in sg.get("ctas", []):
            pid = _asset(cta.get("button_cta") or cta.get("button_sub_cta") or "")
            if pid and pid != bundle_id:
                t = cta.get("overlay_content_title") or cta.get("button_title") or ""
                return pid, (t or await _detail_title(pid) or await _detail_title(bundle_id)), dur[0]
        pid = _asset(sg.get("button_cta", ""))
        if pid and pid != bundle_id:
            return pid, (await _detail_title(pid) or await _detail_title(bundle_id)), dur[0]

    # 2) DETAIL-V2 containers
    for cl in clusters[:4]:
        url = (f"https://apiv3.sonyliv.com/AGL/4.8/SR/ENG/WEB/IN/{cl}/DETAIL-V2/{bundle_id}"
               f"?kids_safe=false&from=0&to=9&segment_id=AB_DetailPage_Disable")
        d = await _get_json(session, url, headers)
        for c in (d.get("resultObj") or {}).get("containers", []):
            meta = c.get("metadata", {})
            if meta.get("duration") and not dur[0]:
                dur[0] = int(meta["duration"]) * 1000
            if c.get("layout") == "CONTENT_ITEM":
                cid = str(c.get("id") or c.get("contentId") or meta.get("contentId") or "")
                if cid and cid != bundle_id:
                    return cid, (meta.get("title") or meta.get("episodeTitle") or meta.get("contentName") or ""), dur[0]
            cid = str(meta.get("contentId") or meta.get("id") or "")
            if cid.isdigit() and cid != bundle_id and int(cid) > 0:
                return cid, (meta.get("title") or meta.get("episodeTitle") or ""), dur[0]

    # 3) already a playable id
    return bundle_id, "", dur[0]


def parse_stream_info(api_resp: dict) -> dict:
    """VIDEOURL response -> mpd / hls / license / kid / title / subtitles (same fields as the GUI)."""
    result = api_resp.get("resultObj", {}) or {}
    vd = result.get("videoDetails", result) or {}

    def pick(*keys):
        for k in keys:
            v = vd.get(k) or result.get(k)
            if v:
                return v
        return ""

    mpd = pick("dashUrl", "mpdUrl", "adaptiveUrl", "videoURL")
    hls = pick("hlsUrl", "streamingUrl")
    if not mpd and hls and ".mpd" in hls:
        mpd, hls = hls, ""
    la_d = result.get("LA_Details") or {}
    la = la_d.get("laURL") or la_d.get("laUrl") or ""
    if not la:
        la = pick("licenseUrl", "widevineLicenseUrl", "laUrl", "drmLicenseUrl")
        ep = pick("expressPlayToken", "widevineToken", "drmToken")
        if ep and not la:
            la = f"https://wv-sony.service.expressplay.com/hms/wv/rights/?ExpressPlayToken={ep}"
    title = pick("episodeTitle", "title", "contentName", "showName") or "SonyLIV_Video"
    media = mpd or hls
    drm = bool(is_drm_domain(media) or result.get("isEncrypted") or la)
    subs = []
    for s in result.get("subtitle", []) or []:
        u = s.get("subtitleUrl", "")
        if u:
            lang = s.get("subtitleLanguageName", "")
            subs.append({"lang": lang.lower(), "name": s.get("subtitleDisplayName", lang), "url": u})
    return {"mpd": mpd, "hls": hls, "license": la, "kid": result.get("drm_video_kid", ""),
            "title": title, "is_drm": drm, "subs": subs, "duration_ms": result.get("content_duration", 0)}


async def _fetch_video_url(session: aiohttp.ClientSession, headers: dict, content_id: str, contact_id: str,
                           device_id: str, ppid: str, cluster: str) -> dict:
    contact_param = f"?contactId={contact_id}" if contact_id and contact_id != "0" else ""
    url = (f"https://apiv2.sonyliv.com/AGL/5.0/SR/ENG/WEB/IN/{cluster}"
           f"/CONTENT/VIDEOURL/VOD/{content_id}{contact_param}")
    payload = {
        "actionType": "play", "browser": "Chrome", "deviceId": device_id, "os": "Windows",
        "platform": "web", "hasLAURLEnabled": True,
        "adsParams": {"Idtype": "uuid", "Is_lat": "0", "ppid": ppid, "preroll": "disable"},
    }
    try:
        async with session.post(url, json=payload, headers=headers, timeout=_T) as r:
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        return {"errorDescription": f"request_failed: {e}"}


_SKIP_CODES = {"404-10143", "404-10144", "404-10145"}


async def resolve(session: aiohttp.ClientSession, content_id: str, token: Optional[str] = None,
                  cookies=None, device_id: Optional[str] = None, user_key: str = "default") -> SonyPlayable:
    """Full GUI-equivalent resolve. Works with token, cookies, or both (best). Raises SonyLIVError."""
    if token and _is_token_expired(token):
        if not cookies:
            raise SonyLIVError("token_expired - use /login again or /setcookies")
        log.warning("bearer token expired - continuing with cookies only")
        token = None
    if not token and not cookies:
        raise SonyLIVError("not_logged_in - use /login or /setcookies")

    cdict = cookies_to_dict(cookies)
    device_id = pick_device_id(token, cookies, device_id)
    session_id = generate_session_id()
    ppid = cdict.get("sl_ppid") or hashlib.md5(device_id.encode()).hexdigest()
    base_h = build_device_headers(device_id, session_id, token, cookies)
    post_h = build_device_headers(device_id, session_id, token, cookies, json_body=True)
    preferred = _CLUSTER_CACHE.get(user_key)

    contact_id = contact_id_from_token(token) or "0"
    if contact_id == "0":
        contact_id = await fetch_contact_id(session, base_h, preferred)

    cid_in = extract_content_id(content_id)
    pid, title0, dur0 = await resolve_playable_id(session, base_h, cid_in, contact_id, preferred or "MH")

    resp, used, last_err, seen = None, "", "", {}
    for cl in cluster_order(preferred):
        r = await _fetch_video_url(session, post_h, pid, contact_id, device_id, ppid, cl)
        err = r.get("errorDescription") or r.get("message") or ""
        code = r.get("resultCode", "")
        has_obj = bool(r.get("resultObj"))
        if err and err not in seen:
            seen[err] = cl
        if has_obj and code != "KO":
            resp, used = r, cl
            break
        miss = ("cluster" in err.lower() or "not found" in err.lower() or err in _SKIP_CODES
                or (code == "KO" and not has_obj) or not err)
        if miss:
            last_err = err or last_err
            continue
        raise SonyLIVError(f"{err} (code={code}, cluster={cl})")  # real error: auth / geo / device limit
    if not resp:
        raise SonyLIVError(f"no cluster matched ({last_err or 'routing miss'}). Cookies/token may be expired "
                           f"or the title is not available in your region. errors={list(seen.items())[:3]}")
    _CLUSTER_CACHE[user_key] = used

    info = parse_stream_info(resp)
    media = info["mpd"] or info["hls"]
    if not media:
        raise SonyLIVError("API answered but returned no stream URL (subscription / device limit?)")
    drm_h = build_drm_headers(cookies, device_id) if (info["is_drm"] or is_drm_domain(media)) else {}
    return SonyPlayable(
        content_id=pid,
        title=title0 or info["title"],
        duration=int((info["duration_ms"] or dur0 or 0) / 1000) if (info["duration_ms"] or dur0) else 0,
        stream_url=info["hls"] or None,
        download_url=media,
        mpd_url=info["mpd"] or None,
        license_url=info["license"] or None,
        is_drm=info["is_drm"],
        kid=info["kid"],
        subtitles=info["subs"],
        drm_headers=drm_h,
        device_id=device_id,
        contact_id=contact_id,
        cluster=used,
    )


# ---- public wrappers (same names the bot already calls) ----
async def resolve_playable(session: aiohttp.ClientSession, token: str, content_id: str,
                           cookies=None, device_id: Optional[str] = None,
                           user_key: str = "default") -> Optional[SonyPlayable]:
    """Bearer-token resolve. Pass cookies too (recommended) so sl_ppid / sl_device_token reach the headers."""
    return await resolve(session, content_id, token=token, cookies=cookies, device_id=device_id, user_key=user_key)


async def resolve_playable_with_cookies(session: aiohttp.ClientSession, cookies, content_id: str,
                                        token: Optional[str] = None, device_id: Optional[str] = None,
                                        user_key: str = "default") -> Optional[SonyPlayable]:
    """Cookie resolve (token optional)."""
    return await resolve(session, content_id, token=token, cookies=cookies, device_id=device_id, user_key=user_key)


async def fetch_content_info(session: aiohttp.ClientSession, token: str, content_id: str,
                             cookies=None) -> Optional[SonyContent]:
    """Content metadata (title / duration) via the same resolve path."""
    try:
        p = await resolve(session, content_id, token=token, cookies=cookies)
    except SonyLIVError as e:
        log.error(f"fetch_content_info failed: {e}")
        return None
    return SonyContent(id=p.content_id, title=p.title, duration=p.duration, resolved=time.time())

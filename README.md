# SonyLIV Bot (cookies + DRM download)

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill API_ID, API_HASH, BOT_TOKEN, WVD_FILE ...
    python sonyliv_bot_complete_with_cookies.py

Needs on the server: N_m3u8DL-RE, mkvmerge, mp4decrypt, ffmpeg/ffprobe (MP4 conversion + thumbnails) (+ device.wvd for DRM titles).

Login: /login (OTP) or /setcookies (include sl_ppid and sl_device_token for DRM / full quality).
Flow: /search -> pick title -> Download -> pick quality -> file arrives in chat.

Files:
  sonyliv_bot_complete_with_cookies.py  bot handlers
  sonyliv_api.py                        API client (device headers, contactId, cluster loop, VIDEOURL)
  sonyliv_download.py                   MPD parse, Widevine key, N_m3u8DL-RE, mkvmerge, split
  store.py                              JSON persistence (tokens, cookies, device id, downloads)

Output: OUTPUT_FORMAT=mp4 (default) converts the decrypted mkv with mp4_convert.py (fbot/AnimeHD converter), splits big files with split_upload.py and sends as a streamable video. OUTPUT_FORMAT=mkv keeps the old document upload.

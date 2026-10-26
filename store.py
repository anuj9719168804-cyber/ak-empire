"""Enhanced persistence layer for SonyLIV bot: JSON file storage with cookies.

Stores:
  - users: {user_id: {phone, access_token, token_expire_at, cookies}}
  - downloads: {user_id: [download records]}
  - channels: list of channel IDs to mirror uploads
  - banned: list of banned user IDs
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str = "bot_data.json"):
        self.path = path
        self.data = {
            "users": {},      # {user_id: {phone, access_token, token_expire_at, cookies}}
            "downloads": {},  # {user_id: [...]}
            "channels": [],   # channel IDs for mirroring
            "banned": [],     # banned user IDs
        }
        self._dirty = False
        self._lock = asyncio.Lock()
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
        except Exception as e:
            print(f"[store] load failed: {e}")

    def _write(self, data: dict):
        """Atomic write: write to temp file then rename."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[store] write failed: {e}")

    async def flush(self):
        """Save to disk if dirty."""
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            await asyncio.to_thread(self._write, self.data.copy())

    # ---- user token management ----
    def get_user_token(self, user_id: int) -> str:
        """Get stored access token for user (expired tokens return empty string)."""
        user = self.data["users"].get(str(user_id), {})
        token = user.get("access_token", "")
        expire_at = user.get("token_expire_at", 0)
        
        if token and expire_at > time.time():
            return token
        return ""

    def set_user_token(self, user_id: int, token: str, phone: str = "", expires_in: int = 2592000):
        """Save access token (default 30 days)."""
        key = str(user_id)
        if key not in self.data["users"]:
            self.data["users"][key] = {}
        
        self.data["users"][key].update({
            "phone": phone,
            "access_token": token,
            "token_expire_at": time.time() + expires_in,
            "token_saved_at": time.time(),
        })
        self._dirty = True

    def get_user_phone(self, user_id: int) -> str:
        """Get stored phone number for user."""
        return self.data["users"].get(str(user_id), {}).get("phone", "")

    def set_user_phone(self, user_id: int, phone: str):
        """Store phone number (for convenience, OTP resend)."""
        key = str(user_id)
        if key not in self.data["users"]:
            self.data["users"][key] = {}
        self.data["users"][key]["phone"] = phone
        self._dirty = True

    def clear_user_token(self, user_id: int):
        """Clear stored token (e.g., on logout)."""
        key = str(user_id)
        if key in self.data["users"]:
            self.data["users"][key]["access_token"] = ""
            self.data["users"][key]["token_expire_at"] = 0
        self._dirty = True

    # ---- cookie management (new - like fbot FapHouse) ----
    def get_user_cookies(self, user_id: int) -> List[Dict[str, Optional[str]]]:
        """Get stored cookies for user (fallback auth if token expired)."""
        user = self.data["users"].get(str(user_id), {})
        cookies = user.get("cookies", [])
        if not isinstance(cookies, list):
            return []
        return cookies

    def set_user_cookies(self, user_id: int, cookies: List[Dict[str, Optional[str]]]):
        """Store cookies for user (cookie-based fallback auth)."""
        key = str(user_id)
        if key not in self.data["users"]:
            self.data["users"][key] = {}
        
        # Validate and clean cookies
        if not isinstance(cookies, list):
            cookies = []
        
        self.data["users"][key].update({
            "cookies": cookies,
            "cookies_saved_at": time.time(),
        })
        self._dirty = True

    def add_user_cookies(self, user_id: int, new_cookies: List[Dict[str, Optional[str]]]):
        """Add/merge new cookies with existing ones (no duplicates)."""
        existing = self.get_user_cookies(user_id)
        
        # Merge: existing + new, no duplicates by (domain, name)
        merged = {(c.get("domain"), c.get("name")): c for c in existing}
        for c in new_cookies:
            merged[(c.get("domain"), c.get("name"))] = c
        
        self.set_user_cookies(user_id, list(merged.values()))

    def get_cookie_count(self, user_id: int) -> int:
        """Get number of stored cookies for user."""
        return len(self.get_user_cookies(user_id))

    def clear_user_cookies(self, user_id: int) -> int:
        """Clear all stored cookies for user. Returns count removed."""
        key = str(user_id)
        count = 0
        if key in self.data["users"]:
            count = len(self.data["users"][key].get("cookies", []))
            self.data["users"][key]["cookies"] = []
        self._dirty = True
        return count

    # ---- per-user device id (stable md5(uuid)-timestamp_ms, sent as device_id / advertiserid) ----
    def get_user_device_id(self, user_id: int) -> str:
        return self.data["users"].get(str(user_id), {}).get("device_id", "") or ""

    def set_user_device_id(self, user_id: int, device_id: str):
        key = str(user_id)
        if key not in self.data["users"]:
            self.data["users"][key] = {}
        self.data["users"][key]["device_id"] = device_id
        self._dirty = True

    def has_user_cookies(self, user_id: int) -> bool:
        """Check if user has stored cookies."""
        return self.get_cookie_count(user_id) > 0

    # ---- download history ----
    def add_download(self, user_id: int, content_id: str, title: str, url: str):
        """Log a download."""
        key = str(user_id)
        if key not in self.data["downloads"]:
            self.data["downloads"][key] = []
        
        self.data["downloads"][key].append({
            "content_id": content_id,
            "title": title,
            "url": url,
            "timestamp": time.time(),
            "date": _today(),
        })
        
        # Keep only last 100 downloads per user
        if len(self.data["downloads"][key]) > 100:
            self.data["downloads"][key].pop(0)
        
        self._dirty = True

    def get_downloads(self, user_id: int, limit: int = 10) -> list:
        """Get recent downloads for user."""
        key = str(user_id)
        downloads = self.data["downloads"].get(key, [])
        return downloads[-limit:] if downloads else []

    # ---- channel management ----
    def add_channel(self, channel_id: int):
        """Add a channel to mirror uploads."""
        channel_id = int(channel_id)
        if channel_id not in self.data["channels"]:
            self.data["channels"].append(channel_id)
            self._dirty = True

    def remove_channel(self, channel_id: int):
        """Remove a channel."""
        channel_id = int(channel_id)
        if channel_id in self.data["channels"]:
            self.data["channels"].remove(channel_id)
            self._dirty = True

    def get_channels(self) -> list:
        """Get all mirror channels."""
        return self.data.get("channels", [])

    # ---- ban management ----
    def ban_user(self, user_id: int):
        """Ban a user."""
        user_id = int(user_id)
        if user_id not in self.data["banned"]:
            self.data["banned"].append(user_id)
            self._dirty = True

    def unban_user(self, user_id: int):
        """Unban a user."""
        user_id = int(user_id)
        if user_id in self.data["banned"]:
            self.data["banned"].remove(user_id)
            self._dirty = True

    def is_banned(self, user_id: int) -> bool:
        """Check if user is banned."""
        return int(user_id) in self.data["banned"]

    # ---- stats ----
    def get_stats(self) -> dict:
        """Get bot statistics."""
        total_users = len(self.data["users"])
        total_downloads = sum(len(v) for v in self.data["downloads"].values())
        total_cookies = sum(
            len(self.data["users"].get(k, {}).get("cookies", [])) 
            for k in self.data["users"]
        )
        return {
            "users": total_users,
            "downloads": total_downloads,
            "channels": len(self.data["channels"]),
            "banned": len(self.data["banned"]),
            "cookies": total_cookies,
        }

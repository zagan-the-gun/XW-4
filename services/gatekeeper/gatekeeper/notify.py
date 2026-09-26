"""Discord Webhook への通知（画像付き埋め込み）。"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

COLORS = {"low": 0x5B6270, "medium": 0xFFB000, "high": 0xFF4D4F}


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], session=None, timeout: float = 15.0):
        self.url = (webhook_url or "").strip()
        self.timeout = timeout
        self.session = session
        if self.url and self.session is None:
            import requests

            self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def send(self, title: str, description: str, level: str, fields: list, image: Optional[bytes] = None,
             link: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        embed = {
            "title": title,
            "description": description,
            "color": COLORS.get(level, COLORS["low"]),
            "fields": [{"name": n, "value": v, "inline": True} for n, v in fields][:10],
        }
        if link:
            embed["url"] = link
        if image:
            embed["image"] = {"url": "attachment://snapshot.jpg"}
        payload = {"username": "門番くん", "embeds": [embed]}
        try:
            if image:
                r = self.session.post(self.url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                                      files={"file": ("snapshot.jpg", image, "image/jpeg")}, timeout=self.timeout)
            else:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
            ok = 200 <= r.status_code < 300
            if not ok:
                log.warning("Discord 通知に失敗: %s %s", r.status_code, getattr(r, "text", "")[:120])
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("Discord 通知に失敗: %s", e)
            return False

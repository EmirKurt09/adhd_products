"""Pushover: sistem uyarılarının kanalı (çökme, erişim sorunu, hata artışı, giriş sorunları).

Ders bildirimleri Telegram'da kalır; sistemin kendisiyle ilgili uyarılar buraya gelir. Böylece Telegram
tarafında bir sorun olsa da uyarı ulaşır. Bekçi (takılma algılayıcı) event loop'a güvenemeyeceği için
senkron gönderimi kullanır.
"""

from __future__ import annotations

import re

import httpx

API_URL = "https://api.pushover.net/1/messages.json"
VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"
TITLE = "e-Kampüs asistanı"
MAX_MESSAGE = 1024
# Acil (2) öncelik: onaylanana kadar her EMERGENCY_RETRY_S saniyede bir tekrar çalar, en fazla EMERGENCY_EXPIRE_S
EMERGENCY_RETRY_S = 300
EMERGENCY_EXPIRE_S = 3600
_ALLOWED = {"b", "i", "u", "font", "a"}


class PushoverError(Exception):
    pass


def to_pushover_html(text: str) -> str:
    """Telegram HTML'ini Pushover'ın desteklediği küçük alt kümeye indirger; uzunluk sınırına göre kırpar."""
    text = re.sub(r"</?code>", "", text)
    text = re.sub(r"<(/?)s>", "", text)
    text = re.sub(r"<(/?)([a-zA-Z]+)([^>]*)>",
                  lambda m: m.group(0) if m.group(2).lower() in _ALLOWED else "", text)
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 1].rstrip() + "…"
        text = re.sub(r"<[^>]*$", "", text)  # yarım kalan etiket olmasın
    return text


class Pushover:
    def __init__(self, app_token: str, user_key: str, *, transport: httpx.BaseTransport | None = None,
                 async_transport: httpx.AsyncBaseTransport | None = None):
        self._token = app_token
        self._user = user_key
        self._transport = transport
        self._async_transport = async_transport

    def _payload(self, text: str, priority: int, url: str | None, url_title: str | None) -> dict:
        priority = max(-2, min(2, priority))
        data = {"token": self._token, "user": self._user, "title": TITLE, "message": to_pushover_html(text) or "-",
                "html": "1", "priority": str(priority)}
        if priority == 2:
            data["retry"] = str(EMERGENCY_RETRY_S)
            data["expire"] = str(EMERGENCY_EXPIRE_S)
        if url:
            data["url"] = url
            data["url_title"] = url_title or "Aç"
        return data

    @staticmethod
    def _check(response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or body.get("status") != 1:
            errors = ", ".join(body.get("errors", [])) or f"HTTP {response.status_code}"
            raise PushoverError(errors)

    async def send(self, text: str, priority: int = 0, url: str | None = None, url_title: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=20, transport=self._async_transport) as client:
            self._check(await client.post(API_URL, data=self._payload(text, priority, url, url_title)))

    def send_sync(self, text: str, priority: int = 0) -> None:
        with httpx.Client(timeout=20, transport=self._transport) as client:
            self._check(client.post(API_URL, data=self._payload(text, priority, None, None)))

    async def validate(self) -> str:
        """Anahtarları Pushover'a doğrulatır; geçerliyse kayıtlı cihazları döndürür, değilse PushoverError."""
        async with httpx.AsyncClient(timeout=20, transport=self._async_transport) as client:
            response = await client.post(VALIDATE_URL, data={"token": self._token, "user": self._user})
        self._check(response)
        return ", ".join(response.json().get("devices", [])) or "cihaz yok"


def make_pushover(settings) -> Pushover | None:
    return Pushover(settings.pushover_app_token, settings.pushover_user_key) if settings.pushover_enabled else None

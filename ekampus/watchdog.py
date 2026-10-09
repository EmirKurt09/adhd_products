"""Takılma bekçisi: bot süreci ayakta ama işlemiyorsa (event loop takıldı) fark eder ve süreci kapatır.

Docker (restart: unless-stopped) ya da Windows Görev Zamanlayıcı süreci yeniden başlatır. Bekçi ayrı bir
thread'de çalışır ve takılmış event loop'a ihtiyaç duymadan Pushover'a senkron haber verir. Canlılık işareti,
gönderim işinin 15 saniyede bir güncellediği heartbeat dosyasıdır.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from . import prefs as PR
from .config import Settings
from .pushover import Pushover
from .store import Store

log = logging.getLogger(__name__)

STALE_AFTER_S = 10 * 60
CHECK_EVERY_S = 60
EXIT_CODE = 3


def heartbeat_age(path: Path, now: float | None = None) -> float | None:
    try:
        return (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return None


def start_watchdog(settings: Settings, pushover: Pushover | None, stale_after: float = STALE_AFTER_S) -> threading.Thread:
    started = time.monotonic()

    def loop() -> None:
        while True:
            time.sleep(CHECK_EVERY_S)
            if time.monotonic() - started < stale_after:  # açılışta eski heartbeat'e bakıp yanlış alarm verme
                continue
            age = heartbeat_age(settings.heartbeat_path)
            if age is not None and age > stale_after:
                _restart(settings, pushover, age)

    thread = threading.Thread(target=loop, name="watchdog", daemon=True)
    thread.start()
    return thread


def _restart(settings: Settings, pushover: Pushover | None, age: float) -> None:
    text = f"<b>Bot {int(age // 60)} dakikadır yanıt vermiyor</b>; yeniden başlatılıyor."
    log.critical("Takılma algılandı (heartbeat %d sn önce), süreç kapatılıyor", int(age))
    try:
        settings.watchdog_marker_path.write_text(f"heartbeat {int(age)} sn", encoding="utf-8")
    except OSError:
        pass
    if pushover is not None and _pushover_enabled(settings):
        try:
            pushover.send_sync(text, priority=1)
        except Exception as e:  # noqa: BLE001 - kapanmayı hiçbir şey engellememeli
            log.error("Pushover'a takılma bildirilemedi: %s", e)
    logging.shutdown()
    os._exit(EXIT_CODE)


def _pushover_enabled(settings: Settings) -> bool:
    """/ayarlar'daki Pushover anahtarı. Bekçi ayrı thread'de olduğu için kendi kısa bağlantısıyla okur;
    okuyamazsa göndermeyi seçer (takılma haberi kaybolmasın)."""
    try:
        store = Store(settings.db_path)
        try:
            return bool(PR.load(store).get("pushover", True))
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return True

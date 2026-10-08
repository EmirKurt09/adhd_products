"""Komut satırı: `python -m ekampus <komut>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from .config import ConfigError, Settings, load_settings


def _setup_logging(settings: Settings, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr is not None:  # pythonw altında konsol yok; sadece dosyaya yazılır
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(settings.logs_dir / "ekampus.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)
    except OSError:
        pass
    # httpx her isteği INFO'da URL'siyle loglar; Telegram URL'si bot token'ını içerir → sustur
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _login(settings: Settings, headed: bool, force: bool) -> int:
    from .browser import AuthError, Campus

    settings.require("ekampus")
    async with Campus(settings, headless=not headed) as campus:
        if force:
            campus.guard.reset()
        page = await campus.context.new_page()
        try:
            if headed:
                print("Tarayıcı açıldı. Gerekirse captcha'yı çöz ve 'Giriş Yap'a bas (5 dk süren var)...")
                await campus.manual_login(page)
            else:
                await campus.open_home(page)
        except AuthError as e:
            print(f"Giriş başarısız: {type(e).__name__}: {e}")
            return 1
        print(f"Giriş tamam: {await page.title()}\nOturum kaydedildi: {settings.session_path}")
    return 0


async def _explore(settings: Settings, max_pages: int) -> int:
    from .browser import AuthError
    from .explore import explore

    settings.require("ekampus")
    try:
        index = await explore(settings, max_pages=max_pages)
    except AuthError as e:
        print(f"Keşif başlatılamadı: {type(e).__name__}: {e}")
        return 1
    print(
        f"\nKeşif bitti: {len(index['pages'])} sayfa, {len(index['json_responses'])} JSON yanıtı, "
        f"{len(index['skipped_links'])} atlanan link\nÇıktı: {settings.explore_dir}"
    )
    return 0


async def _check(settings: Settings, dry_run: bool) -> int:
    from datetime import datetime, timezone

    from .browser import AuthError
    from .detect import diff
    from .scan import FetchError, with_scanner
    from .store import Store

    settings.require("ekampus")
    try:
        scan, report = await with_scanner(settings, lambda scanner: scanner.scan())
    except (AuthError, FetchError) as e:
        print(f"Tarama başarısız: {type(e).__name__}: {e}")
        return 1

    by_kind: dict[str, list] = {}
    for item in scan.items:
        by_kind.setdefault(item.kind, []).append(item)
    for kind, items in sorted(by_kind.items()):
        print(f"\n[{kind}] {len(items)} kayıt")
        for item in sorted(items, key=lambda i: (i.course, i.due_iso or "", i.title)):
            due = item.due_at.astimezone(settings.tz).strftime("%d.%m %H:%M") if item.due_at else "-"
            flags = " ".join(f"{k}={v}" for k, v in {**item.extra, **item.meta}.items() if v not in (None, "", {}))
            print(f"  {item.course[:28]:28} | {item.title[:40]:40} | {due:11} | {flags[:90]}")
    if scan.failed_scopes:
        print("\nHatalı kapsamlar:")
        for (kind, scope), err in scan.failed_scopes.items():
            print(f"  {kind}/{scope}: {err}")
    print(f"\n{len(report.courses)} ders, {len(scan.ok_scopes)} başarılı kapsam, tam tarama: {scan.complete}")

    if dry_run:
        return 0
    store = Store(settings.db_path)
    try:
        result = diff(store.stored_items(), store.known_scopes(), scan)
        store.apply(scan, result, datetime.now(timezone.utc))
    finally:
        store.close()
    print(f"\nOlaylar ({len(result.events)}): " + ", ".join(f"{e.type}:{e.data.get('title', '')}" for e in result.events))
    if result.anomalies:
        print("Anomaliler: " + "; ".join(result.anomalies))
    return 0


async def _setup_telegram(settings: Settings) -> int:
    from telegram import Bot

    if not settings.telegram_token:
        print("Önce .env'e TELEGRAM_BOT_TOKEN yaz (@BotFather → /newbot).")
        return 1
    async with Bot(settings.telegram_token) as bot:
        me = await bot.get_me()
        updates = await bot.get_updates(timeout=0)
    chats = {u.effective_chat.id: u.effective_chat for u in updates if u.effective_chat}
    if not chats:
        print(f"@{me.username} botuna Telegram'dan /start yaz, sonra bu komutu tekrar çalıştır.")
        return 1
    print(f"@{me.username} botuna yazan chat'ler:")
    for chat in chats.values():
        name = chat.full_name or chat.title or ""
        print(f"  {chat.id}  {chat.type}  {name}  @{chat.username or '-'}")
    print("\nKendi chat id'ni .env içinde TELEGRAM_OWNER_CHAT_ID olarak yaz.")
    return 0


async def _test_notify(settings: Settings) -> int:
    from telegram import Bot

    settings.require("telegram")
    async with Bot(settings.telegram_token) as bot:
        await bot.send_message(settings.telegram_owner_chat_id, "✅ e-Kampüs asistanı: test bildirimi")
    print("Gönderildi.")
    return 0


def _health(settings: Settings) -> int:
    """Konteyner sağlık kontrolü: bot döngüsü heartbeat dosyasını düzenli güncellemeli."""
    try:
        age = time.time() - settings.heartbeat_path.stat().st_mtime
    except FileNotFoundError:
        print("heartbeat yok")
        return 1
    limit = max(settings.poll_interval_min, settings.night_poll_interval_min) * 60 * 2 + 300
    print(f"heartbeat {int(age)} sn önce")
    return 0 if age < limit else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="ekampus", description="e-Kampüs asistanı")
    parser.add_argument("-v", "--verbose", action="store_true", help="ayrıntılı log")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="ortamı ve ayarları kontrol et (hiçbir şeyi değiştirmez)")
    login = sub.add_parser("login", help="giriş yap ve oturumu kaydet")
    login.add_argument("--headed", action="store_true", help="görünür tarayıcıda elle giriş (captcha durumunda)")
    login.add_argument("--force", action="store_true", help="login kilidini sıfırla ve yeniden dene")
    explore = sub.add_parser("explore", help="siteyi sadece okuyarak keşfet")
    explore.add_argument("--max-pages", type=int, default=80)
    check = sub.add_parser("check", help="tek tarama turu yap (bildirimler outbox'a yazılır)")
    check.add_argument("--dry-run", action="store_true", help="sadece oku ve göster, durumu değiştirme")
    sub.add_parser("setup-telegram", help="bota yazan chat'lerin kimliğini göster")
    sub.add_parser("test-notify", help="Telegram'a deneme mesajı gönder")
    sub.add_parser("bot", help="Telegram botunu ve izlemeyi başlat (sürekli çalışır)")
    sub.add_parser("health", help="konteyner sağlık kontrolü")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Ayar hatası: {e}", file=sys.stderr)
        return 2
    if args.cmd == "health":
        return _health(settings)
    _setup_logging(settings, args.verbose)

    try:
        if args.cmd == "bot":
            from .bot import run

            return run(settings)
        if args.cmd == "doctor":
            from . import doctor

            return asyncio.run(doctor.run(settings))
        if args.cmd == "login":
            return asyncio.run(_login(settings, args.headed, args.force))
        if args.cmd == "explore":
            return asyncio.run(_explore(settings, args.max_pages))
        if args.cmd == "check":
            return asyncio.run(_check(settings, args.dry_run))
        if args.cmd == "setup-telegram":
            return asyncio.run(_setup_telegram(settings))
        if args.cmd == "test-notify":
            return asyncio.run(_test_notify(settings))
    except ConfigError as e:
        print(f"Ayar hatası: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""`python -m ekampus doctor`: ortamı uçtan uca kontrol eder. Hiçbir şeyi değiştirmez, login denemez."""

from __future__ import annotations

import platform
import sys
from importlib.metadata import version

from playwright.async_api import Error as PlaywrightError

from .browser import Campus, is_login_url
from .config import GROUPS, STUDENT_URL, Settings

OK, WARN, FAIL, SKIP = "✓", "!", "✗", "–"


async def run(settings: Settings) -> int:
    results: list[tuple[str, str, str]] = []

    def add(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))
        print(f" {status} {name}" + (f": {detail}" if detail else ""), flush=True)

    add(OK, "Python", f"{platform.python_version()} ({sys.executable})")
    add(OK, "Playwright", version("playwright"))

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".yazma-testi"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "Veri dizini", str(settings.data_dir))
    except OSError as e:
        add(FAIL, "Veri dizini", f"{settings.data_dir} yazılamıyor: {e}")

    for group in GROUPS:
        missing = settings.missing(group)
        add(OK if not missing else WARN, f".env [{group}]", "tamam" if not missing else f"eksik: {', '.join(missing)}")

    await _check_site(settings, add)
    _check_guard(settings, add)
    await _check_telegram(settings, add)
    await _check_llm(settings, add)

    failed = [r for r in results if r[0] == FAIL]
    print()
    print("Sonuç:", "hata var" if failed else "kritik hata yok", flush=True)
    return 1 if failed else 0


async def _check_site(settings: Settings, add) -> None:
    try:
        async with Campus(settings) as campus:
            add(OK, "Chromium", campus._browser.version)
            page = await campus.context.new_page()
            await page.goto(STUDENT_URL, wait_until="domcontentloaded", timeout=60_000)
            await campus.settle(page)
            if is_login_url(page.url):
                fields = {sel: await page.locator(sel).count() for sel in ("#Username", "#Password", "#btnSubmit")}
                missing = [sel for sel, n in fields.items() if n == 0]
                if missing:
                    add(FAIL, "Login formu", f"beklenen alan(lar) yok: {', '.join(missing)}; site değişmiş olabilir")
                else:
                    add(OK, "Login formu", "beklenen yapıda")
                captcha = await campus._captcha_visible(page)
                add(WARN if captcha else OK, "Captcha", "GÖRÜNÜR, elle giriş gerekecek" if captcha else "görünür değil")
                state = "kayıtlı oturum süresi dolmuş" if settings.session_path.exists() else "henüz kayıtlı oturum yok"
                add(SKIP, "Oturum", state)
            elif page.url.startswith(STUDENT_URL):
                add(OK, "Oturum", f"kayıtlı oturum geçerli ({await page.title()})")
            else:
                add(WARN, "Site", f"beklenmeyen adres: {page.url.split('?')[0]}")
    except PlaywrightError as e:
        add(FAIL, "Tarayıcı/site", str(e).splitlines()[0])
    except Exception as e:  # noqa: BLE001 - doctor her hatayı raporlamalı
        add(FAIL, "Tarayıcı/site", f"{type(e).__name__}: {e}")


def _check_guard(settings: Settings, add) -> None:
    from .browser import AuthGuard

    status = AuthGuard(settings.auth_guard_path, settings.username, settings.password).status()
    if status.get("blocked"):
        add(FAIL, "Login kilidi", f"KİLİTLİ: {status.get('reason')} (şifreyi düzelt ya da `login --force`)")
    elif status.get("last_success_at"):
        add(OK, "Login kilidi", f"açık, son başarılı giriş {status['last_success_at']}")
    else:
        add(OK, "Login kilidi", "açık")


async def _check_telegram(settings: Settings, add) -> None:
    if not settings.telegram_token:
        add(SKIP, "Telegram", "token yok")
        return
    from telegram import Bot
    from telegram.error import TelegramError

    try:
        async with Bot(settings.telegram_token) as bot:
            me = await bot.get_me()
        add(OK, "Telegram bot", f"@{me.username}")
    except TelegramError as e:
        add(FAIL, "Telegram bot", f"token geçersiz ya da erişilemiyor: {e}")
        return
    if settings.telegram_owner_chat_id:
        add(OK, "Telegram sahip", str(settings.telegram_owner_chat_id))
    else:
        add(WARN, "Telegram sahip", "TELEGRAM_OWNER_CHAT_ID yok; bota /start yaz, sonra `setup-telegram`")


async def _check_llm(settings: Settings, add) -> None:
    if not settings.llm_api_key:
        add(SKIP, "LLM", "anahtar yok")
        return
    from openai import AsyncOpenAI, OpenAIError

    try:
        async with AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=20) as client:
            models = sorted(m.id for m in (await client.models.list()).data)
    except OpenAIError as e:
        add(FAIL, "LLM", f"{settings.llm_provider} erişilemiyor: {type(e).__name__}: {str(e)[:200]}")
        return
    add(OK, "LLM", f"{settings.llm_provider} erişilebilir, modeller: {', '.join(models[:12])}")
    if settings.llm_model and settings.llm_model not in models:
        add(WARN, "LLM modeli", f"{settings.llm_model!r} listede yok")
    elif not settings.llm_model:
        add(WARN, "LLM modeli", "LLM_MODEL boş; yukarıdakilerden birini seç")
    else:
        add(OK, "LLM modeli", settings.llm_model)

"""Playwright oturumu: kayıtlı oturumu yeniden kullanır, gerektiğinde tek denemelik login yapar.

Login kuralları (hesap kilitlenmesin, captcha tetiklenmesin diye):
  * Sunucu kimlik bilgisini bir kez reddederse aynı bilgilerle bir daha denenmez
    (.env değişene ya da guard elle sıfırlanana kadar).
  * İki login denemesi arasında en az LOGIN_COOLDOWN_S beklenir.
  * Sadece sunucunun gerçekten yanıt verdiği reddetmeler kilitler; ağ hataları kilitlemez.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from .config import IDP_URL, STUDENT_URL, Settings

log = logging.getLogger(__name__)

LOGIN_COOLDOWN_S = 10 * 60
ANALYTICS_HOSTS = ("clarity.ms", "googletagmanager.com", "google-analytics.com", "doubleclick.net")
HEAVY_RESOURCE_TYPES = {"image", "font", "media"}
LOGIN_ERROR_SELECTORS = (
    ".validation-summary-errors li",
    ".validation-summary-errors",
    ".field-validation-error",
    ".alert-danger",
    ".toast-error .toast-message",
    "span.text-danger",
    "div.text-danger",
)


class AuthError(Exception):
    """Giriş yapılamadı (geçici olabilir)."""


class LoginRejected(AuthError):
    """Sunucu kimlik bilgilerini reddetti; guard kilitlendi."""


class CaptchaRequired(AuthError):
    """Login sayfası captcha istiyor; `login --headed` ile elle giriş gerekir."""


class AuthBlocked(AuthError):
    """Guard kilitli: önceki ret yüzünden login denenmiyor."""


class LoginCooldown(AuthError):
    """Son denemeden bu yana yeterli süre geçmedi."""


class AuthGuard:
    """Login denemelerinin kalıcı kaydı (DATA_DIR/auth_guard.json). Şifrenin kendisi değil, özeti tutulur."""

    def __init__(self, path: Path, username: str, password: str):
        self.path = path
        self.cred_hash = hashlib.sha256(f"{username}\0{password}".encode()).hexdigest()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def status(self) -> dict:
        data = self._read()
        blocked = bool(data.get("blocked")) and data.get("cred_hash") == self.cred_hash
        return {**data, "blocked": blocked}

    def check(self) -> None:
        data = self._read()
        if data.get("blocked") and data.get("cred_hash") == self.cred_hash:
            raise AuthBlocked(data.get("reason") or "önceki giriş reddedildi")
        wait = LOGIN_COOLDOWN_S - (time.time() - float(data.get("last_attempt_ts", 0)))
        if wait > 0:
            raise LoginCooldown(f"son login denemesinden beri çok kısa süre geçti, {int(wait)} sn bekle")

    def record_attempt(self) -> None:
        data = self._read()
        data["last_attempt_ts"] = time.time()
        data["last_attempt_at"] = _now_iso()
        self._write(data)

    def record_success(self) -> None:
        data = self._read()
        data.update(blocked=False, reason=None, cred_hash=self.cred_hash, last_success_at=_now_iso())
        self._write(data)

    def block(self, reason: str) -> None:
        data = self._read()
        data.update(blocked=True, reason=reason, cred_hash=self.cred_hash, blocked_at=_now_iso())
        self._write(data)

    def reset(self) -> None:
        data = self._read()
        data.update(blocked=False, reason=None, last_attempt_ts=0)
        self._write(data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_login_url(url: str) -> bool:
    return url.lower().startswith(f"{IDP_URL}/account/login".lower())


class Campus:
    """Tek tarayıcı + tek bağlam. `async with Campus(settings) as campus:` ile kullanılır."""

    def __init__(self, settings: Settings, *, headless: bool | None = None, block_assets: bool = True):
        self.s = settings
        self.headless = settings.headless if headless is None else headless
        self.block_assets = block_assets
        self.guard = AuthGuard(settings.auth_guard_path, settings.username, settings.password)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> Campus:
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self.context = await self.new_context()
        return self

    async def __aexit__(self, *exc) -> None:
        for closer in (self.context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._pw is not None:
            await self._pw.stop()

    async def new_context(self, **kwargs) -> BrowserContext:
        """Kayıtlı oturumla yeni bağlam. Keşifte HAR kaydı gibi ek ayarlar buradan geçer."""
        assert self._browser is not None
        state = str(self.s.session_path) if self.s.session_path.exists() else None
        ctx = await self._browser.new_context(
            storage_state=state,
            locale="tr-TR",
            timezone_id=self.s.tz.key,
            viewport={"width": 1366, "height": 900},
            **kwargs,
        )
        await ctx.route("**/*", self._route)
        return ctx

    async def _route(self, route: Route) -> None:
        request = route.request
        host = urlsplit(request.url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in ANALYTICS_HOSTS):
            await route.abort()
        elif self.block_assets and request.resource_type in HEAVY_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    async def save_session(self, context: BrowserContext | None = None) -> None:
        ctx = context or self.context
        assert ctx is not None
        tmp = self.s.session_path.with_suffix(".tmp")
        await ctx.storage_state(path=str(tmp))
        tmp.replace(self.s.session_path)

    async def open_home(self, page: Page) -> None:
        """Öğrenci ana sayfasına gider; oturum düşmüşse tek denemelik login yapar."""
        try:
            await page.goto(STUDENT_URL, wait_until="domcontentloaded", timeout=60_000)
            await self.settle(page)
        except PlaywrightError as e:
            raise AuthError(f"Siteye ulaşılamadı: {self._redact(str(e))}") from None
        if is_login_url(page.url):
            await self._login(page)
        if not page.url.startswith(STUDENT_URL):
            raise AuthError(f"Giriş sonrası beklenmeyen adres: {page.url.split('?')[0]}")
        await self.save_session(page.context)

    async def settle(self, page: Page, timeout_s: float = 45) -> None:
        """OIDC yönlendirme zincirinin (gizli form → IdP → form_post → öğrenci sitesi) bitmesini bekler."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            url = page.url
            if is_login_url(url):
                await page.wait_for_load_state("domcontentloaded")
                return
            if url.startswith(STUDENT_URL) and "/signin-oidc" not in url:
                try:
                    if await page.locator("form[name=hiddenform]").count() == 0:
                        await page.wait_for_load_state("networkidle", timeout=20_000)
                        return
                except PlaywrightError:
                    pass  # navigasyon sırasında DOM değişti, tekrar bak
            await page.wait_for_timeout(500)
        raise AuthError("Oturum yönlendirmesi zaman aşımına uğradı")

    async def _login(self, page: Page) -> None:
        self.s.require("ekampus")
        self.guard.check()
        if await self._captcha_visible(page):
            self.guard.block("captcha istendi")
            raise CaptchaRequired("Login sayfası captcha istiyor; `python -m ekampus login --headed` ile elle gir")

        self.guard.record_attempt()
        log.info("Oturum yok ya da düşmüş, giriş yapılıyor (tek deneme)")
        try:
            await page.fill("#Username", self.s.username)
            await page.fill("#Password", self.s.password)
            await page.evaluate("() => { const c = document.getElementById('RememberMe'); if (c) c.checked = true; }")
            async with page.expect_response(
                lambda r: r.request.method == "POST" and "/account/login" in r.url.lower(), timeout=30_000
            ) as info:
                await page.click("#btnSubmit")
            response = await info.value
        except PlaywrightTimeout:
            raise AuthError("Login isteği sunucuya ulaşmadı (zaman aşımı); kimlik bilgisi denenmiş sayılmaz") from None
        except PlaywrightError as e:
            raise AuthError(f"Login sırasında tarayıcı hatası: {self._redact(str(e))}") from None

        if response.status == 200:
            # ASP.NET login başarıda yönlendirir (302); 200 = form hatayla yeniden çizildi → ret
            await page.wait_for_load_state("domcontentloaded")
            if await self._captcha_visible(page):
                self.guard.block("captcha istendi")
                raise CaptchaRequired("Sunucu captcha istedi; `python -m ekampus login --headed` ile elle gir")
            message = await self._login_error_text(page) or "sunucu girişi reddetti (mesaj okunamadı)"
            self.guard.block(f"Giriş reddedildi: {message}")
            raise LoginRejected(message)
        if response.status >= 400:
            raise AuthError(f"Login sunucusu {response.status} döndü")

        await self.settle(page)
        if is_login_url(page.url):
            message = await self._login_error_text(page) or "girişten sonra tekrar login sayfasına dönüldü"
            self.guard.block(f"Giriş reddedildi: {message}")
            raise LoginRejected(message)
        self.guard.record_success()
        log.info("Giriş başarılı, oturum kaydedildi")

    async def manual_login(self, page: Page, wait_s: int = 300) -> None:
        """Görünür tarayıcıda kullanıcının elle giriş yapmasını bekler (captcha durumları için)."""
        await page.goto(STUDENT_URL, wait_until="domcontentloaded", timeout=60_000)
        await self.settle(page)
        if is_login_url(page.url) and self.s.username:
            await page.fill("#Username", self.s.username)
            if self.s.password:
                await page.fill("#Password", self.s.password)
        await page.wait_for_url(
            lambda u: u.startswith(STUDENT_URL) and "/signin-oidc" not in u, timeout=wait_s * 1000
        )
        await self.settle(page)
        self.guard.record_success()
        await self.save_session(page.context)

    @staticmethod
    async def _captcha_visible(page: Page) -> bool:
        for selector in ("iframe[src*='recaptcha'][src*='anchor']", ".g-recaptcha", "iframe[src*='hcaptcha']"):
            loc = page.locator(selector)
            if await loc.count() and await loc.first.is_visible():
                return True
        return False

    @staticmethod
    async def _login_error_text(page: Page) -> str:
        for selector in LOGIN_ERROR_SELECTORS:
            for text in await page.locator(selector).all_inner_texts():
                text = " ".join(text.split())
                if text:
                    return text[:300]
        return ""

    def _redact(self, text: str) -> str:
        if self.s.password:
            text = text.replace(self.s.password, "***")
        return text

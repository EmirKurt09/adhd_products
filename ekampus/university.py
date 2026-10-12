"""Public üniversite duyuruları: Playwright okuma, başlık üzerinden JEV gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .browser import ANALYTICS_HOSTS, HEAVY_RESOURCE_TYPES
from .engine import Download, TELEGRAM_FILE_LIMIT, material_filename

SOURCE_URL = "https://ticaret.edu.tr/category/duyurular/"
MONTHS = {name: i for i, name in enumerate(
    ("ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık"), 1)}


class UniversityError(Exception):
    pass


def public_url(value: str, base: str = SOURCE_URL) -> str:
    """Kaynak bağlantıları yalnızca üniversitenin public HTTPS alanlarında kalır."""
    parts = urlsplit(urljoin(base, value))
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password
            or not (host == "ticaret.edu.tr" or host.endswith(".ticaret.edu.tr"))):
        raise UniversityError("Üniversite dışı veya geçersiz bağlantı")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


@dataclass(frozen=True)
class Notice:
    title: str
    url: str
    published: str  # ISO date; liste yalnızca günü veriyor


def parse_date(text: str) -> str:
    words = re.sub(r"[,\s]+", " ", text.strip().lower()).split()
    try:
        if words[0] in MONTHS:
            month, day, year = MONTHS[words[0]], int(words[1]), int(words[2])
        else:
            day, month, year = int(words[0]), MONTHS[words[1]], int(words[2])
        return date(year, month, day).isoformat()
    except (IndexError, KeyError, ValueError) as e:
        raise UniversityError("Duyuru tarihi okunamadı") from e


def parse_listing(html: str, url: str = SOURCE_URL) -> tuple[list[Notice], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    notices = []
    for node in soup.select(".post-item"):
        link, stamp = node.select_one(".post-title a[href]"), node.select_one(".post-date")
        if not link or not stamp or not link.get_text(strip=True):
            raise UniversityError("Duyuru listesi eksik alan içeriyor")
        notices.append(Notice(link.get_text(" ", strip=True), public_url(link["href"], url),
                              parse_date(stamp.get_text(" ", strip=True))))
    if not notices:
        raise UniversityError("Duyuru listesi okunamadı; boş liste başlangıç sayılmaz")
    next_link = soup.select_one("a.next.page-numbers[href]")
    return notices, public_url(next_link["href"], url) if next_link else None


def parse_detail(html: str, url: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h2.title")
    if title is None:
        raise UniversityError("Duyuru detayı okunamadı")
    content = title.parent
    attachments = {}
    for link in content.select("a[href]"):
        href = urljoin(url, link["href"])
        if PurePosixPath(unquote(urlsplit(href).path)).suffix.lower() == ".pdf":
            href = public_url(href)
            attachments[href] = {"url": href, "title": link.get_text(" ", strip=True) or "Duyuru eki"}
    for unwanted in content.select(".header, h2.title, script, style, noscript"):
        unwanted.decompose()
    return content.get_text(" ", strip=True), list(attachments.values())


class UniversityBrowser:
    """e-Kampüs storage state veya login kullanmayan kısa ömürlü Playwright context."""

    def __init__(self, settings):
        self.settings = settings
        self.pw = self.browser = self.context = None

    async def __aenter__(self):
        try:
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=self.settings.headless)
            self.context = await self.browser.new_context(locale="tr-TR", timezone_id=self.settings.tz.key)
            await self.context.route("**/*", self._route)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.pw:
                await self.pw.stop()

    @staticmethod
    async def _route(route):
        host = urlsplit(route.request.url).hostname or ""
        if (route.request.resource_type in HEAVY_RESOURCE_TYPES
                or any(host == h or host.endswith("." + h) for h in ANALYTICS_HOSTS)):
            await route.abort()
        else:
            await route.continue_()

    async def html(self, url: str) -> str:
        page = await self.context.new_page()
        try:
            response = await page.goto(public_url(url), wait_until="domcontentloaded", timeout=60_000)
            if response is None or response.status >= 400:
                raise UniversityError(f"Duyuru sayfası HTTP {response.status if response else 'yanıtsız'}")
            public_url(page.url)
            return await page.content()
        finally:
            await page.close()

    async def listing(self, url: str):
        return parse_listing(await self.html(url), url)

    async def detail(self, url: str):
        return parse_detail(await self.html(url), url)

    async def download(self, url: str, title: str) -> Download:
        # Dosya da Playwright'ın aynı public context'inden alınır; PDF metni çıkarılmaz.
        url = public_url(url)
        head = await self.context.request.head(url, timeout=30_000)
        try:
            size = int(head.headers.get("content-length", "0") or 0)
            if head.ok and size > TELEGRAM_FILE_LIMIT:
                return Download(material_filename(title, url, "application/pdf"), None, url)
        finally:
            await head.dispose()
        response = await self.context.request.get(url, timeout=120_000)
        try:
            if not response.ok:
                raise UniversityError(f"PDF indirilemedi: HTTP {response.status}")
            public_url(response.url)
            body = await response.body()
            if not body.startswith(b"%PDF-"):
                raise UniversityError("Ek bağlantısı PDF döndürmedi")
            return Download(material_filename(title, url, "application/pdf"),
                            body if len(body) <= TELEGRAM_FILE_LIMIT else None, url)
        finally:
            await response.dispose()

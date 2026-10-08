"""Site keşfi: oturum açıp öğrenci sitesini sadece GET ile gezer ve her şeyi kaydeder.

Çıktı (DATA_DIR/explore/, kişisel veri içerir, repoya girmez):
  index.json        gezilen sayfalar, atlanan linkler (nedeniyle), yakalanan JSON yanıtları
  pages/NNN.*       her sayfanın HTML'i, görünen metni ve ekran görüntüsü
  json/NNN.json     sitenin kendi XHR/JSON yanıtları (asıl veri kaynağı adayları)
  trafik.har        login SONRASI tüm trafik (login isteği ve şifre bu kayda girmez)

Güvenlik: aynı origin dışına çıkılmaz, form gönderilmez, tıklanmaz; sadece href'lere GET yapılır.
Durum değiştirebilecek (çıkış, silme, teslim, yükleme, sınav başlatma, canlı derse katılma vb.)
linkler atlanır.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import unicodedata
from collections import Counter, deque
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError, Page, Response

from .browser import Campus, is_login_url
from .config import STUDENT_URL, Settings

log = logging.getLogger(__name__)

PER_PATTERN_LIMIT = 3  # aynı şablondaki (sadece ID'si farklı) sayfalardan en fazla bu kadar gez
# URL/link metninde geçerse atla (alt dize eşleşmesi; ASCII'ye indirgenmiş küçük harf)
DENY_SUBSTRINGS = (
    "logout", "logoff", "signout", "cikis", "delete", "remove", "teslim", "submit", "upload", "yukle",
    "gonder", "basla", "start", "attempt", "launch", "katil", "join", "sinav", "exam", "quiz",
    "download", "indir", "print", "yazdir", "kaydet", "save", "update", "guncelle", "onayla", "approve",
    "enroll", "sync",  # enroll içeriği "görüldü" işaretler; sync dersleri ÖBS ile eşitler
)
# Sadece tam kelime olarak geçerse atla (kısa olduğu için alt dize eşleşmesi yanlış pozitif verir)
DENY_TOKENS = {"sil", "edit", "duzenle", "iptal", "cancel"}

_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUM = re.compile(r"\d+")


def _ascii(text: str) -> str:
    text = text.replace("ı", "i").replace("İ", "I")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def deny_reason(url: str, text: str) -> str | None:
    haystack = _ascii(f"{urlsplit(url).path} {urlsplit(url).query} {text}")
    for word in DENY_SUBSTRINGS:
        if word in haystack:
            return word
    tokens = set(re.split(r"[^a-z0-9]+", haystack))
    hit = tokens & DENY_TOKENS
    return next(iter(hit)) if hit else None


def url_pattern(url: str) -> str:
    parts = urlsplit(url)
    path = _NUM.sub("{n}", _GUID.sub("{id}", parts.path.lower()))
    keys = sorted(k for k, _ in parse_qsl(parts.query))
    return f"{path}?{'&'.join(keys)}" if keys else path


async def explore(settings: Settings, max_pages: int = 80) -> dict:
    out = settings.explore_dir
    if out.exists():
        shutil.rmtree(out)
    (out / "pages").mkdir(parents=True)
    (out / "json").mkdir()

    async with Campus(settings, block_assets=False) as campus:
        # 1) Önce HAR'sız bağlamda giriş: şifre içeren login isteği hiçbir kayda girmesin
        login_page = await campus.context.new_page()
        await campus.open_home(login_page)
        await campus.context.close()

        # 2) Kayıtlı oturumla, HAR kaydı açık yeni bağlam
        ctx = await campus.new_context(record_har_path=str(out / "trafik.har"), record_har_content="embed")
        campus.context = ctx
        captured: list[dict] = []
        ctx.on("response", lambda r: asyncio.ensure_future(_capture_json(r, out, captured)))

        page = await ctx.new_page()
        pages, skipped = await _crawl(page, max_pages, out)
        await asyncio.sleep(1)  # bekleyen yanıt yakalamaları bitsin
        await campus.save_session(ctx)

    index = {"pages": pages, "skipped_links": skipped, "json_responses": captured}
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


async def _crawl(page: Page, max_pages: int, out) -> tuple[list[dict], list[dict]]:
    queue: deque[tuple[str, str, int]] = deque([(STUDENT_URL + "/", "ana sayfa", 0)])
    seen: set[str] = set()
    pattern_counts: Counter[str] = Counter()
    pages: list[dict] = []
    skipped: dict[str, dict] = {}

    while queue and len(pages) < max_pages:
        url, link_text, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        pattern = url_pattern(url)
        if pattern_counts[pattern] >= PER_PATTERN_LIMIT:
            continue
        pattern_counts[pattern] += 1

        n = len(pages) + 1
        try:
            response = await page.goto(url, wait_until="networkidle", timeout=45_000)
        except PlaywrightError as e:
            log.warning("Açılamadı: %s (%s)", url, str(e).splitlines()[0])
            pages.append({"n": n, "url": url, "error": str(e).splitlines()[0]})
            continue
        if is_login_url(page.url):
            log.error("Oturum keşif sırasında düştü, durduruluyor: %s", url)
            pages.append({"n": n, "url": url, "error": "oturum düştü"})
            break

        stem = out / "pages" / f"{n:03d}"
        title = await page.title()
        stem.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        stem.with_suffix(".txt").write_text(text, encoding="utf-8")
        try:
            await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        except PlaywrightError:
            pass

        links = await page.evaluate(
            """() => [...document.querySelectorAll('a[href]')].map(a => ({
                href: a.href, text: (a.innerText || a.title || a.getAttribute('aria-label') || '').trim().slice(0, 120)
            }))"""
        )
        new_links = 0
        for link in links:
            href = urldefrag(urljoin(page.url, link["href"]))[0]
            if not href.startswith(STUDENT_URL) or href in seen:
                continue
            reason = deny_reason(href, link["text"])
            if reason:
                skipped.setdefault(href, {"url": href, "text": link["text"], "reason": reason, "from": n})
                continue
            queue.append((href, link["text"], depth + 1))
            new_links += 1

        pages.append({
            "n": n, "url": page.url, "requested": url, "status": response.status if response else None,
            "title": title, "link_text": link_text, "depth": depth, "pattern": pattern, "links": new_links,
        })
        log.info("[%d/%d] %s — %s", n, max_pages, title, page.url)
        await page.wait_for_timeout(random.randint(1000, 2000))

    return pages, list(skipped.values())


async def _capture_json(response: Response, out, captured: list[dict]) -> None:
    try:
        content_type = response.headers.get("content-type", "")
        host = urlsplit(response.url).hostname or ""
        if "json" not in content_type or not host.endswith("ticaret.edu.tr"):
            return
        body = await response.text()
    except PlaywrightError:
        return
    n = len(captured) + 1
    request = response.request
    (out / "json" / f"{n:03d}.json").write_text(body, encoding="utf-8")
    captured.append({
        "n": n, "url": response.url, "method": request.method, "status": response.status,
        "post_keys": sorted(_post_keys(request.post_data)), "bytes": len(body),
    })


def _post_keys(post_data: str | None) -> set[str]:
    if not post_data:
        return set()
    try:
        data = json.loads(post_data)
        return set(data) if isinstance(data, dict) else set()
    except json.JSONDecodeError:
        return {k for k, _ in parse_qsl(post_data)}

"""Bir tarama turu: siteden okur, ayrıştırır ve algılama katmanına verilecek Scan'i kurar.

Kaynaklar ve rolleri:
  /Course                 ders listesi (her şeyin başlangıcı; okunamazsa tur başarısız)
  /Course/Details/{id}    ödevlerin varlığı, teslim durumu, notlar, ders materyalleri
  /Schedule/Data          ödevlerin tam tarihleri ve açıklamaları, canlı dersler, sınavlar, diğer etkinlikler
  /Announce/Refresh       duyurular (sitenin "yenile" butonu; listeyi güncelleyip gösterir)

Sayfalar tarayıcı bağlamının istek API'siyle (aynı çerezler) çekilir; JavaScript gerekmez. Oturum düşmüşse
tarayıcıyla tek denemelik login yapılır ve tur bir kez tekrarlanır.

Asla açılmayan adresler: /content/enroll (içeriği "görüldü" işaretler), sınav başlatma, teslim, yükleme.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

from . import parse as P
from .browser import AuthError, Campus, is_login_url
from .config import STUDENT_URL, Settings
from .models import PRIMARY, SECONDARY, Item, Scan, normalize

log = logging.getLogger(__name__)

SCHEDULE_PAST = timedelta(days=150)
SCHEDULE_FUTURE = timedelta(days=200)
FORBIDDEN_PATHS = ("/content/enroll", "/exam/start", "/assignment/upload", "/account/logout")
DEBUG_KEEP = 30


class SessionExpired(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass
class ScanReport:
    """Taramanın kullanıcıya/bota dönük özeti (Scan'den bağımsız ek bilgiler)."""

    courses: list[P.Course] = field(default_factory=list)
    details: dict[str, P.CourseDetail] = field(default_factory=dict)
    calendar: list[P.CalendarEntry] = field(default_factory=list)
    parse_errors: dict[str, str] = field(default_factory=dict)   # kapsam → ParseError mesajı
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Scanner:
    def __init__(self, campus: Campus, settings: Settings):
        self.campus = campus
        self.s = settings
        self._page = None

    # ── HTTP ──────────────────────────────────────────────────────────────
    async def get(self, path: str) -> str:
        if any(path.lower().startswith(p) for p in FORBIDDEN_PATHS):
            raise ValueError(f"yasaklı adres: {path}")
        url = path if path.startswith("http") else STUDENT_URL + path
        try:
            response = await self.campus.context.request.get(url, timeout=45_000, max_redirects=10)
            body = await response.text()
        except PlaywrightError as e:
            raise FetchError(f"{path}: {str(e).splitlines()[0]}") from None
        if is_login_url(response.url) or 'name="hiddenform"' in body or "/connect/authorize" in body[:3000]:
            raise SessionExpired(path)
        if response.status >= 400:
            raise FetchError(f"{path}: HTTP {response.status}")
        await asyncio.sleep(random.uniform(0.3, 0.9))  # siteyi yormamak için
        return body

    async def ensure_session(self) -> None:
        """Oturumu tarayıcıyla tazeler (gerekirse tek denemelik login). AuthError dışarı fırlar."""
        if self._page is None or self._page.is_closed():
            self._page = await self.campus.context.new_page()
        await self.campus.open_home(self._page)

    async def get_with_session(self, path: str) -> str:
        try:
            return await self.get(path)
        except SessionExpired:
            log.info("Oturum düşmüş, yenileniyor")
            await self.ensure_session()
            return await self.get(path)

    # ── Tarama ────────────────────────────────────────────────────────────
    async def scan(self) -> tuple[Scan, ScanReport]:
        report = ScanReport(started_at=datetime.now(timezone.utc))
        scan = Scan()

        course_html = await self.get_with_session("/Course")  # AuthError/FetchError dışarı fırlar
        try:
            report.courses = P.parse_course_list(course_html)
        except P.ParseError as e:
            self._save_debug("courses", course_html)
            raise FetchError(f"Ders listesi okunamadı: {e}") from None

        for course in report.courses:
            try:
                html = await self.get_with_session(f"/Course/Details/{course.id}")
                report.details[course.id] = P.parse_course_detail(html, course.id)
            except (FetchError, P.ParseError) as e:
                if isinstance(e, P.ParseError):
                    self._save_debug(f"course-{course.id}", html)
                    report.parse_errors[f"course:{course.id}"] = str(e)
                for kind in ("assignment", "grade", "file"):
                    scan.failed_scopes[(kind, f"course:{course.id}")] = str(e)

        now = datetime.now(timezone.utc)
        start = (now - SCHEDULE_PAST).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (now + SCHEDULE_FUTURE).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        schedule_ok = False
        try:
            body = await self.get_with_session(f"/Schedule/Data?start={start}&end={end}")
            report.calendar = P.parse_schedule(body, self.s.tz)
            schedule_ok = True
        except (FetchError, P.ParseError) as e:
            if isinstance(e, P.ParseError):
                self._save_debug("schedule", body, suffix=".json")
                report.parse_errors["calendar"] = str(e)
            for kind in ("live", "event"):
                scan.failed_scopes[(kind, "calendar")] = str(e)

        announcements: list[P.Announcement] = []
        try:
            html = await self.get_with_session("/Announce/Refresh")
            announcements = P.parse_announcements(html)
            scan.ok_scopes.add(("announcement", "announce"))
        except (FetchError, P.ParseError) as e:
            if isinstance(e, P.ParseError):
                self._save_debug("announce", html)
                report.parse_errors["announce"] = str(e)
            scan.failed_scopes[("announcement", "announce")] = str(e)

        scan.items = build_items(report, announcements, schedule_ok)
        for course_id in report.details:
            for kind in ("assignment", "grade", "file"):
                scan.ok_scopes.add((kind, f"course:{course_id}"))
        if schedule_ok:
            scan.ok_scopes |= {("live", "calendar"), ("event", "calendar")}
        report.finished_at = datetime.now(timezone.utc)
        return scan, report

    def _save_debug(self, name: str, content: str, suffix: str = ".html") -> None:
        """Tanınmayan sayfayı sonradan incelemek için sakla (sadece son DEBUG_KEEP dosya)."""
        folder: Path = self.s.data_dir / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (folder / f"{stamp}-{name}{suffix}").write_text(content or "", encoding="utf-8")
        for old in sorted(folder.iterdir())[:-DEBUG_KEEP]:
            old.unlink(missing_ok=True)


def build_items(report: ScanReport, announcements: list[P.Announcement], schedule_ok: bool) -> list[Item]:
    """Kaynakları birleştirip Item listesi kurar. Saf fonksiyon (test edilebilir)."""
    items: list[Item] = []
    by_name = {normalize(c.name): c for c in report.courses}
    names = {c.id: c.name for c in report.courses}
    codes = {c.id: c.code for c in report.courses}
    calendar_assignments = {e.uid: e for e in report.calendar if e.kind == "assignment"}
    seen_assignments: set[str] = set()

    def course_scope(course_name: str) -> tuple[str, str]:
        course = by_name.get(normalize(course_name))
        return (f"course:{course.id}", course.name) if course else (f"course:?{normalize(course_name)}", course_name)

    # Ders sayfasındaki aktiviteler: ödev varlığı, teslim durumu, notlar
    for course_id, detail in report.details.items():
        scope = f"course:{course_id}"
        course_name = names.get(course_id, detail.name)
        for act in detail.activities:
            if act.kind == "assignment":
                entry = calendar_assignments.get(act.id)
                seen_assignments.add(act.id)
                items.append(_assignment(act.id, scope, course_name, codes.get(course_id, ""), entry, act))
            if act.grade is not None:
                items.append(Item(
                    kind="grade", uid=f"{act.kind}:{act.id}", scope=scope, title=act.title, course=course_name,
                    url=act.url, extra={"value": act.grade}, meta={"activity": act.kind},
                ))
        # Ders materyalleri: içerik açılınca linki/ID'si değiştiği için kimlik = ders + bölüm + başlık
        used: dict[str, int] = {}
        for content in detail.contents:
            base = hashlib.sha1(f"{normalize(content.section)}|{normalize(content.title)}".encode()).hexdigest()[:12]
            used[base] = used.get(base, 0) + 1
            uid = f"{course_id}:{base}" + (f"-{used[base]}" if used[base] > 1 else "")
            items.append(Item(
                kind="file", uid=uid, scope=scope, title=content.title, course=course_name, url=content.url,
                extra={"section": content.section, "type": content.icon},
                meta={"viewed": content.viewed},
            ))

    # Takvim: ders sayfasında görünmeyen ödevler (ders sayfası okunamadıysa vs.), canlı dersler, etkinlikler
    for entry in report.calendar:
        scope, course_name = course_scope(entry.course)
        if entry.kind == "assignment":
            if entry.uid not in seen_assignments:
                course_id = scope.split(":", 1)[1]
                items.append(_assignment(entry.uid, scope, course_name, codes.get(course_id, ""), entry, None))
            continue
        if not entry.start:
            continue
        items.append(Item(
            kind=entry.kind, uid=entry.uid, scope="calendar", title=entry.name or entry.type, course=course_name,
            due_at=entry.start, body=entry.description, url=entry.url,
            extra={"type": entry.type, "end": entry.end.isoformat() if entry.end else None},
        ))

    for a in announcements:
        items.append(Item(
            kind="announcement", uid=a.uid, scope="announce", title=a.title, course=a.course,
            body=a.body, url=a.url, extra={"date": a.date},
        ))
    return items


def _assignment(uid: str, scope: str, course: str, code: str, entry: P.CalendarEntry | None,
                act: P.Activity | None) -> Item:
    meta: dict = {"code": code}
    if act is not None:  # teslim bilgisi sadece ders sayfasında var; yoksa eskisini ezme (store meta'yı birleştirir)
        meta.update(submitted=act.submitted, status=act.status, grade=act.grade)
    if entry is not None:
        return Item(
            kind="assignment", uid=uid, scope=scope, title=entry.name, course=course, due_at=entry.end,
            body=entry.description, url=entry.url, rank=PRIMARY,
            extra={"start": entry.start.isoformat() if entry.start else None}, meta=meta,
        )
    assert act is not None
    return Item(kind="assignment", uid=uid, scope=scope, title=act.title, course=course, url=act.url,
                rank=SECONDARY, meta=meta)


async def with_scanner(settings: Settings, fn):
    """Kısa ömürlü kullanım (CLI) için: tarayıcıyı aç, fn(scanner) çalıştır, kapat."""
    async with Campus(settings) as campus:
        return await fn(Scanner(campus, settings))


__all__ = ["Scanner", "ScanReport", "SessionExpired", "FetchError", "AuthError", "build_items", "with_scanner"]

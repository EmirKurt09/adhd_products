import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ekampus.engine import Engine
from ekampus.jev import JevDecision
from ekampus.store import Store
from ekampus.university import (Notice, SOURCE_URL, UniversityError, UniversityMonitor, notice_key,
                                parse_listing, parse_detail, public_url, relevance_state)


LIST = '''<div class="post-item"><h2 class="post-title"><a href="/duyuru/">Örnek sınav duyurusu</a></h2>
<div class="post-date">Ekim 12, 2026</div><div class="post-excerpt">JEV'e gitmeyecek metin</div></div>'''


def test_listing_reads_title_date_url_and_pagination():
    rows, next_page = parse_listing(LIST + '<a class="next page-numbers" href="/category/duyurular/page/2/">Sonraki</a>')
    assert rows[0].title == "Örnek sınav duyurusu"
    assert rows[0].published == "2026-10-12"
    assert rows[0].url == "https://ticaret.edu.tr/duyuru/"
    assert next_page.endswith("/page/2/")
    assert not hasattr(rows[0], "body")


@pytest.mark.parametrize("html", ["<html>bakım</html>", LIST.replace("Ekim 12, 2026", "belirsiz"),
                                   LIST.replace('class="post-date"', 'class="missing"')])
def test_broken_listing_is_not_silent_baseline(html):
    with pytest.raises(UniversityError):
        parse_listing(html)


def test_detail_extracts_body_and_deduplicated_pdf_only():
    html = '''<nav>Menü</nav><div class="col"><div class="header">tarih</div><h2 class="title">Başlık</h2>
    <p>Öğrenciler için açıklama</p><a href="/uploads/ek.PDF">Program</a>
    <a href="/uploads/ek.PDF#page=1">Tekrar</a><a href="/afis.jpg">Afiş</a></div>
    <h2>Diğer duyurular</h2><a href="/diger.pdf">Başka ek</a>'''
    body, files = parse_detail(html, "https://ticaret.edu.tr/duyuru/")
    assert "Öğrenciler için açıklama" in body
    assert "Menü" not in body and "Diğer duyurular" not in body and "tarih" not in body
    assert len(files) == 1
    assert files[0]["url"] == "https://ticaret.edu.tr/uploads/ek.PDF"


def test_pdf_only_detail_does_not_need_text_extraction():
    body, files = parse_detail('<div><h2 class="title">Başlık</h2><a href="/ek.pdf">Tıklayınız</a></div>',
                               "https://ticaret.edu.tr/duyuru/")
    assert body == "Tıklayınız" and len(files) == 1


@pytest.mark.parametrize("url", ["https://evil.test/a.pdf", "file:///secret", "https://ticaret.edu.tr.evil.test/a",
                                 "https://user:pass@ticaret.edu.tr/a"])
def test_public_url_rejects_other_destinations(url):
    with pytest.raises(UniversityError):
        public_url(url)


NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
OLD = Notice("Eski duyuru", "https://ticaret.edu.tr/eski/", "2026-10-11")
NEW = Notice("Örnek bölüm sınav takvimi", "https://ticaret.edu.tr/yeni/", "2026-10-12")


class Browser:
    def __init__(self):
        self.pages = {SOURCE_URL: ([OLD], None)}
        self.visited = []
        self.details = []
        self.fail_detail = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def listing(self, url):
        self.visited.append(url)
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result

    async def detail(self, url):
        self.details.append(url)
        if self.fail_detail:
            raise UniversityError("Detay erişilemiyor")
        return "Gizli olmayan yapay içerik, JEV'e gönderilmez", [
            {"url": "https://ticaret.edu.tr/ek.pdf", "title": "Yapay PDF eki"}]


class Jev:
    def __init__(self):
        self.requests = []
        self.probability = 0.8
        self.fail = False

    async def ask(self, state, questions):
        self.requests.append((state, questions))
        if self.fail:
            raise UniversityError("JEV erişilemiyor")
        return JevDecision({"relevant": self.probability})


@pytest.fixture
def monitor(settings):
    store = Store(":memory:")
    engine = Engine(settings, store)
    engine.feature_on = lambda key: True
    browser, jev = Browser(), Jev()
    m = UniversityMonitor(settings, store, engine, jev, browser_factory=lambda _: browser)
    m.test_browser, m.test_jev = browser, jev
    yield m
    store.close()


def baseline_then_new(m):
    asyncio.run(m.run(NOW))
    m.test_browser.pages = {SOURCE_URL: ([NEW, OLD], None)}


def test_baseline_is_silent_and_restart_deduplicates(monitor):
    m = monitor
    baseline_then_new(m)
    assert not m.store.pending(NOW) and not m.test_jev.requests and not m.test_browser.details
    asyncio.run(m.run(NOW + timedelta(hours=1)))
    assert len(m.store.pending(NOW + timedelta(hours=1))) == 2  # mesaj + PDF
    restarted = UniversityMonitor(m.settings, m.store, m.engine, m.jev, browser_factory=lambda _: m.test_browser)
    asyncio.run(restarted.run(NOW + timedelta(hours=2)))
    assert len(m.test_jev.requests) == 1 and len(m.test_browser.details) == 1
    assert len(m.store.pending(NOW + timedelta(hours=2))) == 2


def test_jev_receives_only_title_department_and_course_names(monitor):
    m = monitor
    m.store.lesson_add({"code": "BIL999", "name": "Örnek Ders", "weekday": 1, "start": "10:00",
                        "end": "11:00", "room": "Gizli sınıf", "teacher": "Gizli isim"}, NOW)
    baseline_then_new(m)
    asyncio.run(m.run(NOW))
    state, questions = m.test_jev.requests[0]
    assert state == {"announcement_title": NEW.title, "student": {
        "department": "Bilgisayar Mühendisliği", "level": "lisans",
        "courses": [{"code": "BIL999", "name": "Örnek Ders"}]}}
    assert set(questions) == {"relevant"}


@pytest.mark.parametrize("probability,accepted", [(0.59, False), (0.60, True), (0.95, True)])
def test_threshold_rejects_before_fetching_detail(monitor, probability, accepted):
    baseline_then_new(monitor)
    monitor.test_jev.probability = probability
    asyncio.run(monitor.run(NOW))
    assert bool(monitor.test_browser.details) is accepted
    assert bool(monitor.store.pending(NOW)) is accepted


@pytest.mark.parametrize("failure", ["disabled", "error", "invalid"])
def test_jev_unavailable_stays_pending_then_recovers(monitor, failure):
    m = monitor
    baseline_then_new(m)
    m.engine.feature_on = lambda key: failure != "disabled"
    m.test_jev.fail = failure == "error"
    m.test_jev.probability = float("nan") if failure == "invalid" else 0.9
    asyncio.run(m.run(NOW))
    assert m.state()["pending"] == 1 and not m.store.pending(NOW) and not m.test_browser.details
    m.engine.feature_on = lambda key: True
    m.test_jev.fail, m.test_jev.probability = False, 0.9
    asyncio.run(m.run(NOW + timedelta(hours=1)))
    assert m.state()["pending"] == 0 and len(m.store.pending(NOW + timedelta(hours=1))) == 2


def test_detail_failure_retries_without_second_jev_call(monitor):
    m = monitor
    baseline_then_new(m)
    m.test_browser.fail_detail = True
    asyncio.run(m.run(NOW))
    assert m.state()["pending"] == 1 and not m.store.pending(NOW)
    m.test_browser.fail_detail = False
    asyncio.run(m.run(NOW))
    assert len(m.test_jev.requests) == 1 and len(m.store.pending(NOW)) == 2


def test_pagination_and_old_unseen_archive_entries(monitor):
    m = monitor
    baseline_then_new(m)
    page2 = SOURCE_URL + "page/2/"
    archived = Notice("Arşiv", "https://ticaret.edu.tr/arsiv/", "2026-10-01")
    m.test_browser.pages = {SOURCE_URL: ([NEW], page2), page2: ([OLD, archived], None)}
    asyncio.run(m.run(NOW))
    assert page2 in m.test_browser.visited
    assert len(m.test_jev.requests) == 1


def test_incomplete_pagination_does_not_advance_cursor(monitor):
    m = monitor
    baseline_then_new(m)
    previous = m.state()["last_ok"]
    m.test_browser.pages = {SOURCE_URL: ([NEW], "broken"), "broken": UniversityError("ağ")}
    asyncio.run(m.run(NOW + timedelta(hours=1)))
    assert m.state()["last_ok"] == previous
    assert not m.store.get(notice_key(NEW.url))
    assert not m.test_jev.requests


def test_outbox_and_final_record_are_atomic(monitor, monkeypatch):
    m = monitor
    baseline_then_new(m)
    original = m.store._enqueue

    def fail_pdf(key, type_, *args, **kwargs):
        if type_ == "university_pdf":
            raise RuntimeError("disk hatası")
        return original(key, type_, *args, **kwargs)

    monkeypatch.setattr(m.store, "_enqueue", fail_pdf)
    asyncio.run(m.run(NOW))
    assert not m.store.pending(NOW)
    assert m.state()["pending"] == 1
    monkeypatch.setattr(m.store, "_enqueue", original)
    asyncio.run(m.run(NOW))
    assert len(m.store.pending(NOW)) == 2 and len(m.test_jev.requests) == 1

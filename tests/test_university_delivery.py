"""Yeni kaynak, mevcut bildirim politikası ve PDF retry davranışı."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ekampus import bot, prefs
from ekampus.engine import Download, Engine
from ekampus.models import Event
from ekampus.store import Store
from ekampus.university import UniversityBrowser

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)


@pytest.fixture
def engine(settings):
    store = Store(":memory:")
    e = Engine(settings, store)
    payload = {"kind": "announcement", "title": "Örnek akademik duyuru", "body": "Açıklama",
               "url": "https://ticaret.edu.tr/ornek/", "published": "2026-10-12"}
    store.enqueue(Event("university_notice", "u:message", payload), NOW)
    store.enqueue(Event("university_pdf", "u:pdf", {**payload, "parent_key": "u:message",
        "attachment": {"url": "https://ticaret.edu.tr/ek.pdf", "title": "Program"}}), NOW)
    yield e
    store.close()


def test_quiet_mode_holds_both_message_and_pdf(engine):
    engine.set_mute(NOW + timedelta(hours=1))
    send = AsyncMock()
    assert asyncio.run(engine.flush(send, NOW)) == 0
    send.assert_not_awaited()
    assert asyncio.run(engine.flush(send, NOW + timedelta(hours=2))) == 2
    assert [call.args[1]["type"] for call in send.await_args_list] == ["university_notice", "university_pdf"]


def test_disabled_announcements_mutes_both(engine):
    prefs.set_value(engine.store, "announcements", False)
    send = AsyncMock()
    assert asyncio.run(engine.flush(send, NOW)) == 0
    assert engine.store.outbox_by_key("u:message")["status"] == "muted"
    assert engine.store.outbox_by_key("u:pdf")["status"] == "muted"


def test_attachment_waits_until_parent_sent(engine):
    send = AsyncMock(side_effect=RuntimeError("Telegram erişilemiyor"))
    assert asyncio.run(engine.flush(send, NOW)) == 0
    assert send.await_count == 1
    assert engine.store.outbox_by_key("u:pdf")["attempts"] == 0
    send = AsyncMock()
    assert asyncio.run(engine.flush(send, NOW + timedelta(minutes=2))) == 2


def test_attachment_retry_does_not_resend_parent(engine):
    calls = []

    async def fail_pdf(message, ctx):
        calls.append(ctx["type"])
        if ctx["type"] == "university_pdf":
            raise RuntimeError("PDF gönderilemedi")

    assert asyncio.run(engine.flush(fail_pdf, NOW)) == 1
    assert calls == ["university_notice", "university_pdf"]
    send = AsyncMock()
    assert asyncio.run(engine.flush(send, NOW + timedelta(minutes=2))) == 1
    assert send.await_args.args[1]["type"] == "university_pdf"


def test_pdf_is_sent_as_document_through_bot(engine, monkeypatch):
    browser = AsyncMock()
    browser.__aenter__.return_value = browser
    browser.download.return_value = Download("program.pdf", b"%PDF-test", "https://ticaret.edu.tr/ek.pdf")
    telegram = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    c = bot.Ctx(engine.s, engine.store, engine, None)
    ctx = SimpleNamespace(bot=telegram, application=SimpleNamespace(bot_data={
        "ctx": c, "university": SimpleNamespace(browser_factory=lambda _: browser)}))
    monkeypatch.setattr("ekampus.engine.utcnow", lambda: NOW)
    asyncio.run(bot.flush_job(ctx))
    assert telegram.send_message.await_count == 1
    assert telegram.send_document.await_count == 1
    assert telegram.send_document.await_args.kwargs["document"] == b"%PDF-test"
    assert telegram.send_document.await_args.kwargs["filename"] == "program.pdf"
    assert browser.download.await_args.args == ("https://ticaret.edu.tr/ek.pdf", "Program")


def test_large_pdf_falls_back_to_link(engine, monkeypatch):
    browser = AsyncMock()
    browser.__aenter__.return_value = browser
    browser.download.return_value = Download("program.pdf", None, "https://ticaret.edu.tr/ek.pdf")
    telegram = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    ctx = SimpleNamespace(bot=telegram, application=SimpleNamespace(bot_data={
        "ctx": bot.Ctx(engine.s, engine.store, engine, None),
        "university": SimpleNamespace(browser_factory=lambda _: browser)}))
    monkeypatch.setattr("ekampus.engine.utcnow", lambda: NOW)
    asyncio.run(bot.flush_job(ctx))
    assert telegram.send_document.await_count == 0 and telegram.send_message.await_count == 2
    assert "50 MB" in telegram.send_message.await_args.args[1]


def test_schedule_is_two_daily_checks_in_istanbul():
    app = SimpleNamespace(job_queue=Mock())
    bot.schedule_university(app)
    daily = app.job_queue.run_daily.call_args_list
    assert len(daily) == 2
    assert [c.args[1].hour for c in daily] == [9, 17]
    assert all(c.args[1].tzinfo.key == "Europe/Istanbul" for c in daily)
    app.job_queue.run_once.assert_called_once()


@pytest.mark.parametrize("last,baseline,should_run", [
    (None, None, True), ("2026-10-12T05:00:00+00:00", "2026-10-11", True),
    ("2026-10-12T06:00:00+00:00", "2026-10-11", False),
])
def test_startup_catches_up_only_if_needed(monkeypatch, last, baseline, should_run):
    monitor = SimpleNamespace(state=lambda: {"last_attempt": last, "baseline_day": baseline})
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"university": monitor}))
    job = AsyncMock()
    monkeypatch.setattr(bot, "now_utc", lambda: NOW)
    monkeypatch.setattr(bot, "university_job", job)
    asyncio.run(bot.university_start_job(context))
    assert bool(job.await_count) is should_run


def test_download_rejects_html_disguised_as_pdf(settings):
    browser = UniversityBrowser(settings)
    page = SimpleNamespace(url="https://ticaret.edu.tr/", goto=AsyncMock(), close=AsyncMock(),
                           evaluate=AsyncMock(return_value={"url": "https://ticaret.edu.tr/ek.pdf",
                               "data": base64.b64encode(b"<html>bakim</html>").decode()}))
    browser.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    with pytest.raises(Exception, match="PDF döndürmedi"):
        asyncio.run(browser.download("https://ticaret.edu.tr/ek.pdf", "Ek"))
    page.close.assert_awaited_once()


@pytest.mark.parametrize("data,expected", [(b"%PDF-example", b"%PDF-example"), (None, None)])
def test_download_uses_chromium_and_accepts_size_fallback(settings, data, expected):
    browser = UniversityBrowser(settings)
    page = SimpleNamespace(url="https://ticaret.edu.tr/", goto=AsyncMock(), close=AsyncMock(),
                           evaluate=AsyncMock(return_value={"url": "https://ticaret.edu.tr/ek.pdf",
                               "data": base64.b64encode(data).decode() if data is not None else None}))
    browser.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    result = asyncio.run(browser.download("https://ticaret.edu.tr/ek.pdf", "Ek"))
    assert result.body == expected and result.filename == "Ek.pdf"
    assert page.evaluate.await_args.args[1]["limit"] == 50 * 1024 * 1024
    page.close.assert_awaited_once()

"""Uyarı yöneticisi tercihleri ve site erişim uyarıları."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from ekampus import messages as M
from ekampus import prefs as PR
from ekampus.engine import Engine, describe_failure
from ekampus.models import Event
from ekampus.store import Store

DAY = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)      # İstanbul 12:00
NIGHT = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)   # İstanbul 03:30


@pytest.fixture
def engine(settings):
    store = Store(":memory:")
    yield Engine(settings, store)
    store.close()


def sent_texts(engine, now):
    texts = []

    async def send(message, ctx):
        texts.append(message.text)

    asyncio.run(engine.flush(send, now))
    return texts


def test_disabled_category_is_muted_not_queued(engine):
    PR.toggle(engine.store, "files")
    engine.store.enqueue(Event("new", "new:file:1", {"kind": "file", "uid": "1", "title": "lecture"}), DAY)
    engine.store.enqueue(Event("new", "new:assignment:1", {"kind": "assignment", "uid": "1", "title": "Ödev"}), DAY)
    texts = sent_texts(engine, DAY)
    assert len(texts) == 1 and "Yeni ödev" in texts[0]
    assert engine.store.outbox_by_key("new:file:1")["status"] == "muted"
    PR.toggle(engine.store, "files")  # tekrar açmak eskileri geri getirmez
    assert sent_texts(engine, DAY) == []


def test_critical_auth_alert_cannot_be_disabled(engine):
    PR.toggle(engine.store, "system")
    engine.alert("auth:x", "giriş reddedildi", DAY, critical=True)
    engine.alert("parse:x", "yapı değişti", DAY)
    assert sent_texts(engine, DAY) == ["giriş reddedildi"]


def test_categories():
    assert PR.category_of("new", {"kind": "assignment"}) == "assignment"
    assert PR.category_of("due_changed", {"kind": "assignment"}) == "changes"
    assert PR.category_of("due_changed", {"kind": "live"}) == "calendar"
    assert PR.category_of("reminder", {}) == "reminders"
    assert PR.category_of("baseline", {}) is None


def test_fail_after_cycles(engine):
    assert PR.load(engine.store)["fail_after"] == 2
    assert [PR.toggle(engine.store, "fail_after")["fail_after"] for _ in range(4)] == [3, 5, 1, 2]


def test_access_failure_alert_after_threshold_with_reason(engine):
    PR.toggle(engine.store, "fail_after")
    PR.toggle(engine.store, "fail_after")
    PR.toggle(engine.store, "fail_after")  # → 1
    engine.store.set("last_ok_at", (DAY - timedelta(hours=1)).isoformat())
    engine._failure(DAY, "FetchError: /Course: HTTP 503", now=DAY)
    texts = sent_texts(engine, DAY)
    assert len(texts) == 1
    assert "girilemiyor" in texts[0] and "sunucu hatası" in texts[0] and "Son başarılı kontrol" in texts[0]
    engine._failure(DAY, "FetchError: /Course: HTTP 503", now=DAY)
    assert sent_texts(engine, DAY + timedelta(minutes=5)) == []  # aynı kesinti için tek uyarı


def test_recovery_reports_downtime(engine):
    engine._failure(DAY, "timeout", now=DAY)
    engine._failure(DAY, "timeout", now=DAY)
    assert len(sent_texts(engine, DAY)) == 1
    engine.store.set("fail_since", (DAY - timedelta(minutes=50)).isoformat())
    engine.store.db.execute("UPDATE outbox SET key = ? WHERE type = 'alert'", (f"alert:fail:{engine.store.get('fail_since')}",))
    engine._recovered(DAY)
    texts = sent_texts(engine, DAY)
    assert len(texts) == 1 and "yeniden ulaşılıyor" in texts[0]


def test_night_outage_that_recovers_before_morning_is_silent(engine):
    engine._failure(NIGHT, "timeout", now=NIGHT)
    engine._failure(NIGHT, "timeout", now=NIGHT)
    assert sent_texts(engine, NIGHT) == []        # gece bekletildi
    engine._recovered(NIGHT + timedelta(hours=1))  # sabahtan önce düzeldi
    assert sent_texts(engine, DAY) == []           # hiç rahatsız etme


def test_night_mode_toggle(engine):
    engine.store.enqueue(Event("new", "new:assignment:1", {"kind": "assignment", "uid": "1", "title": "Ödev"}), NIGHT)
    assert sent_texts(engine, NIGHT) == []
    PR.toggle(engine.store, "night")
    assert len(sent_texts(engine, NIGHT)) == 1


def test_describe_failure():
    assert "zaman aşımı" in describe_failure("AuthError: Siteye ulaşılamadı: Timeout 60000ms exceeded")
    assert "İnternet" in describe_failure("net::ERR_NAME_NOT_RESOLVED")
    assert "bakım" in describe_failure("FetchError: Ders listesi okunamadı: tablo yok")


def test_manager_view_renders(engine, settings):
    status = {"last_ok": DAY - timedelta(minutes=3), "fail_streak": 0, "pending": 0, "sent_24h": 2}
    view = M.alert_manager_view(status, PR.load(engine.store), PR.CATEGORIES, settings.tz, DAY, "01:00–07:00")
    assert "Uyarı yöneticisi" in view.text and "3 dk önce" in view.text
    data = [b.data for row in view.buttons for b in row]
    assert "pref:assignment" in data and "pref:fail_after" in data and "mute:morning" in data
    assert all(len(d.encode()) <= 64 for d in data)  # Telegram callback sınırı

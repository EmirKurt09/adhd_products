"""Sessiz modun seviyeleri ve kişisel hatırlatmalar (outbox'ta zamanı gelince giden 'note' olayları)."""

import asyncio
from datetime import datetime, timedelta, timezone

from ekampus import messages as M
from ekampus import prefs as PR
from ekampus.engine import Engine
from ekampus.models import Event
from ekampus.store import Store

DAY = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)  # İstanbul 12:00, gece değil


def make(settings) -> Engine:
    return Engine(settings, Store(":memory:"))


def flush(engine, now=DAY) -> list[str]:
    sent: list[str] = []

    async def send(message, ctx):
        sent.append(message.text)

    asyncio.run(engine.flush(send, now))
    return sent


def fill(engine) -> None:
    engine.store.enqueue(Event("new", "new:announcement:1", {"kind": "announcement", "uid": "1", "title": "Duyuru"}), DAY)
    engine.store.enqueue(Event("reminder", "reminder:3h:5", {"kind": "assignment", "uid": "5", "title": "Rapor",
                                                              "due_at": (DAY + timedelta(hours=3)).isoformat(),
                                                              "hours": 3}), DAY)
    engine.alert("auth", "giriş reddedildi", DAY, critical=True, priority=2)


def test_normal_quiet_lets_urgent_through(settings):
    engine = make(settings)
    engine.set_mute(DAY + timedelta(hours=2))
    fill(engine)
    sent = flush(engine)
    assert len(sent) == 2 and not any("Duyuru" in t for t in sent)  # 3 saat kala hatırlatma + kritik uyarı


def test_full_quiet_holds_everything_but_critical(settings):
    engine = make(settings)
    engine.set_mute(DAY + timedelta(hours=2), allow_urgent=False)
    assert engine.hold_reason(DAY) == "tam sessiz" and engine.mute_full()
    fill(engine)
    assert flush(engine) == ["giriş reddedildi"]
    later = flush(engine, DAY + timedelta(hours=2, minutes=1))  # sessizlik bitince bekleyenler gelir
    assert len(later) == 2


def test_turning_quiet_off_resets_level(settings):
    engine = make(settings)
    engine.set_mute(DAY + timedelta(hours=2), allow_urgent=False)
    engine.set_mute(None)
    assert not engine.mute_full() and engine.hold_reason(DAY) is None


def test_note_waits_for_its_time(settings):
    engine = make(settings)
    at = DAY + timedelta(hours=3)
    note_id = engine.schedule_note("Ağlar raporunu yükle", at, DAY)
    assert note_id is not None and engine.schedule_note("Ağlar raporunu yükle", at, DAY) is None  # tekrar girmez
    assert flush(engine) == []
    assert engine.store.outbox_stats()["pending"] == 0  # kurulmuş hatırlatma "bekleyen bildirim" sayılmaz
    assert [n["text"] for n in engine.store.notes_pending()] == ["Ağlar raporunu yükle"]
    assert flush(engine, at + timedelta(seconds=30)) == ["<b>Hatırlatma:</b> Ağlar raporunu yükle"]
    assert engine.store.notes_pending() == []


def test_note_ignores_category_toggles_and_normal_quiet(settings):
    engine = make(settings)
    PR.toggle(engine.store, "reminders")  # teslim hatırlatmaları kapalı; kişisel hatırlatma yine gelir
    engine.set_mute(DAY + timedelta(hours=5))
    engine.schedule_note("ilaç", DAY + timedelta(hours=1), DAY)
    assert flush(engine, DAY + timedelta(hours=1)) == ["<b>Hatırlatma:</b> ilaç"]


def test_cancelled_note_is_not_sent(settings):
    engine = make(settings)
    note_id = engine.schedule_note("x", DAY + timedelta(hours=1), DAY)
    assert engine.store.cancel_note(note_id) and not engine.store.cancel_note(note_id)
    assert flush(engine, DAY + timedelta(hours=2)) == []


def test_notes_view(settings):
    engine = make(settings)
    engine.schedule_note("Rapor <son> hali", DAY + timedelta(hours=26), DAY)
    view = M.notes_view(engine.store.notes_pending(), settings.tz, DAY)
    assert "Rapor &lt;son&gt; hali" in view.text and view.buttons[0][0].data.startswith("rem:del:")
    assert "Kurulu hatırlatma yok" in M.notes_view([], settings.tz, DAY).text


def test_manager_view_shows_quiet_level(settings):
    status = {"muted_until": DAY + timedelta(hours=1), "mute_full": True}
    view = M.alert_manager_view(status, PR.DEFAULTS, PR.CATEGORIES, settings.tz, DAY, "01:00–07:00")
    assert "tam sessiz" in view.text

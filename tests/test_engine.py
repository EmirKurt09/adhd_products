"""Gönderim politikası: gruplama, gece/sessiz modu, acil olanlar, hata sonrası tekrar, mesaj biçimleri."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ekampus import messages as M
from ekampus.engine import Engine
from ekampus.lock import InstanceLock
from ekampus.models import Event
from ekampus.store import Store

DAY = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)     # İstanbul 12:00
NIGHT = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)  # İstanbul 03:30
DUE = "2026-10-10T20:59+00:00"


@pytest.fixture
def engine(settings):
    store = Store(":memory:")
    yield Engine(settings, store)
    store.close()


def new(kind: str, uid: str, title: str = "Başlık") -> Event:
    return Event("new", f"new:{kind}:{uid}", {"kind": kind, "uid": uid, "title": title, "course": "Ders",
                                              "due_at": DUE, "url": "https://student.ekampus.ticaret.edu.tr/x"})


class Recorder:
    def __init__(self, fail: bool = False):
        self.sent: list[M.Message] = []
        self.fail = fail

    async def __call__(self, message, ctx):
        if self.fail:
            raise RuntimeError("ağ yok")
        self.sent.append(message)


def flush(engine, now, send=None):
    send = send or Recorder()
    count = asyncio.run(engine.flush(send, now))
    return send, count


def test_many_same_type_events_are_grouped(engine):
    for i in range(5):
        engine.store.enqueue(new("file", str(i), f"Hafta {i}"), DAY)
    engine.store.enqueue(new("assignment", "a", "Ödev"), DAY)
    send, count = flush(engine, DAY)
    assert count == 6
    assert len(send.sent) == 2
    assert any("5 yeni ders materyali" in m.text for m in send.sent)


def test_night_holds_non_urgent_but_sends_alerts(engine):
    engine.store.enqueue(new("assignment", "a"), NIGHT)
    engine.store.enqueue(Event("alert", "alert:x", {"text": "giriş reddedildi"}), NIGHT)
    send, count = flush(engine, NIGHT)
    assert [m.text for m in send.sent] == ["giriş reddedildi"]
    send, count = flush(engine, DAY)  # sabah bekleyen gelir
    assert count == 1 and "Yeni ödev" in send.sent[0].text


def test_mute_holds_until_expiry(engine):
    engine.set_mute(DAY + timedelta(hours=2))
    engine.store.enqueue(new("announcement", "d"), DAY)
    assert flush(engine, DAY)[1] == 0
    assert flush(engine, DAY + timedelta(hours=3))[1] == 1


def test_failed_send_is_retried_not_lost_or_duplicated(engine):
    engine.store.enqueue(new("assignment", "a"), DAY)
    _, count = flush(engine, DAY, Recorder(fail=True))
    assert count == 0
    assert flush(engine, DAY)[1] == 0                         # backoff süresinde tekrar denenmez
    send, count = flush(engine, DAY + timedelta(minutes=2))
    assert count == 1
    assert flush(engine, DAY + timedelta(hours=1))[1] == 0     # bir kez gönderildi, bitti


def test_concurrent_flushes_do_not_duplicate(engine):
    engine.store.enqueue(new("assignment", "a"), DAY)
    send = Recorder()

    async def both():
        await asyncio.gather(engine.flush(send, DAY), engine.flush(send, DAY))

    asyncio.run(both())
    assert len(send.sent) == 1


def test_reminders_flow_into_outbox(engine, settings):
    from ekampus.detect import diff
    from ekampus.models import Item, Scan

    due = DAY + timedelta(hours=20)
    item = Item(kind="assignment", uid="a", scope="course:1", title="Ödev", due_at=due, meta={"submitted": False})
    sc = Scan(items=[item], ok_scopes={("assignment", "course:1")})
    engine.store.apply(sc, diff({}, set(), sc), DAY - timedelta(days=3))
    assert engine.plan_reminders(DAY) == 1
    send, _ = flush(engine, DAY)
    assert any("24 saat kaldı" in m.text for m in send.sent)
    engine.store.set_done("assignment", "a")
    assert engine.plan_reminders(due - timedelta(hours=2)) == 0


@pytest.mark.parametrize("event_type,data", [
    ("new", {"kind": "assignment", "uid": "1", "title": "Ö<1>", "due_at": DUE, "body": "x" * 900, "extra": {"start": DUE}}),
    ("new", {"kind": "announcement", "uid": "2", "title": "Duyuru", "body": "metin"}),
    ("new", {"kind": "grade", "uid": "3", "title": "Ödev", "extra": {"value": "99,00"}}),
    ("new", {"kind": "file", "uid": "4", "title": "lecture", "extra": {"section": "Genel"}}),
    ("new", {"kind": "live", "uid": "5", "title": "Canlı", "due_at": DUE}),
    ("new", {"kind": "event", "uid": "6", "title": "Vize", "due_at": DUE, "extra": {"type": "exam"}}),
    ("due_changed", {"kind": "assignment", "uid": "1", "title": "Ödev", "due_at": DUE, "old_due_at": "2026-10-09T20:59+00:00", "content_changed": True}),
    ("due_changed", {"kind": "assignment", "uid": "1", "title": "Ödev", "due_at": DUE, "old_due_at": None}),
    ("changed", {"kind": "grade", "uid": "3", "title": "Ödev", "extra": {"value": "100"}}),
    ("changed", {"kind": "announcement", "uid": "2", "title": "Duyuru", "body": "yeni"}),
    ("reminder", {"kind": "assignment", "uid": "1", "title": "Ödev", "due_at": DUE, "hours": 3}),
    ("live_soon", {"kind": "live", "uid": "5", "title": "Canlı", "due_at": DUE, "url": "https://x"}),
    ("scope_added", {"kind": "file", "scope": "course:9", "course": "Yeni Ders", "items": [{"title": "a"}] * 12}),
    ("baseline", {"counts": {"assignment": 2}, "open_assignments": [{"title": "Ö", "due_at": DUE}], "failed_scopes": {}}),
    ("alert", {"text": "uyarı"}),
])
def test_every_event_renders(event_type, data):
    message = M.render_event(event_type, data, M.ZoneInfo("Europe/Istanbul"), DAY)
    assert message.text and len(message.text) < 4096
    assert "<1>" not in message.text  # HTML kaçışı


def test_time_formatting():
    tz = M.ZoneInfo("Europe/Istanbul")
    assert M.fmt_dt(datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc), tz, DAY) == "bugün 13:00"
    assert M.fmt_dt(datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc), tz, DAY) == "yarın 13:00"
    assert M.fmt_dt(datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc), tz, DAY) == "14 Eki Çar 13:00"
    assert M.remaining(DAY + timedelta(hours=5, minutes=10), DAY) == "5 sa 10 dk kaldı"
    assert M.remaining(DAY + timedelta(days=3), DAY) == "3 gün kaldı"
    assert M.remaining(DAY - timedelta(minutes=1), DAY) == "süresi geçti"


def test_instance_lock(tmp_path):
    first = InstanceLock(tmp_path / "bot.lock")
    assert first.acquire()
    assert not InstanceLock(tmp_path / "bot.lock").acquire()
    first.release()
    assert InstanceLock(tmp_path / "bot.lock").acquire()


EMOJI = __import__("re").compile("[\U0001F000-\U0001FAFF☀-➿⌀-⏿⬀-⯿]")


def test_no_decorative_emoji_anywhere(settings):
    """Kullanıcı isteği: menülerde ve mesajlarda süs emojisi olmasın."""
    from ekampus import prefs as PR

    tz = settings.tz
    from ekampus import features as F

    texts = [M.help_text(None), M.help_text("ayarlardan kapalı")]
    view = M.settings_view(F.states(settings, None), memory_count=2, reminder_count=1)
    texts += [view.text] + [b.text for row in view.buttons for b in row]
    for event_type, data in [
        ("new", {"kind": k, "uid": "1", "title": "x", "due_at": DUE, "url": "https://x", "extra": {"value": "1", "section": "s"}})
        for k in ("assignment", "announcement", "grade", "file", "live", "event")
    ] + [("due_changed", {"kind": "assignment", "uid": "1", "title": "x", "due_at": DUE, "old_due_at": DUE}),
         ("reminder", {"kind": "assignment", "uid": "1", "title": "x", "due_at": DUE, "hours": 3}),
         ("live_soon", {"kind": "live", "uid": "1", "title": "x", "due_at": DUE, "url": "https://x"}),
         ("baseline", {"counts": {}, "open_assignments": [{"title": "x", "due_at": DUE}]})]:
        m = M.render_event(event_type, data, tz, DAY)
        texts += [m.text] + [b.text for row in m.buttons for b in row]
    view = M.alert_manager_view({"fail_streak": 2, "fail_reason": "x", "guard_blocked": True, "muted_until": DAY + timedelta(hours=1)},
                                PR.DEFAULTS, PR.CATEGORIES, tz, DAY, "01:00–07:00")
    texts += [view.text] + [b.text for row in view.buttons for b in row]
    texts.append(M.digest_view([], {"file": [1]}, tz, DAY, None).text)
    from ekampus import bot as B
    texts += [label for row in B.KEYBOARD.keyboard for label in (getattr(b, "text", b) for b in row)]
    offenders = [t for t in texts if EMOJI.search(t)]
    assert offenders == []

"""Ders programı: saat hesapları, "şu anki ders", derse çıkış planı ve store kayıtları."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ekampus import prefs as PR
from ekampus import timetable as TT
from ekampus.engine import Engine
from ekampus.models import Event
from ekampus.store import Store

TZ = ZoneInfo("Europe/Istanbul")
TUESDAY = datetime(2026, 10, 13, tzinfo=TZ)  # Salı


def at(day_offset: int, clock: str) -> datetime:
    h, m = map(int, clock.split(":"))
    return (TUESDAY + timedelta(days=day_offset)).replace(hour=h, minute=m)


SCHEDULE = [
    {"code": "BIL463", "name": "Bulanık Mantık", "weekday": "Salı", "start": "13:00", "end": "15:50", "room": "B-111"},
    {"code": "GNL390", "name": "Yapay Zekâ ve Büyük Veri ile Finans", "weekday": "salı", "start": "17:00",
     "end": "18:50", "room": "C-307", "campus": "Küçükyalı"},
    {"code": "BIL469", "name": "Sistem Programlama", "weekday": "Çarşamba", "start": "9:00", "end": "11:50",
     "room": "A-127"},
    {"code": "KRP", "name": "Kariyer Planlama", "weekday": "Persembe", "start": "16:40", "end": "17:30"},
    {"code": "BIL453", "name": "Bilgisayar Sistemleri Lab.", "weekday": 3, "start": "18:00", "end": "19:50",
     "room": "C-205"},
    {"code": "BIL441", "name": "Computer Networks", "weekday": "cuma", "start": "09:00", "end": "11:50",
     "room": "B-212"},
]


def store_with_schedule() -> Store:
    store = Store(":memory:")
    for fields in SCHEDULE:
        store.lesson_add(fields, TUESDAY)
    return store


def by_code(store: Store, code: str) -> dict:
    return next(l for l in store.lessons() if l["code"] == code)


# ── Ayrıştırma ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,day", [("Salı", 1), ("sali", 1), ("ÇARŞAMBA", 2), ("cuma", 4), ("Cumartesi", 5),
                                      ("pazar", 6), ("Pazartesi", 0), ("pzt", 0), (3, 3), ("per", 3)])
def test_weekday_names(text, day):
    assert TT.parse_weekday(text) == day


def test_bad_values_rejected():
    with pytest.raises(ValueError):
        TT.parse_weekday("haftaiçi")
    with pytest.raises(ValueError):
        TT.parse_clock("25:00")
    store = Store(":memory:")
    with pytest.raises(ValueError):
        store.lesson_add({"code": "X", "name": "X", "weekday": 1, "start": "15:00", "end": "14:00"}, TUESDAY)
    with pytest.raises(ValueError):
        store.lesson_add({"code": "X", "weekday": 1, "start": "15:00", "end": "16:00"}, TUESDAY)


def test_add_is_idempotent_and_normalized():
    store = store_with_schedule()
    lesson_id, new = store.lesson_add(SCHEDULE[0], TUESDAY)
    assert not new and len(store.lessons()) == 6
    assert by_code(store, "BIL469")["start"] == "09:00"


# ── Şu anki / sıradaki ders ──────────────────────────────────────────────
def test_current_lesson_during_class():
    lessons = store_with_schedule().lessons()
    current, following = TT.now_or_next(lessons, at(0, "14:10"), TZ)
    assert current.lesson["code"] == "BIL463" and following.lesson["code"] == "GNL390"


def test_next_lesson_between_classes():
    lessons = store_with_schedule().lessons()
    current, following = TT.now_or_next(lessons, at(0, "16:10"), TZ)
    assert current is None and following.lesson["room"] == "C-307"


def test_friday_evening_rolls_over_to_next_week():
    lessons = store_with_schedule().lessons()
    current, following = TT.now_or_next(lessons, at(3, "20:00"), TZ)
    assert current is None
    assert following.lesson["code"] == "BIL463" and following.start == at(7, "13:00")


def test_find_ignores_case_and_turkish_letters():
    lessons = store_with_schedule().lessons()
    assert [l["code"] for l in TT.find(lessons, "bulanik mantik")] == ["BIL463"]
    assert [l["code"] for l in TT.find(lessons, "bil 453")] == ["BIL453"]
    assert [l["code"] for l in TT.find(lessons, "kariyer")] == ["KRP"]


# ── Derse çıkış planı ────────────────────────────────────────────────────
def planned(store: Store, now: datetime) -> list[tuple[str, datetime]]:
    skips = store.lesson_skips(now.date().isoformat())
    return [(d.occurrence.lesson["code"], d.at) for d in TT.departures(store.lessons(), skips, now, TZ)]


def test_tuesday_alerts_both_campuses():
    store = store_with_schedule()
    assert planned(store, at(0, "08:00")) == [("BIL463", at(0, "11:45")), ("GNL390", at(0, "15:45"))]


def test_morning_class_gets_no_alert():
    store = store_with_schedule()
    assert planned(store, at(1, "06:00")) == []  # Çarşamba sadece 09:00 dersi var


def test_same_campus_followup_class_gets_no_alert():
    store = store_with_schedule()
    assert planned(store, at(2, "08:00")) == [("KRP", at(2, "15:25"))]  # BIL453 için zaten kampüste


def test_skipped_class_alerts_next_one():
    store = store_with_schedule()
    store.lesson_skip(by_code(store, "KRP")["id"], at(2, "0:00").date().isoformat())
    assert planned(store, at(2, "08:00")) == [("BIL453", at(2, "16:45"))]


def test_forced_alert_overrides_rule():
    store = store_with_schedule()
    store.lesson_update(by_code(store, "BIL469")["id"], {"alert": True})
    store.lesson_update(by_code(store, "KRP")["id"], {"alert": False})
    assert planned(store, at(1, "06:00")) == [("BIL469", at(1, "07:45"))]
    assert planned(store, at(2, "08:00")) == []  # Kariyer'e yine gidiliyor; sadece bildirimi kapalı


def test_started_class_is_not_planned_but_late_alert_is():
    store = store_with_schedule()
    assert planned(store, at(0, "12:10")) == [("BIL463", at(0, "11:45")), ("GNL390", at(0, "15:45"))]
    assert planned(store, at(0, "13:05"))[0][0] == "GNL390"


# ── Store: değişiklik bekleyen bildirimi yeniler ─────────────────────────
def test_update_and_skip_drop_pending_departure():
    store = store_with_schedule()
    lesson = by_code(store, "BIL463")
    key = f"depart:{lesson['id']}:2026-10-13"
    now = at(0, "08:00").astimezone(timezone.utc)
    store.enqueue(Event("depart", key, {"lesson_id": lesson["id"]}), now, not_before=now + timedelta(hours=3))
    store.lesson_update(lesson["id"], {"room": "B-112"})
    assert store.outbox_by_key(key) is None and store.lesson_get(lesson["id"])["room"] == "B-112"
    store.enqueue(Event("depart", key, {"lesson_id": lesson["id"]}), now, not_before=now + timedelta(hours=3))
    store.lesson_skip(lesson["id"], "2026-10-13")
    assert store.outbox_by_key(key) is None
    store.lesson_skip(lesson["id"], "2026-10-13", skip=False)
    assert store.lesson_skips("2026-10-01") == set()


# ── Engine: Pushover'a giden "derse çıkma vakti" ─────────────────────────
def engine_with_schedule(settings, pushed=None):
    channel = None
    if pushed is not None:
        settings = replace(settings, pushover_app_token="APPTOKEN", pushover_user_key="USERKEY")

        async def channel(text, priority):
            pushed.append((text, priority))
    engine = Engine(settings, store_with_schedule(), alert_channel=channel)
    return engine


def flush(engine, now) -> list[str]:
    telegram: list[str] = []

    async def send(message, ctx):
        telegram.append(message.text)

    asyncio.run(engine.flush(send, now.astimezone(timezone.utc)))
    return telegram


def test_departure_goes_to_pushover_at_time(settings):
    pushed: list = []
    engine = engine_with_schedule(settings, pushed)
    morning = at(0, "08:00").astimezone(timezone.utc)
    assert engine.plan_departures(morning) == 2
    assert engine.plan_departures(morning) == 0  # her dakika çağrılır, tekrar kurmaz
    assert flush(engine, at(0, "11:40")) == [] and pushed == []
    assert flush(engine, at(0, "11:45")) == []
    text, priority = pushed[0]
    assert priority == 1 and "Derse çıkma vakti: BIL463 Bulanık Mantık" in text
    assert "Başlangıç 13:00 (1 sa 15 dk kaldı) · B-111" in text


def test_departure_without_pushover_goes_to_telegram(settings):
    engine = engine_with_schedule(settings)
    engine.plan_departures(at(0, "08:00").astimezone(timezone.utc))
    sent = flush(engine, at(0, "15:45"))
    assert len(sent) == 2 and "C-307 (Küçükyalı)" in sent[1]  # 11:45'teki geç de olsa gider


def test_departure_category_can_be_turned_off(settings):
    engine = engine_with_schedule(settings)
    PR.set_value(engine.store, "lesson", False)
    engine.plan_departures(at(0, "08:00").astimezone(timezone.utc))
    assert flush(engine, at(0, "12:00")) == []


def test_departure_held_in_full_quiet_but_not_normal_quiet(settings):
    engine = engine_with_schedule(settings)
    engine.plan_departures(at(0, "08:00").astimezone(timezone.utc))
    engine.set_mute(at(0, "20:00").astimezone(timezone.utc))
    assert len(flush(engine, at(0, "11:45"))) == 1
    engine.set_mute(at(0, "20:00").astimezone(timezone.utc), allow_urgent=False)
    assert flush(engine, at(0, "15:45")) == []


def test_program_view_shows_now_next_and_week():
    from ekampus import messages as M

    store = store_with_schedule()
    gnl = by_code(store, "GNL390")["id"]
    store.lesson_skip(gnl, "2026-10-13")
    text = M.timetable_view(store.lessons(), store.lesson_skips("2026-10-12"), TZ, at(0, "14:10")).text
    assert "Şu an: BIL463 Bulanık Mantık · B-111 (15:50 biter)" in text
    assert "Sıradaki: 14 Eki Çarşamba 09:00" in text or "Sıradaki: yarın 09:00" in text  # iptal edilen GNL390 atlanır
    assert "17:00-18:50 GNL390 Yapay Zekâ ve Büyük Veri ile Finans · C-307 (Küçükyalı) · <i>bu hafta iptal</i>" in text
    assert text.index("<b>Salı</b>") < text.index("<b>Cuma</b>")


def test_cli_import_and_export(settings, monkeypatch, tmp_path, capsys):
    import json

    from ekampus import __main__ as cli

    path = tmp_path / "program.json"
    path.write_text(json.dumps(SCHEDULE + [{"code": "BOZUK", "name": "x", "weekday": "x", "start": "1", "end": "2"}],
                               ensure_ascii=False), encoding="utf-8")
    assert cli._timetable(settings, "import", str(path)) == 0
    assert cli._timetable(settings, "import", str(path)) == 0  # ikinci kez eklemez
    out = capsys.readouterr().out
    assert "6 ders eklendi" in out and "0 ders eklendi, programda 6 ders var" in out and "Atlandı (BOZUK)" in out
    cli._timetable(settings, "export", None)
    exported = json.loads(capsys.readouterr().out)
    assert exported[0]["code"] == "BIL463" and exported[1]["campus"] == "Küçükyalı"


def test_delete_removes_skips():
    store = store_with_schedule()
    lesson = by_code(store, "BIL441")
    store.lesson_skip(lesson["id"], "2026-10-16")
    assert store.lesson_delete(lesson["id"])["code"] == "BIL441"
    assert store.lesson_get(lesson["id"]) is None and store.lesson_skips("2026-01-01") == set()

"""Ajanın ders programı araçları: sınıf soruları, değiştirme ve "dersi iptal ettim" (JEV kontrolüyle)."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ekampus import agent as agent_module
from ekampus.agent import Toolbox, Turn
from ekampus.engine import Engine
from ekampus.guard import ActionGuard
from ekampus.store import Store

from test_guard import FakeJev
from test_timetable import SCHEDULE

TZ = ZoneInfo("Europe/Istanbul")
NOW = datetime(2026, 10, 13, 14, 10, tzinfo=TZ)  # Salı, BIL463 sürüyor


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def box(settings, monkeypatch):
    monkeypatch.setattr(agent_module, "datetime", FixedDatetime)
    keyed = replace(settings, typesafe_api_key="k")  # JEV açık; kontrol sadece guard kurulunca çalışır
    engine = Engine(keyed, Store(":memory:"))
    for fields in SCHEDULE:
        engine.store.lesson_add(fields, NOW)
    return Toolbox(keyed, engine.store, engine=engine)


def guarded(box, jev) -> Toolbox:
    guarded_box = Toolbox(box.s, box.store, engine=box.engine)
    guarded_box.guard = ActionGuard(jev, box.engine)
    return guarded_box


def call(box, name, args=None, text=""):
    turn = Turn(user_text=text)
    return asyncio.run(box.call(name, args or {}, turn)), turn


def lesson_id(box, code) -> int:
    return next(l["id"] for l in box.store.lessons() if l["code"] == code)


def test_class_now_gives_room_of_current_and_next(box):
    result, _ = call(box, "class_now")
    assert result["şu_an_süren"]["sınıf"] == "B-111" and result["şu_an_süren"]["bitmesine"] == "1 sa 41 dk"
    assert result["sıradaki"]["ders"].startswith("GNL390") and result["sıradaki"]["kampüs"] == "Küçükyalı"


def test_find_class_and_timetable(box):
    result, _ = call(box, "find_class", {"query": "sistem programlama"})
    assert result[0]["sınıf"] == "A-127" and result[0]["gün"] == "Çarşamba"
    week, _ = call(box, "timetable")
    assert len(week["dersler"]) == 6
    alerts = {d["ders"].split()[0]: d["sıradaki_çıkış_bildirimi"] for d in week["dersler"]}
    assert alerts["GNL390"] == "bugün 15:45" and alerts["KRP"].startswith("15 Eki") and alerts["KRP"].endswith("15:25")
    assert alerts["BIL469"].startswith("bu hafta gelmeyecek") and alerts["BIL453"].startswith("bu hafta gelmeyecek")
    thursday, _ = call(box, "timetable", {"day": "perşembe"})
    assert [d["saat"] for d in thursday["dersler"]] == ["16:40-17:30", "18:00-19:50"]
    missing, _ = call(box, "find_class", {"query": "ENG405"})
    assert "hata" in missing


def test_read_tools_are_available_in_triage(box):
    names = {t["function"]["name"] for t in box.specs("triage")}
    assert {"timetable", "class_now", "find_class"} <= names
    assert not {"skip_lesson", "update_lesson", "add_lesson"} & names


def test_skip_lesson_defaults_to_this_week_and_hides_it(box):
    gnl = lesson_id(box, "GNL390")
    result, turn = call(box, "skip_lesson", {"id": gnl}, "bugünkü finans dersini iptal ettim")
    assert result["durum"] == "tamam" and "bugün 17:00" in result["iptal_edilen"]
    assert turn.actions[0].startswith("Ders iptal edildi")
    after, _ = call(box, "class_now")
    assert after["sıradaki"]["ders"].startswith("BIL469")  # iptal edilen ders atlanır
    plan = box.engine.plan_departures(NOW.astimezone(timezone.utc))
    assert plan == 0  # 15:45 bildirimi kurulmaz; Perşembe'ninki 26 saatten uzak
    undo, _ = call(box, "unskip_lesson", {"id": gnl})
    assert undo["durum"] == "tamam" and box.engine.plan_departures(NOW.astimezone(timezone.utc)) == 1


def test_skip_rejects_wrong_weekday(box):
    with pytest.raises(ValueError, match="Çarşamba"):  # sohbette hata metni modele döner
        call(box, "skip_lesson", {"id": lesson_id(box, "BIL463"), "date": "2026-10-14"})


def test_update_add_delete_and_alert_mode(box):
    krp = lesson_id(box, "KRP")
    result, turn = call(box, "update_lesson", {"id": krp, "room": "D-204"})
    assert result["ders"]["sınıf"] == "D-204" and "D-204" in turn.actions[0]
    result, _ = call(box, "set_lesson_alert", {"id": lesson_id(box, "BIL469"), "mode": "on"})
    assert result["çıkış_bildirimi"].startswith("her zaman")
    result, turn = call(box, "add_lesson", {"code": "ENG405", "name": "Engineering Philosophy", "weekday": "Çarşamba",
                                            "start": "14:00", "end": "16:50", "room": "C-201"})
    assert result["durum"] == "eklendi" and turn.actions
    result, _ = call(box, "delete_lesson", {"id": lesson_id(box, "ENG405")})
    assert result["durum"] == "silindi" and len(box.store.lessons()) == 6


def test_lesson_changes_go_through_jev(box):
    jev = FakeJev(requested=0.2, risky=0.9)
    strict = guarded(box, jev)
    result, turn = call(strict, "skip_lesson", {"id": lesson_id(box, "BIL463")}, "bulanık mantık hangi sınıfta?")
    assert result["hata"] == "güvenlik kontrolü bu eylemi durdurdu"
    assert box.store.lesson_skips("2026-10-01") == set()
    action = jev.states[0][0]["proposed_action"]
    assert action["tool"] == "skip_lesson"
    # JEV id'yi öğrencinin sözüyle eşleştirebilsin: dersin adı, o günkü tarih ve bugünün tarihi
    assert "BIL463 Bulanık Mantık" in action["target"] and "2026-10-13 13:00 (bugün 13:00)" in action["target"]
    assert "bugün Salı 2026-10-13" in action["target"]

    ok = guarded(box, FakeJev(requested=0.55, risky=0.5))
    result, _ = call(ok, "skip_lesson", {"id": lesson_id(box, "BIL463")}, "bulanık mantığı iptal ettim")
    assert result["durum"] == "tamam"


def test_low_risk_does_not_excuse_unrequested_lesson_change(box):
    # Gerçek JEV: "şu anki ders hangi sınıftaydı?" sorusunda sınıf değişikliğine istendi %8, riskli %11 verdi
    strict = guarded(box, FakeJev(requested=0.08, risky=0.11))
    krp = lesson_id(box, "KRP")
    result, _ = call(strict, "update_lesson", {"id": krp, "room": "D-204"}, "şu anki ders hangi sınıftaydı?")
    assert result["hata"] == "güvenlik kontrolü bu eylemi durdurdu" and box.store.lesson_get(krp)["room"] == ""


def test_read_tools_skip_jev(box):
    jev = FakeJev(requested=0.0, risky=1.0)
    strict = guarded(box, jev)
    result, _ = call(strict, "class_now", {}, "")
    assert result["şu_an_süren"]["sınıf"] == "B-111" and jev.states == []


def test_upcoming_reminders_include_departures(box):
    result, _ = call(box, "upcoming_reminders", {"days": 3})
    departures = [r for r in result if r["tür"].startswith("derse çıkış")]
    assert [d["başlık"] for d in departures] == ["GNL390 Yapay Zekâ ve Büyük Veri ile Finans", "KRP Kariyer Planlama"]

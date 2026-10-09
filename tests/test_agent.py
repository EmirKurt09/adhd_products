"""Ajanın yazma araçları: sessiz mod, ayarlar, ödev işareti, hafıza, hatırlatma, dosya, yenileme (ağ çağrısı yok)."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ekampus import prefs as PR
from ekampus.agent import Toolbox, Turn, parse_local
from ekampus.detect import diff
from ekampus.engine import Engine, ScanOutcome
from ekampus.models import Item, Scan
from ekampus.store import Store


def local(settings, delta: timedelta) -> str:
    """Modelin vereceği biçimde yerel zaman: '2026-10-10 18:00'."""
    return (datetime.now(timezone.utc) + delta).astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def box(settings):
    engine = Engine(settings, Store(":memory:"))
    now = datetime.now(timezone.utc)
    items = [
        Item(kind="assignment", uid="7", scope="course:1", title="Lab Raporu", course="Ağlar", due_at=now + timedelta(days=2)),
        *[Item(kind="file", uid=f"1:{i}", scope="course:1", title=f"lecture {i}", course="Ağlar") for i in range(5)],
    ]
    scan = Scan(items=items, ok_scopes={("assignment", "course:1"), ("file", "course:1")})
    engine.store.apply(scan, diff({}, set(), scan), now)
    return Toolbox(settings, engine.store, engine=engine)


def call(box, name, args=None, mode="chat"):
    turn = Turn(mode=mode)
    return asyncio.run(box.call(name, args or {}, turn)), turn


def names(specs) -> set[str]:
    return {t["function"]["name"] for t in specs or []}


# ── Modlar ────────────────────────────────────────────────────────────────────

def test_write_tools_only_in_chat(box):
    writes = {name for name, tool in box.tools.items() if tool.writes}
    assert {"set_quiet", "remember", "remind_me", "set_feature", "send_file", "refresh_now"} <= writes
    assert not writes & names(box.specs("triage"))  # site metni işlenen yolda yazma aracı yok
    assert writes <= names(box.specs("chat"))
    result, turn = call(box, "set_quiet", {"until": "2099-01-01 10:00"}, mode="triage")
    assert result == {"hata": "bu araç burada kullanılamaz"} and turn.actions == []


def test_parse_local_assumes_istanbul(settings):
    now = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    assert parse_local("2026-10-10 18:00", settings.tz, now) == datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_local("cuma akşamı", settings.tz, now)


# ── Sessiz mod ────────────────────────────────────────────────────────────────

def test_set_quiet(box, settings):
    result, turn = call(box, "set_quiet", {"until": local(settings, timedelta(hours=5))})
    assert result["durum"] == "tamam" and "kaldı" in result["bitiş"]
    assert box.engine.hold_reason(datetime.now(timezone.utc)) == "sessiz"
    assert turn.actions[0].startswith("Sessiz mod:") and "acil olanlar yine gelir" in turn.actions[0]
    call(box, "set_quiet", {"until": local(settings, timedelta(hours=5)), "allow_urgent": False})
    assert box.engine.mute_full()
    _, turn = call(box, "end_quiet")
    assert box.engine.hold_reason(datetime.now(timezone.utc)) is None and turn.flush


@pytest.mark.parametrize("delta", [timedelta(hours=-1), timedelta(days=31)])
def test_set_quiet_rejects_past_and_far(box, settings, delta):
    result, turn = call(box, "set_quiet", {"until": local(settings, delta)})
    assert "hata" in result and turn.actions == [] and box.engine.muted_until() is None


# ── Ayarlar ───────────────────────────────────────────────────────────────────

def test_set_notification(box):
    _, turn = call(box, "set_notification", {"setting": "announcements", "enabled": False})
    assert PR.load(box.store)["announcements"] is False and turn.actions == ["Duyurular: kapalı"]
    assert "hata" in call(box, "set_notification", {"setting": "fail_after", "enabled": False})[0]


def test_set_feature_respects_keys(box, settings):
    result, _ = call(box, "set_feature", {"feature": "jev", "enabled": True})
    assert result == {"hata": "JEV karar katmanı çalışamaz: .env'de TYPESAFE_API_KEY yok"}
    keyed = Toolbox(replace(settings, typesafe_api_key="k"), box.store, engine=box.engine)
    _, turn = call(keyed, "set_feature", {"feature": "jev", "enabled": False})
    assert PR.load(box.store)["jev"] is False and turn.actions == ["JEV karar katmanı: kapalı"]
    assert call(keyed, "set_feature", {"feature": "jev", "enabled": False})[0] == {"durum": "zaten kapalı"}


def test_mark_assignment(box):
    _, turn = call(box, "mark_assignment", {"uid": "7", "done": True})
    assert box.store.item("assignment", "7")["done_manual"] == 1
    assert turn.actions == ["“Lab Raporu” teslim edildi olarak işaretlendi"]
    assert "hata" in call(box, "mark_assignment", {"uid": "999", "done": True})[0]


# ── Hafıza ve hatırlatma ──────────────────────────────────────────────────────

def test_remember_and_forget(box):
    result, turn = call(box, "remember", {"text": "Ağlar dersini bıraktı"})
    assert result["durum"] == "tamam" and turn.actions == ["Hafızaya eklendi: Ağlar dersini bıraktı"]
    again, turn = call(box, "remember", {"text": "ağlar dersini bıraktı"})
    assert again["durum"] == "zaten hafızada" and turn.actions == []
    _, turn = call(box, "forget", {"id": result["id"]})
    assert box.store.memory_list() == [] and turn.actions == ["Hafızadan silindi: Ağlar dersini bıraktı"]


def test_remind_me_and_cancel(box, settings):
    result, turn = call(box, "remind_me", {"at": local(settings, timedelta(days=1)), "text": "raporu yükle"})
    assert result["durum"] == "tamam" and turn.actions[0].startswith("Hatırlatma kuruldu:")
    assert [n["text"] for n in box.store.notes_pending()] == ["raporu yükle"]
    assert "hata" in call(box, "remind_me", {"at": local(settings, timedelta(minutes=-5)), "text": "x"})[0]
    _, turn = call(box, "cancel_reminder", {"id": result["id"]})
    assert box.store.notes_pending() == [] and turn.actions == ["Hatırlatma iptal edildi: raporu yükle"]
    assert "hata" in call(box, "cancel_reminder", {"id": result["id"]})[0]


# ── Dosya ve yenileme ─────────────────────────────────────────────────────────

def test_send_file_queues_for_bot(box):
    turn = Turn()
    for i in range(4):
        asyncio.run(box.call("send_file", {"uid": f"1:{i}"}, turn))
    assert turn.files == ["1:0", "1:1", "1:2"]  # bir seferde en fazla 3
    assert "hata" in asyncio.run(box.call("send_file", {"uid": "yok"}, turn))


def test_refresh_now_has_cooldown(box):
    scans = []

    async def fake_scan(reason):
        scans.append(reason)
        return ScanOutcome(True, events=2, duration_s=12)

    box.engine.run_scan = fake_scan
    result, turn = call(box, "refresh_now")
    assert result["yeni_olay"] == 2 and turn.flush and turn.actions == ["Site kontrol edildi: 2 yeni olay"]
    assert "hata" in call(box, "refresh_now")[0] and scans == ["asistan"]

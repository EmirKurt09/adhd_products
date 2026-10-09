"""Ajanın yazma araçları: sessiz mod, ayarlar, ödev işareti, hafıza, hatırlatma, dosya, yenileme (ağ çağrısı yok)."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ekampus import messages as M
from ekampus import prefs as PR
from ekampus.agent import Toolbox, Turn, parse_local
from ekampus.detect import diff
from ekampus.engine import Engine, ScanOutcome
from ekampus.llm import Assistant
from ekampus.models import Event, Item, Scan
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
    assert turn.files == [f"1:{i}" for i in range(4)]
    assert "hata" in asyncio.run(box.call("send_file", {"uid": "yok"}, turn))


def test_refresh_now_lists_what_is_new_and_has_cooldown(box, settings):
    scans = []
    due = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    found = [Event("new", "new:assignment:9", {"kind": "assignment", "uid": "9", "title": "Homework 5",
                                               "course": "Ağlar", "due_at": due}),
             Event("new", "new:grade:x", {"kind": "grade", "uid": "x", "title": "Vize", "course": "Ağlar",
                                          "extra": {"value": "85"}})]

    async def fake_scan(reason):
        scans.append(reason)
        return ScanOutcome(True, events=2, duration_s=12, findings=found)

    box.engine.run_scan = fake_scan
    result, turn = call(box, "refresh_now")
    assert [f["başlık"] for f in result["yeniler"]] == ["Homework 5", "Vize"]
    assert result["yeniler"][0]["tarih"].endswith("3 gün kaldı") or "kaldı" in result["yeniler"][0]["tarih"]
    assert result["yeniler"][1]["not"] == "85" and result["yeniler"][0]["olay"] == "yeni"
    assert turn.flush and turn.actions == ["Site kontrol edildi: 2 yeni ya da değişen kayıt"]
    assert "hata" in call(box, "refresh_now")[0] and scans == ["asistan"]


def test_retry_login_resets_guard_and_scans(box, settings):
    from ekampus.browser import AuthGuard

    async def fake_scan(reason):
        return ScanOutcome(True, findings=[])

    box.engine.run_scan = fake_scan
    guard = AuthGuard(settings.auth_guard_path, settings.username, settings.password)
    guard.block("şifre yanlış")
    assert box.run_read("bot_state", {})["giriş"] == "kilitli: şifre yanlış"
    result, turn = call(box, "retry_login")
    assert result["durum"] == "tamam" and result["kilit_vardı"] is True
    assert turn.actions == ["Giriş kilidi kaldırıldı, tekrar denendi", "Site kontrol edildi: yeni bir şey yok"]
    assert not guard.status().get("blocked")


# ── Sohbet döngüsü: model aracı çağırır, kod "Yapılanlar"ı yazar ─────────────

class _Call:
    def __init__(self, call_id, name, args):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))

    def model_dump(self):
        return {"id": self.id, "type": "function", "function": {"name": self.function.name, "arguments": self.function.arguments}}


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=50), choices=[SimpleNamespace(message=message)])


def scripted(*responses):
    queue, sent = list(responses), []

    async def create(**kwargs):
        sent.append({"messages": [dict(m) for m in kwargs["messages"]], "tools": kwargs.get("tools")})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


def agent(box, settings) -> Assistant:
    a = Assistant(replace(settings, llm_api_key="test"), box.store, engine=box.engine)
    a._model = "test-model"
    return a


def test_chat_acts_and_reports_actions(box, settings):
    a = agent(box, settings)
    until = local(settings, timedelta(hours=6))
    a.client, sent = scripted(
        _response(tool_calls=[_Call("c1", "set_quiet", {"until": until}),
                              _Call("c2", "remember", {"text": "Cuma günleri yarı zamanlı çalışıyor"})]),
        _response(content="Tamam, akşama kadar rahatsız etmeyeceğim."),
    )
    reply = asyncio.run(a.chat("akşama kadar rahatsız etme, bu arada cuma günleri çalışıyorum"))
    assert reply.text.startswith("Tamam, akşama kadar rahatsız etmeyeceğim.\n\nYapılanlar:\n• Sessiz mod:")
    assert "• Hafızaya eklendi: Cuma günleri yarı zamanlı çalışıyor" in reply.text
    assert box.engine.muted_until() is not None
    assert "set_quiet" in {t["function"]["name"] for t in sent[0]["tools"]}
    assert a.store.chat_recent(2)[-1]["content"] == reply.text  # model sonraki soruda ne yaptığını görür
    entry = a.store.llm_log_at(0)
    assert [s["writes"] for s in entry["steps"]] == [True, True]
    assert "[eylem] set_quiet" in M.llm_log_view(entry, 0, 1, settings.tz, datetime.now(timezone.utc)).text


def test_memory_reaches_every_prompt(box, settings):
    box.store.memory_add("Ağlar dersini bıraktı", datetime.now(timezone.utc))
    a = agent(box, settings)
    a.client, sent = scripted(_response(content="Tamam."))
    asyncio.run(a.chat("selam"))
    system = sent[0]["messages"][0]["content"]
    assert "#1 Ağlar dersini bıraktı" in system and "YYYY-MM-DD HH:MM" in system


def test_actions_survive_a_failed_answer(box, settings):
    a = agent(box, settings)
    a.client, _ = scripted(_response(tool_calls=[_Call("c1", "send_file", {"uid": "1:0"})]), RuntimeError("koptu"))
    reply = asyncio.run(a.chat("lecture 0'ı at"))
    assert reply.files == ["1:0"] and reply.text.startswith("Cevabı tamamlayamadım ama istediklerini yaptım.")


def test_chat_without_actions_has_no_footer(box, settings):
    a = agent(box, settings)
    a.client, _ = scripted(_response(tool_calls=[_Call("c1", "bot_state", {})]), _response(content="Her şey açık."))
    reply = asyncio.run(a.chat("durum ne?"))
    assert reply.text == "Her şey açık." and reply.actions == [] and not reply.flush


def test_model_written_action_list_is_replaced_by_code(box, settings):
    a = agent(box, settings)
    a.client, _ = scripted(
        _response(tool_calls=[_Call("c1", "end_quiet", {})]),
        _response(content="Sessiz modu kapattım.\n\nYapılanlar:\n• Sessiz mod kapatıldı"),  # geçmişi taklit ediyor
    )
    reply = asyncio.run(a.chat("sessizi kapat"))
    assert reply.text == "Sessiz modu kapattım.\n\nYapılanlar:\n• Sessiz mod kapatıldı"
    a.client, _ = scripted(_response(content="Yapılanlar:\n• Bot durumu gösterildi"))  # eylem yokken uydurma liste
    assert asyncio.run(a.chat("durum?")).text == ""


def test_duplicate_reminder_call_counts_once(box, settings):
    a = agent(box, settings)
    args = {"at": local(settings, timedelta(days=1)), "text": "raporu yükle"}
    a.client, sent = scripted(_response(tool_calls=[_Call("c1", "remind_me", args), _Call("c2", "remind_me", args)]),
                              _response(content="Kurdum."))
    reply = asyncio.run(a.chat("yarın hatırlat"))
    results = [json.loads(m["content"]) for m in sent[1]["messages"] if m["role"] == "tool"]
    assert results[0] == results[1] and results[0]["durum"] == "tamam"  # model "zaten kuruluydu" sanmasın
    assert reply.text.count("Hatırlatma kuruldu") == 1 and len(box.store.notes_pending()) == 1


# ── Veritabanı ve hatırlatma takvimi ──────────────────────────────────────────

def test_query_db_reads_but_never_writes(box):
    result = box.run_read("query_db", {"sql": "SELECT title, yerel(due_at) AS teslim FROM items WHERE kind = 'assignment'"})
    assert result["sütunlar"] == ["title", "teslim"] and result["satırlar"][0][0] == "Lab Raporu"
    assert len(result["satırlar"][0][1]) == 16  # '2026-10-11 12:00' gibi yerel saat
    for sql in ("DELETE FROM items", "UPDATE items SET title = 'x'", "SELECT 1; DROP TABLE items",
                "WITH x AS (SELECT 1) DELETE FROM memory", "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(items)"):
        assert "hata" in box.run_read("query_db", {"sql": sql}), sql
    assert box.store.item("assignment", "7")["title"] == "Lab Raporu"
    box.store.memory_add("hala yazılabilir", datetime.now(timezone.utc))  # yetkilendirici sorgudan sonra kalkar


def test_query_db_stops_runaway_queries(box):
    sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    assert "hata" in box.run_read("query_db", {"sql": sql})


def test_upcoming_reminders_merge_automatic_and_personal(box, settings):
    now = datetime.now(timezone.utc)
    box.engine.schedule_note("ilaç", now + timedelta(hours=5), now)
    upcoming = box.run_read("upcoming_reminders", {"days": 7})
    kinds = [u["tür"] for u in upcoming]
    # Lab Raporu 2 gün sonra: 24 saat ve 3 saat kala hatırlatmaları; arada kişisel hatırlatma
    assert kinds == ["kişisel hatırlatma", "teslim hatırlatması (24 saat kala)", "teslim hatırlatması (3 saat kala)"]
    asyncio.run(box.call("mark_assignment", {"uid": "7", "done": True}, Turn()))
    assert [u["tür"] for u in box.run_read("upcoming_reminders", {})] == ["kişisel hatırlatma"]


# ── Ders ve ödev bazında bildirim, erteleme ───────────────────────────────────

def flush_texts(engine, now=None) -> list[str]:
    sent: list[str] = []

    async def send(message, ctx):
        sent.append(message.text)

    asyncio.run(engine.flush(send, now or datetime.now(timezone.utc)))
    return sent


def test_mute_course_silences_everything_from_it(box):
    result, turn = call(box, "mute_course", {"course": "ağ", "muted": True})
    assert result["ders"] == "Ağlar" and turn.actions == ["Ağlar: bildirimler kapatıldı"]
    now = datetime.now(timezone.utc)
    box.store.enqueue(Event("new", "new:announcement:a", {"kind": "announcement", "uid": "a", "title": "Ağ duyurusu",
                                                          "course": "Ağlar"}), now)
    box.store.enqueue(Event("new", "new:announcement:b", {"kind": "announcement", "uid": "b", "title": "Başka",
                                                          "course": "Fizik"}), now)
    sent = flush_texts(box.engine)
    assert any("Başka" in t for t in sent) and not any("Ağ duyurusu" in t for t in sent)
    assert box.run_read("upcoming_reminders", {}) == []  # Ağlar ödevinin hatırlatmaları da yok
    assert "hata" in call(box, "mute_course", {"course": "kimya", "muted": True})[0]
    _, turn = call(box, "mute_course", {"course": "Ağlar", "muted": False})
    assert PR.muted_courses(box.store) == [] and turn.actions == ["Ağlar: bildirimler açıldı"]


def test_muted_course_findings_skip_jev_and_llm(box, settings):
    from ekampus.router import FindingRouter

    asked = []

    class Jev:
        async def ask(self, state):
            asked.append(state)

    PR.set_course_muted(box.store, "Ağlar", True)
    router = FindingRouter(replace(settings, typesafe_api_key="k"), box.engine, None, Jev())
    asyncio.run(router.route([Event("new", "new:assignment:9", {"kind": "assignment", "uid": "9", "title": "x",
                                                                 "course": "Ağlar"})]))
    assert asked == []


def test_remind_before_due_and_mute_assignment_reminders(box):
    result, turn = call(box, "remind_before_due", {"uid": "7", "hours_before": 0.5})
    assert result["durum"] == "tamam" and turn.actions[0].endswith("Lab Raporu (30 dk kala)")
    note = box.store.notes_pending()[0]
    due = box.store.item("assignment", "7")["due"]
    assert M.parse_dt(note["at"]) == due - timedelta(minutes=30) and "teslimine 30 dk kaldı" in note["text"]
    assert "hata" in call(box, "remind_before_due", {"uid": "7", "hours_before": 100})[0]  # zaman geçmiş

    _, turn = call(box, "mute_assignment_reminders", {"uid": "7", "muted": True})
    assert turn.actions == ["“Lab Raporu” teslim hatırlatmaları: susturuldu"]
    assert box.engine.plan_reminders(due - timedelta(hours=2)) == 0  # 3 saat eşiği geçti ama susturuldu
    assert [u["tür"] for u in box.run_read("upcoming_reminders", {})] == ["kişisel hatırlatma"]


def test_snooze_last_and_scheduled(box):
    now = datetime.now(timezone.utc)
    note_id, _ = box.engine.schedule_note("su iç", now + timedelta(hours=1), now)
    _, turn = call(box, "snooze", {"minutes": 30, "id": note_id})
    assert M.parse_dt(box.store.notes_pending()[0]["at"]) > now + timedelta(hours=1, minutes=29)
    box.store.cancel_note(note_id)

    box.store.enqueue(Event("reminder", "reminder:3h:7", {"kind": "assignment", "uid": "7", "title": "Lab Raporu",
                                                           "course": "Ağlar", "hours": 3,
                                                           "due_at": (now + timedelta(hours=3)).isoformat()}), now)
    box.store.mark_sent(box.store.outbox_by_key("reminder:3h:7")["id"], now)
    result, turn = call(box, "snooze", {"minutes": 60})
    assert result["metin"].startswith("Teslim: Lab Raporu (Ağlar)") and turn.actions[0].startswith("Ertelendi:")
    assert len(box.store.notes_pending()) == 1
    assert "hata" in call(box, "snooze", {"minutes": 1})[0]


# ── Bekleyenler, özet, zamanlama, tekrar gönderme ─────────────────────────────

def test_pending_notifications_and_send_now(box):
    now = datetime.now(timezone.utc)
    flush_texts(box.engine)  # kurulum özeti gitsin
    box.engine.set_mute(now + timedelta(hours=3))
    box.store.enqueue(Event("new", "new:announcement:z", {"kind": "announcement", "uid": "z", "title": "Vize yeri",
                                                          "course": "Ağlar"}), now)
    pending = box.run_read("pending_notifications", {})
    assert pending["bekletme"] == "sessiz"
    assert pending["bekleyen"] == [{"id": pending["bekleyen"][0]["id"], "tür": "Yeni duyuru", "başlık": "Vize yeri",
                                    "neden": "sessiz nedeniyle bekliyor"}]
    assert flush_texts(box.engine) == []
    result, turn = call(box, "send_pending_now", {})
    assert turn.force_flush and turn.actions == ["Bekleyen 1 bildirim şimdi gönderiliyor"]
    sent = []

    async def send(message, ctx):
        sent.append(message.text)

    asyncio.run(box.engine.flush(send, ignore_hold=True))
    assert len(sent) == 1 and "Vize yeri" in sent[0] and box.engine.muted_until() is not None  # sessiz mod sürer
    assert call(box, "send_pending_now", {})[0] == {"durum": "bekleyen bildirim yok"}


def test_set_schedule_changes_night_and_digest(box, settings):
    _, turn = call(box, "set_schedule", {"digest_time": "9:30", "night_hours": "00:00-08:00"})
    assert box.engine.digest_time().strftime("%H:%M") == "09:30" and turn.reschedule
    assert turn.actions == ["Sabah özeti saati: 09:30", "Gece saatleri: 00:00-08:00"]
    seven_am = datetime(2026, 10, 9, 7, 30, tzinfo=settings.tz)  # .env'de 01-07 olsa gece sayılmazdı
    assert box.engine.is_night(seven_am)
    assert "hata" in call(box, "set_schedule", {"night_hours": "25:00-08:00"})[0]
    assert "hata" in call(box, "set_schedule", {})[0]
    assert box.run_read("bot_state", {})["sabah_özeti"] == "açık, saat 09:30"


def test_alert_threshold_and_digest_now(box):
    _, turn = call(box, "set_alert_threshold", {"failures": 4})
    assert PR.load(box.store)["fail_after"] == 4 and turn.actions == ["Erişim uyarısı: 4 başarısız kontrolden sonra"]
    _, turn = call(box, "send_digest_now", {})
    assert turn.digest


def test_resend_notification(box):
    now = datetime.now(timezone.utc)
    box.store.enqueue(Event("new", "new:grade:g", {"kind": "grade", "uid": "g", "title": "Vize", "course": "Ağlar",
                                                   "extra": {"value": "85"}}), now)
    outbox_id = box.store.outbox_by_key("new:grade:g")["id"]
    assert "hata" in call(box, "resend_notification", {"id": outbox_id})[0]  # henüz gönderilmedi
    box.store.mark_sent(outbox_id, now)
    _, turn = call(box, "resend_notification", {"id": outbox_id})
    assert turn.resend == [outbox_id] and turn.actions == ["Tekrar gönderiliyor: Yeni not · Vize"]

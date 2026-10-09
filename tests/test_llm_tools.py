"""LLM araçları salt okunur ve doğru veri döndürmeli (ağ çağrısı yok)."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ekampus import messages as M
from ekampus.detect import diff
from ekampus.llm import Assistant
from ekampus.models import Item, Scan
from ekampus.store import Store


def assistant(settings) -> Assistant:
    store = Store(":memory:")
    now = datetime.now(timezone.utc)
    items = [
        Item(kind="assignment", uid="1", scope="course:1", title="Açık ödev", course="Ağlar", due_at=now + timedelta(days=2),
             body="Rapor yaz", meta={"submitted": False}),
        Item(kind="assignment", uid="2", scope="course:1", title="Bitti", course="Ağlar", due_at=now + timedelta(days=1),
             meta={"submitted": True, "grade": "90"}),
        Item(kind="grade", uid="assignment:2", scope="course:1", title="Bitti", course="Ağlar", extra={"value": "90"}),
        Item(kind="file", uid="1:x", scope="course:1", title="lecture 1", course="Ağlar", extra={"section": "Genel"}),
    ]
    sc = Scan(items=items, ok_scopes={("assignment", "course:1"), ("grade", "course:1"), ("file", "course:1")})
    store.apply(sc, diff({}, set(), sc), now)
    return Assistant(replace(settings, llm_api_key="test"), store)


def test_tools_are_declared_for_every_handler(settings):
    a = assistant(settings)
    for tool in a.toolbox.specs("triage"):
        name = tool["function"]["name"]
        args = {"days": 7} if name == "agenda" else {"text": "ödev"} if name == "search" else \
            {"kind": "assignment", "uid": "1"} if name == "get_item" else \
            {"sql": "SELECT count(*) FROM items"} if name == "query_db" else {}
        assert "hata" not in str(a.run_tool(name, args))[:20], name


def test_open_assignments_excludes_submitted(settings):
    titles = [r["title"] for r in assistant(settings).run_tool("list_assignments", {"status": "open"})]
    assert titles == ["Açık ödev"]


def test_get_item_includes_body_and_agenda_has_both(settings):
    a = assistant(settings)
    assert a.run_tool("get_item", {"kind": "assignment", "uid": "1"})["metin"] == "Rapor yaz"
    assert {r["uid"] for r in a.run_tool("agenda", {"days": 3})} == {"1", "2"}
    assert a.run_tool("list_grades", {})[0]["not"] == "90"


def test_unknown_tool(settings):
    assert "hata" in assistant(settings).run_tool("delete_everything", {})


# ── /llmlog kayıtları ─────────────────────────────────────────────────────────


class _Call:
    def __init__(self, call_id, name, args):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))

    def model_dump(self):
        return {"id": self.id, "type": "function", "function": {"name": self.function.name, "arguments": self.function.arguments}}


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=100), choices=[SimpleNamespace(message=message)])


def fake_client(*responses):
    queue = list(responses)

    async def create(**kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_answer_is_logged_with_tools_and_full_messages(settings):
    a = assistant(settings)
    a._model = "test-model"
    a.client = fake_client(
        _response(tool_calls=[_Call("c1", "list_assignments", {"status": "open"})]),
        _response(content="Tek açık ödevin var: Açık ödev."),
    )
    assert asyncio.run(a.answer("açık ödevlerim ne?")) == "Tek açık ödevin var: Açık ödev."
    entry = a.store.llm_log_at(0)
    assert entry["kind"] == "sohbet" and entry["question"] == "açık ödevlerim ne?"
    assert entry["tokens"] == 200 and entry["model"] == "test-model"
    assert entry["steps"][0]["tool"] == "list_assignments" and entry["steps"][0]["count"] == 1
    assert "Açık ödev" in entry["steps"][0]["preview"]
    roles = [m["role"] for m in entry["messages"]]
    assert roles[0] == "system" and "tool" in roles  # modele giden her şey kayıtta

    view = M.llm_log_view(entry, 0, 1, settings.tz, M.datetime.now(M.ZoneInfo("UTC")))
    assert "list_assignments(status=open)" in view.text and "1 kayıt" in view.text
    assert all(len((b.data or "").encode()) <= 64 for row in view.buttons for b in row)


def test_failed_call_is_logged_with_error(settings):
    a = assistant(settings)
    a._model = "test-model"
    a.client = fake_client(RuntimeError("bağlantı koptu"))
    with pytest.raises(RuntimeError):
        asyncio.run(a.answer("selam"))
    assert "bağlantı koptu" in a.store.llm_log_at(0)["error"]


def test_log_keeps_last_30(settings):
    from datetime import datetime, timezone

    a = assistant(settings)
    for i in range(35):
        a.store.llm_log_add({"kind": "sohbet", "question": str(i), "steps": []}, datetime.now(timezone.utc))
    assert a.store.llm_log_count() == 30
    assert a.store.llm_log_at(0)["question"] == "34"
    view = M.llm_log_list_view(a.store.llm_log_recent(10), settings.tz, datetime.now(timezone.utc))
    assert view.text.count("araç yok") == 10


# ── Sohbet hafızası (son N mesaj) ─────────────────────────────────────────────

def capturing_client(answer: str):
    sent: list[list[dict]] = []

    async def create(**kwargs):
        sent.append([dict(m) for m in kwargs["messages"]])
        return _response(content=answer)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


def test_last_20_messages_are_resent(settings):
    a = assistant(settings)
    a._model = "test-model"
    now = datetime.now(timezone.utc)
    for i in range(15):  # 30 mesaj; sadece son 20'si kalmalı
        a.store.chat_add("user", f"soru {i}", now)
        a.store.chat_add("assistant", f"cevap {i}", now)
    a.client, sent = capturing_client("tamam")
    asyncio.run(a.answer("yeni soru"))
    history = sent[0][1:-1]  # sistem talimatı ve yeni soru arası
    assert len(history) == 20
    assert history[0] == {"role": "user", "content": "soru 5"} and history[-1]["content"] == "cevap 14"
    assert sent[0][-1] == {"role": "user", "content": "yeni soru"}
    assert len(a.store.chat_recent(100)) == 20  # yeni soru-cevap eklendi, en eski ikisi düştü


def test_window_never_starts_mid_answer(settings):
    a = assistant(settings)
    now = datetime.now(timezone.utc)
    a.store.chat_add("assistant", "yarım kalmış cevap", now)
    a.store.chat_add("user", "soru", now)
    a.store.chat_add("assistant", "cevap", now)
    assert [m["role"] for m in a.store.chat_recent(20)] == ["user", "assistant"]


def test_history_can_be_disabled(settings):
    a = assistant(replace(settings, llm_history_messages=0))
    a._model = "test-model"
    a.client, sent = capturing_client("tamam")
    asyncio.run(a.answer("birinci"))
    asyncio.run(a.answer("ikinci"))
    assert [m["role"] for m in sent[1]] == ["system", "user"]


# ── Botun kendi durumu ────────────────────────────────────────────────────────

def test_bot_state_and_recent_notifications(settings):
    from ekampus.engine import Engine
    from ekampus.models import Event
    from ekampus.agent import Toolbox

    engine = Engine(settings, Store(":memory:"))
    now = datetime.now(timezone.utc)
    engine.set_mute(now + timedelta(hours=3), allow_urgent=False)
    engine.schedule_note("raporu yükle", now + timedelta(days=1), now)
    engine.store.memory_add("Ağlar'ı bıraktı", now)
    engine.store.enqueue(Event("new", "new:announcement:1", {"kind": "announcement", "uid": "1", "title": "Vize yeri"}), now)
    engine.store.mark_sent(engine.store.outbox_by_key("new:announcement:1")["id"], now)
    box = Toolbox(settings, engine.store, engine=engine)

    state = box.run_read("bot_state", {})
    assert state["özellikler"]["JEV karar katmanı"] == "çalışmıyor: .env'de TYPESAFE_API_KEY yok"
    assert state["sessiz_mod"]["seviye"] == "tam sessiz" and state["sessiz_mod"]["kalan"].startswith("2 sa")
    assert state["bildirim_türleri"]["Duyurular"] == "açık"
    assert state["kişisel_hatırlatma_sayısı"] == 1 and state["hafıza_not_sayısı"] == 1
    assert box.run_read("recent_notifications", {})[0] == {"id": 2, "zaman": M.fmt_dt(now, settings.tz), "tür": "Yeni duyuru",
                                                            "başlık": "Vize yeri"}
    assert box.run_read("list_reminders", {})[0]["metin"] == "raporu yükle"

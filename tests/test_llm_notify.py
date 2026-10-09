"""LLM'in soyut uyarı aracı (notify_owner) ve olay güdümlü bulgu değerlendirmesi. Ağ çağrısı yok."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ekampus import prefs as PR
from ekampus.engine import Engine, ScanOutcome
from ekampus.llm import TOOLS, Assistant, notify_tool
from ekampus.models import Event
from ekampus.store import Store

DAY = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
SECRET_TOKEN = "aSECRETapptoken1234567890abcd"
SECRET_USER = "uSECRETuserkey12345678901234x"


@pytest.fixture
def secret_settings(settings):
    return replace(settings, llm_api_key="test", pushover_app_token=SECRET_TOKEN, pushover_user_key=SECRET_USER)


def make(settings, channel=None):
    settings = replace(settings, llm_api_key=settings.llm_api_key or "test")
    engine = Engine(settings, Store(":memory:"), alert_channel=channel)
    assistant = Assistant(settings, engine.store, notifier=engine)
    assistant._model = "test-model"
    return engine, assistant


def finding(title="Homework 5", hours_left=6, submitted=False) -> Event:
    due = (datetime.now(timezone.utc) + timedelta(hours=hours_left)).isoformat(timespec="minutes")
    return Event("new", "new:assignment:5", {"kind": "assignment", "uid": "5", "title": title, "course": "Ağlar",
                                             "due_at": due, "body": "Raporu PDF olarak yükleyin.",
                                             "meta": {"submitted": submitted}})


def response(content=None, tool_calls=None):
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=50),
                           choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


class Call:
    def __init__(self, name, args):
        self.id = "c1"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))

    def model_dump(self):
        return {"id": self.id, "type": "function", "function": {"name": self.function.name, "arguments": self.function.arguments}}


def scripted(*replies):
    """Modelin yerine geçer; modele giden her isteği (mesajlar + araçlar) kaydeder."""
    queue, sent = list(replies), []

    async def create(**kwargs):
        sent.append(json.dumps({"messages": kwargs["messages"], "tools": kwargs.get("tools")}, ensure_ascii=False, default=str))
        return queue.pop(0)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


# ── Aracın kendisi ────────────────────────────────────────────────────────────

def test_tool_is_abstract():
    spec = json.dumps(notify_tool(2), ensure_ascii=False).lower()
    for word in ("pushover", "token", "priority", "öncelik", "api", "user_key", "telegram"):
        assert word not in spec
    assert "bugün kalan hak: 2" in spec
    assert set(notify_tool(2)["function"]["parameters"]["properties"]) == {"title", "message", "reason"}  # alıcı yok


def test_tool_offered_only_when_usable(settings):
    engine, assistant = make(settings)
    names = lambda: [t["function"]["name"] for t in assistant.tools(TOOLS) or []]  # noqa: E731
    assert "notify_owner" in names()
    PR.toggle(engine.store, "assistant")                 # kullanıcı kapattı
    assert "notify_owner" not in names()
    PR.toggle(engine.store, "assistant")
    for i in range(settings.llm_alerts_per_day):          # hak doldu
        engine.notify_owner(f"t{i}", "m")
    assert "notify_owner" not in names()
    no_channel = Assistant(replace(settings, llm_api_key="test"), engine.store)
    assert "notify_owner" not in [t["function"]["name"] for t in no_channel.tools(TOOLS)]


def test_notify_owner_queues_high_priority_escaped_and_deduped(settings):
    engine, _ = make(settings)
    result = engine.notify_owner("<b>Acil</b>", "Teslim <script>bugün</script>", "6 saat kaldı", now=DAY)
    assert result == {"durum": "gönderildi", "bugün_kalan_hak": 2}
    payload = json.loads(engine.store.db.execute("SELECT payload FROM outbox").fetchone()["payload"])
    assert payload["priority"] == 1 and payload["category"] == "assistant" and payload["urgent"] is True
    assert payload["text"].startswith("<b>Asistan: &lt;b&gt;Acil&lt;/b&gt;</b>") and "<script>" not in payload["text"]
    assert engine.notify_owner("<b>Acil</b>", "Teslim <script>bugün</script>", now=DAY)["durum"] == "gönderilmedi"
    engine.notify_owner("b", "m", now=DAY)
    engine.notify_owner("c", "m", now=DAY)
    assert engine.notify_owner("d", "m", now=DAY)["neden"].startswith("bugünkü uyarı hakkı doldu")


# ── Olay güdümlü değerlendirme ────────────────────────────────────────────────

def test_triage_decides_to_alert_and_keys_never_reach_llm(secret_settings):
    pushed = []

    async def channel(text, priority):
        pushed.append((text, priority))

    engine, assistant = make(secret_settings, channel)
    assistant.client, sent = scripted(
        response(tool_calls=[Call("notify_owner", {"title": "Homework 5 için 6 saat", "message": "Teslim edilmedi.",
                                                   "reason": "teslime 24 saatten az var"})]),
        response(content="Teslime 6 saat kaldığı için uyardım."),
    )
    asyncio.run(assistant.triage([finding()]))

    everything = "\n".join(sent)
    assert SECRET_TOKEN not in everything and SECRET_USER not in everything  # anahtarlar modele hiç gitmedi
    assert "pushover" not in everything.lower()
    tool_results = [m for m in json.loads(sent[1])["messages"] if m["role"] == "tool"]
    assert json.loads(tool_results[0]["content"]) == {"durum": "gönderildi", "bugün_kalan_hak": 2}  # sadece durum döner
    assert "Homework 5" in sent[0] and "<bulgular>" in sent[0]

    async def telegram(message, ctx):
        raise AssertionError("uyarı Telegram'a düşmemeliydi")

    asyncio.run(engine.flush(telegram))
    assert len(pushed) == 1 and pushed[0][1] == 1 and "Asistan: Homework 5 için 6 saat" in pushed[0][0]
    log = engine.store.llm_log_at(0)
    assert log["kind"] == "olay değerlendirme" and log["steps"][0]["tool"] == "notify_owner"


def test_triage_can_decide_not_to_alert(settings):
    engine, assistant = make(settings)
    assistant.client, sent = scripted(response(content="Tarihi uzak, uyarı gerekmiyor."))
    asyncio.run(assistant.triage([finding(hours_left=200)]))
    assert len(sent) == 1 and not list(engine.store.db.execute("SELECT 1 FROM outbox"))


def test_triage_skipped_without_quota(settings):
    engine, assistant = make(replace(settings, llm_alerts_per_day=0))

    async def never(**kwargs):
        raise AssertionError("hak yokken model çağrılmamalı")

    assistant.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=never)))
    assert asyncio.run(assistant.triage([finding()])) is None


def test_due_change_finding_shows_old_date(settings):
    _, assistant = make(settings)
    event = Event("due_changed", "due:assignment:5:x", {"kind": "assignment", "uid": "5", "title": "Ödev",
                                                        "due_at": DAY.isoformat(), "old_due_at": None})
    assert assistant._finding(event, DAY)["eski_tarih"] == "yoktu"


def test_assistant_alerts_follow_manager_toggle(settings):
    engine, _ = make(settings)
    engine.notify_owner("t", "m", now=DAY)
    PR.toggle(engine.store, "assistant")

    async def send(message, ctx):
        raise AssertionError("kapalı kategori gönderilmemeli")

    asyncio.run(engine.flush(send, DAY))
    assert engine.store.db.execute("SELECT status FROM outbox").fetchone()["status"] == "muted"


# ── Tarama → bulgu kancası ────────────────────────────────────────────────────

def test_scan_findings_trigger_hook_in_background(settings):
    engine, _ = make(settings)
    seen = []

    async def fake_scan(reason):
        return ScanOutcome(True, 1, findings=[finding()])

    async def hook(findings):
        seen.append([e.data["title"] for e in findings])

    engine._run_scan_locked = fake_scan
    engine.on_findings = hook

    async def go():
        outcome = await engine.run_scan()
        await asyncio.gather(*list(engine._background))
        return outcome

    assert asyncio.run(go()).ok and seen == [["Homework 5"]]


def test_hook_failure_is_recorded_not_raised(settings):
    engine, _ = make(settings)

    async def fake_scan(reason):
        return ScanOutcome(True, 1, findings=[finding()])

    async def broken(findings):
        raise RuntimeError("model yok")

    engine._run_scan_locked = fake_scan
    engine.on_findings = broken

    async def go():
        await engine.run_scan()
        await asyncio.gather(*list(engine._background))

    asyncio.run(go())
    assert "olay değerlendirme" in engine.store.get("errors")

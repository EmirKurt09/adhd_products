"""JEV öncü karar katmanı: istemci, state, karar bantları ve yönlendirici. Ağ çağrısı yok."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ekampus import messages as M
from ekampus import prefs as PR
from ekampus.engine import Engine
from ekampus.jev import QUESTIONS, JevClient, JevDecision, JevError, build_state
from ekampus.models import Event
from ekampus.router import FindingRouter, decide, push_text
from ekampus.store import Store

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
KEY = "ts_SECRETjevkey_1234567890"


def answers(probs: dict) -> dict:
    return {"model": "jev-1.13.0", "answers": {q: {"type": "noul", "noul": p} for q, p in probs.items()},
            "usage": {"input_tokens": 120, "output_tokens": 0}}


def all_probs(**overrides) -> dict:
    base = {q: 0.1 for q in QUESTIONS}
    base.update(overrides)
    return base


def client(*responses):
    seen = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    async def no_sleep(_):
        return None

    return JevClient(KEY, transport=httpx.MockTransport(handler), sleep=no_sleep), seen


def assignment(hours=5, submitted=False, etype="new", old_hours=None, uid="5") -> Event:
    data = {"kind": "assignment", "uid": uid, "title": "Lab Raporu 2", "course": "Ağlar",
            "due_at": (NOW + timedelta(hours=hours)).isoformat(), "body": "PDF yükleyin.", "meta": {"submitted": submitted}}
    if old_hours is not None:
        data["old_due_at"] = (NOW + timedelta(hours=old_hours)).isoformat()
    return Event(etype, f"{etype}:assignment:{uid}", data)


# ── İstemci ───────────────────────────────────────────────────────────────────

def test_request_shape_and_parsing():
    jev, seen = client((200, answers(all_probs(push_now=0.9))))
    decision = asyncio.run(jev.ask({"finding": {"title": "x"}}))
    body = json.loads(seen[0].content)
    assert seen[0].url == "https://api.typesafe.ai/v1/systemone"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "jev-latest" and set(body["questions"]) == set(QUESTIONS)
    assert all(q["type"] == "noul" for q in body["questions"].values())
    assert decision.p("push_now") == 0.9 and decision.model == "jev-1.13.0" and decision.tokens == 120


def test_retries_on_overload_then_succeeds():
    jev, seen = client((529, {}), (429, {}), (200, answers(all_probs())))
    asyncio.run(jev.ask({}))
    assert len(seen) == 3


@pytest.mark.parametrize("status", [401, 422])
def test_errors_raise_without_leaking_key(status):
    jev, _ = client((status, {"detail": "invalid"}))
    with pytest.raises(JevError) as err:
        asyncio.run(jev.ask({}))
    assert KEY not in str(err.value)


def test_missing_answer_is_an_error():
    jev, _ = client((200, answers({"push_now": 0.5})))
    with pytest.raises(JevError):
        asyncio.run(jev.ask({}))


def test_labels():
    decision = JevDecision(all_probs(requires_submission=0.9, exam_related=0.2, schedule_change=0.7))
    assert decision.labels == ["Teslim gerektiriyor", "Tarih değişikliği"]


# ── State: gerçekler kodda hesaplanır ─────────────────────────────────────────

def test_state_facts_are_precomputed():
    state = build_state(assignment(hours=5), None, NOW)
    assert state["facts"] == {"hours_until_due": 5.0, "deadline_passed": False, "due_within_24h": True,
                              "due_within_72h": True, "submitted": False, "due_added": False,
                              "due_moved_earlier": False, "due_moved_later": False}
    assert state["finding"]["title"] == "Lab Raporu 2" and "uid" not in json.dumps(state)


def test_state_uses_current_submission_and_due_moves():
    item = {"submitted": False, "done_manual": 1}
    assert build_state(assignment(), item, NOW)["facts"]["submitted"] is True  # "teslim ettim" butonu
    moved = build_state(assignment(hours=30, etype="due_changed", old_hours=120), None, NOW)["facts"]
    assert moved["due_moved_earlier"] is True and moved["due_within_72h"] is True
    added = build_state(Event("due_changed", "k", {"kind": "assignment", "uid": "1", "title": "x",
                                                   "due_at": NOW.isoformat(), "old_due_at": None}), None, NOW)["facts"]
    assert added["due_added"] is True and added["deadline_passed"] is True


def test_state_contains_no_secrets():
    """State sadece bulgudan kurulur; ayarlara hiç erişmez, dolayısıyla anahtar ya da şifre giremez."""
    text = json.dumps(build_state(assignment(), {"submitted": False, "done_manual": 0, "url": "https://x/y"}, NOW))
    assert "Bearer" not in text and "https://" not in text and set(json.loads(text)) == {"finding", "facts"}


# ── Karar bantları ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("push,explain,expected", [
    (0.95, 0.1, ("send", False)), (0.80, 0.6, ("send", True)), (0.5, 0.9, ("ask_llm", True)),
    (0.31, 0.0, ("ask_llm", False)), (0.30, 0.59, ("none", False)), (0.0, 0.0, ("none", False)),
])
def test_decision_bands(settings, push, explain, expected):
    route = decide(JevDecision(all_probs(push_now=push, needs_explanation=explain)), settings)
    assert (route.push, route.explain) == expected


def test_push_text_template(settings):
    decision = JevDecision(all_probs(requires_submission=0.9))
    title, message = push_text(assignment(hours=5), decision, None, settings, NOW)
    assert title == "Lab Raporu 2"
    assert message == "Ağlar\nTeslim gerektiriyor\nTeslim: bugün 17:00 · 5 sa 0 dk kaldı · teslim edilmedi"


# ── Yönlendirici ──────────────────────────────────────────────────────────────

class FakeJev:
    def __init__(self, by_uid: dict, fail: set = frozenset()):
        self.by_uid, self.fail, self.states = by_uid, fail, []

    async def ask(self, state):
        self.states.append(state)
        title = state["finding"]["title"]
        if title in self.fail:
            raise JevError("HTTP 529")
        return JevDecision(self.by_uid[title], model="jev-test", tokens=10)


class FakeAssistant:
    def __init__(self):
        self.triaged, self.explained = [], []

    async def triage(self, findings, notes=None):
        self.triaged.append(([e.data["title"] for e in findings], notes))

    async def explain(self, event, labels=None):
        self.explained.append(event.data["title"])
        return f"{event.data['title']} için yapılacaklar"


def titled(title, uid, **kw) -> Event:
    e = assignment(uid=uid, **kw)
    e.data["title"] = title
    return e


def make_router(settings, jev, assistant=None):
    keyed = replace(settings, llm_alerts_per_day=5, llm_api_key="test", typesafe_api_key="test")
    engine = Engine(keyed, Store(":memory:"))
    return engine, FindingRouter(engine.s, engine, assistant, jev)


def outbox(engine) -> list[tuple[str, dict]]:
    return [(r["type"], json.loads(r["payload"])) for r in engine.store.db.execute("SELECT type, payload FROM outbox ORDER BY id")]


def test_router_bands_end_to_end(settings):
    jev = FakeJev({"Acil": all_probs(push_now=0.92, requires_submission=0.9), "Belirsiz": all_probs(push_now=0.55),
                   "Sıradan": all_probs(push_now=0.05, needs_explanation=0.8)})
    assistant = FakeAssistant()
    engine, router = make_router(settings, jev, assistant)
    asyncio.run(router.route([titled("Acil", "1"), titled("Belirsiz", "2"), titled("Sıradan", "3")]))

    rows = outbox(engine)
    alerts = [p for t, p in rows if t == "alert"]
    assert len(alerts) == 1 and alerts[0]["priority"] == 1 and "Acil" in alerts[0]["text"]
    assert "Teslim gerektiriyor" in alerts[0]["text"]
    assert assistant.triaged == [(["Belirsiz"], ["Belirsiz: etiket yok; uyarı olasılığı %55"])]  # tek çağrı
    assert assistant.explained == ["Sıradan"]
    explains = [p for t, p in rows if t == "explain"]
    assert explains == [{"kind": "assignment", "uid": "3", "title": "Sıradan", "course": "Ağlar",
                         "text": "Sıradan için yapılacaklar"}]


def test_router_logs_decisions(settings):
    engine, router = make_router(settings, FakeJev({"Acil": all_probs(push_now=0.92)}), FakeAssistant())
    asyncio.run(router.route([titled("Acil", "1")]))
    entry = engine.store.llm_log_at(0)
    assert entry["kind"] == "JEV karar" and entry["decisions"]["push_now"] == 0.92
    assert entry["actions"] == ["uyarı gönderildi"]
    view = M.llm_log_view(entry, 0, 1, settings.tz, NOW)
    assert "Hemen uyar: %92" in view.text and "Yapılan: uyarı gönderildi" in view.text


def test_jev_failure_falls_back_to_llm(settings):
    assistant = FakeAssistant()
    engine, router = make_router(settings, FakeJev({}, fail={"Acil"}), assistant)
    asyncio.run(router.route([titled("Acil", "1")]))
    assert assistant.triaged == [(["Acil"], ["Acil: hızlı karar alınamadı"])]
    assert "jev" in engine.store.get("errors")


def test_only_content_findings_are_routed(settings):
    jev = FakeJev({"Acil": all_probs()})
    engine, router = make_router(settings, jev, FakeAssistant())
    scope = Event("scope_added", "scope:file:course:9", {"kind": "file", "scope": "course:9", "items": []})
    asyncio.run(router.route([scope, titled("Acil", "1")]))
    assert len(jev.states) == 1


def test_explanation_follows_category_toggle(settings):
    engine, router = make_router(settings, FakeJev({"Ödev": all_probs(needs_explanation=0.9)}), FakeAssistant())
    asyncio.run(router.route([titled("Ödev", "1")]))
    PR.toggle(engine.store, "assignment")  # "Yeni ödev" kapalı → açıklaması da gelmez

    async def send(message, ctx):
        raise AssertionError("kapalı kategori gönderilmemeli")

    # Yönlendirici gerçek saatle kuyruğa koyar; gönderim de gerçek saatten sonra denenmeli (sabit saat değil)
    asyncio.run(engine.flush(send, datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert engine.store.db.execute("SELECT status FROM outbox WHERE type = 'explain'").fetchone()["status"] == "muted"


def test_explanation_renders_after_notification(settings):
    message = M.render_event("explain", {"kind": "assignment", "title": "Lab", "course": "Ağlar", "text": "Adım <1>"},
                             settings.tz, NOW)
    assert message.text == "<b>Kısaca:</b> Lab\n<i>Ağlar</i>\nAdım &lt;1&gt;" and message.silent


# ── /ayarlar: hangi katman çalışır ────────────────────────────────────────────

MIXED = {"Acil": all_probs(push_now=0.92), "Belirsiz": all_probs(push_now=0.55, needs_explanation=0.9),
         "Sıradan": all_probs(push_now=0.05)}


def mixed():
    return [titled("Acil", "1"), titled("Belirsiz", "2"), titled("Sıradan", "3")]


def alerts(engine) -> list[str]:
    return [p["text"] for t, p in outbox(engine) if t == "alert"]


def test_jev_off_sends_everything_to_llm(settings):
    jev, assistant = FakeJev(MIXED), FakeAssistant()
    engine, router = make_router(settings, jev, assistant)
    PR.toggle(engine.store, "jev")
    asyncio.run(router.route(mixed()))
    assert jev.states == [] and assistant.triaged == [(["Acil", "Belirsiz", "Sıradan"], None)]


def test_llm_off_uncertain_findings_become_alerts(settings):
    assistant = FakeAssistant()
    engine, router = make_router(settings, FakeJev(MIXED), assistant)
    PR.toggle(engine.store, "llm")
    asyncio.run(router.route(mixed()))
    sent = alerts(engine)
    assert len(sent) == 2 and "Acil" in sent[0] and "Belirsiz" in sent[1]  # kaçmasın diye kararsız da gider
    assert assistant.triaged == [] and assistant.explained == []
    logged = [engine.store.llm_log_at(i)["actions"] for i in range(3)]
    assert ["kararsız, LLM kapalı → uyarı gönderildi", "açıklama atlandı (LLM kapalı)"] in logged


def test_without_llm_key_router_still_works(settings):
    engine, router = make_router(settings, FakeJev(MIXED), None)
    asyncio.run(router.route(mixed()))
    assert len(alerts(engine)) == 2


def test_jev_error_with_llm_off_sends_nothing(settings):
    engine, router = make_router(settings, FakeJev({}, fail={"Acil"}), FakeAssistant())
    PR.toggle(engine.store, "llm")
    asyncio.run(router.route([titled("Acil", "1")]))
    assert alerts(engine) == []
    assert engine.store.llm_log_at(0)["actions"] == ["JEV hatası, LLM kapalı → sadece normal bildirim"]


def test_both_off_does_nothing(settings):
    jev, assistant = FakeJev(MIXED), FakeAssistant()
    engine, router = make_router(settings, jev, assistant)
    PR.toggle(engine.store, "jev")
    PR.toggle(engine.store, "llm")
    asyncio.run(router.route(mixed()))
    assert jev.states == [] and assistant.triaged == [] and outbox(engine) == []

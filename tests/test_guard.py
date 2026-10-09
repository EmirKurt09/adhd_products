"""Riskli eylemlerde JEV kontrolü: döngüde insan yok, JEV "istendi mi / riskli mi" der, karar kodda (ağ yok)."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ekampus import prefs as PR
from ekampus.agent import Toolbox, Turn
from ekampus.engine import Engine
from ekampus.guard import ActionGuard, decide
from ekampus.jev import JevDecision, JevError
from ekampus.store import Store


class FakeJev:
    def __init__(self, requested=0.9, risky=0.9, fail=False):
        self.requested, self.risky, self.fail, self.states = requested, risky, fail, []

    async def ask(self, state, questions=None):
        self.states.append((state, set(questions or {})))
        if self.fail:
            raise JevError("HTTP 529")
        return JevDecision({"explicitly_requested": self.requested, "risky": self.risky}, model="jev-test", tokens=5)


def make(settings, jev):
    keyed = replace(settings, typesafe_api_key="k")
    engine = Engine(keyed, Store(":memory:"))
    box = Toolbox(keyed, engine.store, engine=engine)
    box.guard = ActionGuard(jev, engine)
    return box


def call(box, name, args, text="", context=None):
    turn = Turn(user_text=text, context=context or [])
    return asyncio.run(box.call(name, args, turn)), turn


@pytest.mark.parametrize("requested,risky,allowed", [(0.9, 0.95, True), (0.3, 0.8, False), (0.3, 0.2, True), (0.18, 0.48, False),
                                                     (0.6, 0.99, True), (0.59, 0.5, False)])
def test_decision(requested, risky, allowed):
    assert decide(JevDecision({"explicitly_requested": requested, "risky": risky})).allowed is allowed


def test_requested_risky_action_goes_through(settings):
    jev = FakeJev(requested=0.95, risky=0.9)
    box = make(settings, jev)
    result, turn = call(box, "set_feature", {"feature": "pushover", "enabled": False}, "pushover'ı kapat")
    assert result["durum"] == "tamam" and PR.load(box.store)["pushover"] is False
    state, questions = jev.states[0]
    assert questions == {"explicitly_requested", "risky"}
    assert state["student_message"] == "pushover'ı kapat" and state["proposed_action"]["tool"] == "set_feature"
    entry = box.store.llm_log_at(0)
    assert entry["kind"] == "JEV eylem kontrolü" and entry["actions"][0].startswith("izin verildi")


def test_unrequested_risky_action_is_stopped(settings):
    box = make(settings, FakeJev(requested=0.2, risky=0.85))
    result, turn = call(box, "set_notification", {"setting": "assignment", "enabled": False}, "ödevlerim ne durumda?")
    assert result["hata"] == "güvenlik kontrolü bu eylemi durdurdu"
    assert PR.load(box.store)["assignment"] is True  # hiçbir şey değişmedi
    assert turn.actions[0].startswith("Durduruldu (güvenlik kontrolü): set_notification")
    assert box.store.llm_log_at(0)["actions"][0].startswith("durduruldu")


def test_confirmation_of_a_proposal_carries_context(settings):
    jev = FakeJev(requested=0.9, risky=0.9)
    box = make(settings, jev)
    context = [{"role": "assistant", "text": "Ağlar dersinin bildirimlerini kapatayım mı?"}]
    box.store.db.execute("INSERT INTO items (kind, uid, scope, title, course, fingerprint, first_seen, last_seen, "
                         "updated_at) VALUES ('file', '1', 'c', 't', 'Ağlar', 'f', 'x', 'x', 'x')")
    result, _ = call(box, "mute_course", {"course": "Ağlar", "muted": True}, "evet", context)
    assert result["durum"] == "tamam" and jev.states[0][0]["conversation"] == context


def test_safe_directions_and_tools_skip_jev(settings):
    jev = FakeJev(requested=0.0, risky=1.0)  # sorulsaydı durdururdu
    box = make(settings, jev)
    call(box, "set_notification", {"setting": "grades", "enabled": True})
    call(box, "remember", {"text": "cuma çalışıyor"})
    call(box, "remind_me", {"at": (datetime.now(settings.tz) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                            "text": "su iç"})
    call(box, "set_alert_threshold", {"failures": 2})
    assert jev.states == [] and len(box.store.notes_pending()) == 1


def test_jev_off_or_failing_does_not_block(settings):
    jev = FakeJev(requested=0.0, risky=1.0)
    box = make(settings, jev)
    PR.toggle(box.store, "jev")  # /ayarlar → JEV kapalı
    assert call(box, "set_feature", {"feature": "pushover", "enabled": False})[0]["durum"] == "tamam"
    assert jev.states == []

    failing = make(settings, FakeJev(fail=True))
    result, _ = call(failing, "set_feature", {"feature": "pushover", "enabled": False})
    assert result["durum"] == "tamam" and "jev" in failing.store.get("errors")
    assert failing.store.llm_log_at(0)["actions"][0].startswith("izin verildi: JEV'e ulaşılamadı")


def test_chat_passes_user_text_to_guard(settings):
    import json
    from types import SimpleNamespace

    from ekampus.llm import Assistant

    jev = FakeJev(requested=0.1, risky=0.9)
    box = make(settings, jev)
    a = Assistant(replace(box.s, llm_api_key="test"), box.store, engine=box.engine)
    a.toolbox.guard = box.guard
    a._model = "m"
    call_ = SimpleNamespace(id="c1", function=SimpleNamespace(name="forget", arguments=json.dumps({"id": 1})),
                            model_dump=lambda: {"id": "c1", "type": "function",
                                                "function": {"name": "forget", "arguments": "{\"id\": 1}"}})
    queue = [SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call_]))]),
             SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="Silmedim.", tool_calls=None))])]

    async def create(**kwargs):
        return queue.pop(0)

    a.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    box.store.memory_add("vize 20 Ekim", datetime.now(timezone.utc))
    reply = asyncio.run(a.chat("selam, nasılsın"))
    assert jev.states[0][0]["student_message"] == "selam, nasılsın"
    assert box.store.memory_list() != [] and "Durduruldu (güvenlik kontrolü): forget" in reply.text

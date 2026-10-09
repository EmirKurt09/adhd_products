"""Anahtara bağlı özellikler: LLM, JEV, Pushover. Anahtar yoksa çalışmaz, varsa /ayarlar'dan açılıp kapanır."""

from dataclasses import replace

import pytest

from ekampus import features as F
from ekampus import prefs as PR
from ekampus.config import GROUPS
from ekampus.engine import Engine
from ekampus.store import Store


@pytest.fixture
def keyed(settings):
    return replace(settings, llm_api_key="k", typesafe_api_key="k", pushover_app_token="a", pushover_user_key="u")


def test_every_feature_has_env_keys():
    assert {key for key, _ in F.FEATURES} <= set(GROUPS)


def test_missing_keys_are_named(settings):
    store = Store(":memory:")
    by_key = {s.key: s for s in F.states(settings, store)}
    assert by_key["llm"].describe() == "çalışmıyor: .env'de LLM_API_KEY yok"
    assert by_key["jev"].describe() == "çalışmıyor: .env'de TYPESAFE_API_KEY yok"
    assert by_key["pushover"].describe() == "çalışmıyor: .env'de PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY yok"
    assert not any(s.usable for s in by_key.values())
    assert [s.key for s in F.unavailable(settings, store)] == ["llm", "jev", "pushover"]


def test_toggle_turns_feature_off_and_on(keyed):
    engine = Engine(keyed, Store(":memory:"))
    assert all(engine.feature_on(k) for k, _ in F.FEATURES)
    PR.toggle(engine.store, "jev")
    assert not engine.feature_on("jev") and engine.feature_on("llm")
    assert F.state(keyed, engine.store, "jev").describe() == "ayarlardan kapalı"
    PR.toggle(engine.store, "jev")
    assert engine.feature_on("jev")
    assert F.unavailable(keyed, engine.store) == []


def test_toggled_off_without_key_still_reports_missing_key(settings):
    store = Store(":memory:")
    PR.toggle(store, "llm")
    assert F.state(settings, store, "llm").describe().startswith("çalışmıyor")


def test_watchdog_respects_pushover_toggle(keyed, monkeypatch):
    from ekampus import watchdog

    sent = []

    class FakePushover:
        def send_sync(self, text, priority=0):
            sent.append(priority)

    store = Store(keyed.db_path)
    PR.toggle(store, "pushover")
    store.close()
    monkeypatch.setattr(watchdog.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setattr(watchdog.logging, "shutdown", lambda: None)
    with pytest.raises(SystemExit):
        watchdog._restart(keyed, FakePushover(), 700)
    assert sent == [] and keyed.watchdog_marker_path.exists()  # yeniden başlatma yine olur, sadece Pushover'a gitmez

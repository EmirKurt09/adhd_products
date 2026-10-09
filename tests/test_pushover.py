"""Pushover kanalı, uyarı yönlendirmesi, hata artışı, çökme algılama ve bekçi (ağ çağrısı yok)."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from ekampus import prefs as PR
from ekampus.engine import Engine
from ekampus.models import Event
from ekampus.pushover import MAX_MESSAGE, Pushover, PushoverError, to_pushover_html
from ekampus.store import Store
from ekampus.watchdog import heartbeat_age

DAY = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)


# ── Pushover istemcisi ────────────────────────────────────────────────────────

def mock(status_code: int = 200, body: dict | None = None):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()} | {"_url": str(request.url)})
        return httpx.Response(status_code, json=body if body is not None else {"status": 1, "request": "r"})

    transport = httpx.MockTransport(handler)
    return Pushover("APPTOKEN", "USERKEY", transport=transport, async_transport=transport), seen


def test_html_is_reduced_to_pushover_subset():
    text = "<b>Başlık</b>\n<code>HTTP 503</code> <s>eski</s> <i>not</i> <pre>x</pre>"
    assert to_pushover_html(text) == "<b>Başlık</b>\nHTTP 503 eski <i>not</i> x"


def test_long_message_is_clipped_without_broken_tag():
    out = to_pushover_html("a" * (MAX_MESSAGE - 3) + "<b>uzun kısım</b>")
    assert len(out) <= MAX_MESSAGE and not out.rstrip("…").endswith("<")


def test_send_payload():
    p, seen = mock()
    asyncio.run(p.send("<b>Çöktü</b>", priority=1))
    assert seen[0]["token"] == "APPTOKEN" and seen[0]["user"] == "USERKEY"
    assert seen[0]["priority"] == "1" and seen[0]["html"] == "1" and seen[0]["message"] == "<b>Çöktü</b>"
    assert seen[0]["_url"].endswith("/1/messages.json")


def test_emergency_priority_repeats_until_acknowledged():
    p, seen = mock()
    p.send_sync("x", priority=2)
    p.send_sync("x", priority=5)   # 2'den büyükler acile kısılır
    p.send_sync("x", priority=1)
    assert (seen[0]["priority"], seen[0]["retry"], seen[0]["expire"]) == ("2", "300", "3600")
    assert seen[1]["priority"] == "2"
    assert seen[2]["priority"] == "1" and "retry" not in seen[2]


@pytest.mark.parametrize("status_code,body", [(400, {"status": 0, "errors": ["application token is invalid"]}),
                                              (200, {"status": 0, "errors": ["user key is invalid"]}),
                                              (500, None)])
def test_errors_raise(status_code, body):
    p, _ = mock(status_code, body if body is not None else {})
    with pytest.raises(PushoverError):
        asyncio.run(p.send("x"))


def test_validate_returns_devices():
    p, seen = mock(body={"status": 1, "devices": ["telefon", "pc"]})
    assert asyncio.run(p.validate()) == "telefon, pc"
    assert seen[0]["_url"].endswith("/1/users/validate.json")


# ── Uyarı yönlendirmesi ───────────────────────────────────────────────────────

def make_engine(settings, channel=None):
    if channel is not None:  # kanal sadece anahtarlar varken kurulur
        settings = replace(settings, pushover_app_token="APPTOKEN", pushover_user_key="USERKEY")
    return Engine(settings, Store(":memory:"), alert_channel=channel)


def flush(engine, now=DAY):
    telegram: list[str] = []

    async def send(message, ctx):
        telegram.append(message.text)

    asyncio.run(engine.flush(send, now))
    return telegram


def test_alerts_go_to_pushover_and_content_stays_on_telegram(settings):
    pushed: list[tuple[str, int]] = []

    async def channel(text, priority):
        pushed.append((text, priority))

    engine = make_engine(settings, channel)
    engine.alert("x", "<b>sistem</b>", DAY, priority=1)
    engine.store.enqueue(Event("new", "new:assignment:1", {"kind": "assignment", "uid": "1", "title": "Ödev"}), DAY)
    telegram = flush(engine)
    assert pushed == [("<b>sistem</b>", 1)]
    assert len(telegram) == 1 and "Yeni ödev" in telegram[0]


def test_pushover_failure_falls_back_to_telegram(settings):
    async def broken(text, priority):
        raise PushoverError("user key is invalid")

    engine = make_engine(settings, broken)
    engine.alert("x", "sistem uyarısı", DAY)
    assert flush(engine) == ["sistem uyarısı"]
    assert engine.store.outbox_by_key("alert:x")["status"] == "sent"
    assert "pushover" in engine.store.get("errors")


def test_pushover_turned_off_in_settings_uses_telegram(settings):
    pushed = []

    async def channel(text, priority):
        pushed.append(text)

    engine = make_engine(settings, channel)
    PR.toggle(engine.store, "pushover")  # /ayarlar → Pushover kapalı
    engine.alert("x", "sistem uyarısı", DAY)
    assert flush(engine) == ["sistem uyarısı"] and pushed == []


def test_without_pushover_alerts_stay_on_telegram(settings):
    engine = make_engine(settings)
    engine.alert("x", "sistem uyarısı", DAY)
    assert flush(engine) == ["sistem uyarısı"]


# ── Hata artışı ───────────────────────────────────────────────────────────────

def spikes(engine) -> list:
    return [r for r in engine.store.db.execute("SELECT key, payload FROM outbox WHERE key LIKE 'alert:spike:%'")]


def test_error_spike_alerts_once_per_hour(settings):
    engine = make_engine(settings)  # eşik: 5 / saat
    for i in range(4):
        engine.record_error("bot", f"hata {i}", DAY + timedelta(minutes=i))
    assert spikes(engine) == []
    engine.record_error("llm", "zaman aşımı", DAY + timedelta(minutes=5))
    rows = spikes(engine)
    assert len(rows) == 1 and "son 1 saatte 5 hata" in rows[0]["payload"] and "bot: 4" in rows[0]["payload"]
    engine.record_error("bot", "yine", DAY + timedelta(minutes=10))
    assert len(spikes(engine)) == 1  # aynı saat içinde ikinci uyarı yok
    for i in range(5):
        engine.record_error("bot", "sonra", DAY + timedelta(hours=2, minutes=i))
    assert len(spikes(engine)) == 2  # bir saat sonra yeni artış yeni uyarı


def test_old_errors_do_not_count(settings):
    engine = make_engine(settings)
    for i in range(4):
        engine.record_error("bot", "eski", DAY - timedelta(hours=2))
    engine.record_error("bot", "yeni", DAY)
    assert spikes(engine) == []


# ── Çökme ve takılma ──────────────────────────────────────────────────────────

def test_crash_detected_on_next_start(settings):
    engine = make_engine(settings)
    engine.on_start(DAY)                      # ilk açılış: uyarı yok
    assert not list(engine.store.db.execute("SELECT 1 FROM outbox"))
    engine.on_start(DAY + timedelta(hours=1))  # düzgün kapanmadan tekrar açıldı = çökme
    rows = list(engine.store.db.execute("SELECT key, payload FROM outbox"))
    assert len(rows) == 1 and rows[0]["key"].startswith("alert:crash:") and "beklenmedik" in rows[0]["payload"]


def test_clean_exit_is_not_a_crash(settings):
    engine = make_engine(settings)
    engine.on_start(DAY)
    engine.on_clean_exit()
    engine.on_start(DAY + timedelta(hours=1))
    assert not list(engine.store.db.execute("SELECT 1 FROM outbox"))


def test_watchdog_restart_reported_once(settings):
    engine = make_engine(settings)
    engine.on_start(DAY)
    settings.watchdog_marker_path.write_text("heartbeat 700 sn", encoding="utf-8")
    engine.on_start(DAY + timedelta(minutes=12))
    rows = list(engine.store.db.execute("SELECT key FROM outbox"))
    assert [r["key"].split(":")[1] for r in rows] == ["restart"]  # çökme uyarısı ayrıca gelmez
    assert not settings.watchdog_marker_path.exists()


def test_heartbeat_age(tmp_path):
    hb = tmp_path / "heartbeat"
    assert heartbeat_age(hb) is None
    hb.write_text("x")
    mtime = hb.stat().st_mtime
    assert heartbeat_age(hb, now=mtime + 700) == pytest.approx(700)


# ── Öncelik tablosu (kullanıcının istediği ayar) ──────────────────────────────

def priorities(engine) -> dict[str, int]:
    out = {}
    for key, payload in engine.store.db.execute("SELECT key, payload FROM outbox WHERE type = 'alert'"):
        out[key.split(":")[1]] = __import__("json").loads(payload)["priority"]
    return out


def test_priority_table(settings):
    from ekampus.browser import LoginRejected

    engine = make_engine(settings)
    while PR.load(engine.store)["fail_after"] != 1:
        PR.toggle(engine.store, "fail_after")
    engine._failure(DAY, "timeout", now=DAY)                     # siteye girilemiyor
    engine.store.set("fail_since", (DAY - timedelta(hours=1)).isoformat())
    engine.store.db.execute("UPDATE outbox SET status = 'sent'")  # uyarı gitmiş olsun ki düzelme bildirilsin
    engine._recovered(DAY + timedelta(minutes=30))               # yeniden ulaşılıyor
    engine._auth_alert(LoginRejected("şifre yanlış"))            # giriş reddi
    engine._track_parse_errors({"course:1": "yapı yok"}, DAY)
    engine._track_parse_errors({"course:1": "yapı yok"}, DAY)    # 2. kez: yapı değişti uyarısı
    engine._track_anomalies(["düştü"], DAY)
    engine._track_anomalies(["düştü"], DAY)                      # 2. kez: anomali uyarısı
    for i in range(5):
        engine.record_error("bot", "x", DAY + timedelta(minutes=i))  # hata artışı
    engine.on_start(DAY)
    engine.on_start(DAY + timedelta(hours=1))                    # çökme
    settings.watchdog_marker_path.write_text("x", encoding="utf-8")
    engine.on_start(DAY + timedelta(hours=2))                    # takılma sonrası yeniden başladı
    assert priorities(engine) == {
        "fail": 1, "recovered": 0, "auth": 2, "parse": 1, "anomaly": 1, "spike": 2, "crash": 2, "restart": 1,
    }


def test_watchdog_alert_is_high_priority(settings, monkeypatch):
    from ekampus import watchdog

    p, seen = mock()
    monkeypatch.setattr(watchdog.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setattr(watchdog.logging, "shutdown", lambda: None)
    with pytest.raises(SystemExit) as exit_info:
        watchdog._restart(settings, p, 700)
    assert exit_info.value.code == watchdog.EXIT_CODE
    assert seen[0]["priority"] == "1" and settings.watchdog_marker_path.exists()

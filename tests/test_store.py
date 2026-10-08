from datetime import datetime, timedelta, timezone

from ekampus.models import Event
from ekampus.reminders import Planned
from ekampus.store import Store

T0 = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)


def test_outbox_dedupe_and_retry_backoff():
    store = Store(":memory:")
    assert store.enqueue(Event("new", "new:assignment:a", {"title": "x"}), T0) is True
    assert store.enqueue(Event("new", "new:assignment:a", {"title": "x"}), T0) is False

    (row,) = store.pending(T0)
    store.mark_failed(row["id"], "ağ hatası", T0)
    assert store.pending(T0) == []                              # backoff süresince bekler
    assert len(store.pending(T0 + timedelta(minutes=1))) == 1   # sonra tekrar dener

    store.mark_sent(row["id"], T0 + timedelta(minutes=1))
    assert store.pending(T0 + timedelta(days=1)) == []
    # gönderilmiş olay tekrar kuyruğa giremez
    assert store.enqueue(Event("new", "new:assignment:a", {"title": "x"}), T0) is False


def test_backoff_is_capped():
    store = Store(":memory:")
    store.enqueue(Event("new", "k", {}), T0)
    (row,) = store.pending(T0)
    for _ in range(12):
        store.mark_failed(row["id"], "hata", T0)
    assert len(store.pending(T0 + timedelta(minutes=30))) == 1


def test_skipped_reminders_are_never_sent():
    store = Store(":memory:")
    planned = [
        Planned("reminder:24h:a:x", "reminder", "skipped", {}),
        Planned("reminder:3h:a:x", "reminder", "pending", {}),
    ]
    assert store.enqueue_planned(planned, T0) == 2
    assert [r["key"] for r in store.pending(T0)] == ["reminder:3h:a:x"]
    # sonraki turlarda aynı plan tekrar gelirse hiçbir şey eklenmez
    assert store.enqueue_planned(planned, T0) == 0


def test_kv():
    store = Store(":memory:")
    assert store.get("x") is None
    store.set("x", "1")
    store.set("x", "2")
    assert store.get("x") == "2"

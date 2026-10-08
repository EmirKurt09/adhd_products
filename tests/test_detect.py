from datetime import datetime, timedelta, timezone

import pytest

from ekampus.detect import MISSING_TO_GONE, diff
from ekampus.models import SECONDARY, Item, Scan
from ekampus.store import Store

T0 = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 10, 20, 20, 59, tzinfo=timezone.utc)
SCOPE = ("assignment", "course:1")


def odev(uid: str, title: str = "Ödev", due: datetime | None = DUE, body: str = "açıklama", **kw) -> Item:
    return Item(kind="assignment", uid=uid, scope="course:1", title=title, course="Ders 1",
                due_at=due, body=body, **kw)


def scan(*items: Item, ok=(SCOPE,), failed=()) -> Scan:
    return Scan(items=list(items), ok_scopes=set(ok), failed_scopes={s: "hata" for s in failed})


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def run(store: Store, sc: Scan, now: datetime = T0):
    result = diff(store.stored_items(), store.known_scopes(), sc)
    store.apply(sc, result, now)
    return result


def types(result) -> list[str]:
    return [e.type for e in result.events]


def test_first_run_is_silent_baseline(store):
    result = run(store, scan(odev("a"), odev("b")))
    assert types(result) == ["baseline"]
    assert result.events[0].data["counts"] == {"assignment": 2}
    assert len(result.events[0].data["open_assignments"]) == 2


def test_new_item_after_baseline_emits_exactly_once(store):
    run(store, scan(odev("a")))
    result = run(store, scan(odev("a"), odev("b", "Yeni ödev")))
    assert types(result) == ["new"]
    assert result.events[0].data["title"] == "Yeni ödev"
    # aynı tarama tekrar işlenirse olay yok
    assert types(run(store, scan(odev("a"), odev("b", "Yeni ödev")))) == []


def test_replayed_event_is_not_queued_twice(store):
    run(store, scan(odev("a")))
    sc = scan(odev("a"), odev("b"))
    result = diff(store.stored_items(), store.known_scopes(), sc)
    store.apply(sc, result, T0)
    store.apply(sc, result, T0)  # aynı diff ikinci kez uygulanırsa (ör. yeniden başlatma)
    assert [r["key"] for r in store.pending(T0)].count("new:assignment:b") == 1


def test_due_change_is_its_own_event(store):
    run(store, scan(odev("a")))
    result = run(store, scan(odev("a", due=DUE + timedelta(days=2))))
    assert types(result) == ["due_changed"]
    assert result.events[0].data["old_due_at"] == "2026-10-20T20:59+00:00"
    assert result.events[0].data["content_changed"] is False


def test_due_and_content_change_reports_both_in_one_event(store):
    run(store, scan(odev("a")))
    result = run(store, scan(odev("a", due=DUE + timedelta(days=2), body="yeni açıklama")))
    assert types(result) == ["due_changed"]
    assert result.events[0].data["content_changed"] is True


def test_content_change(store):
    run(store, scan(odev("a")))
    assert types(run(store, scan(odev("a", body="güncellenmiş açıklama")))) == ["changed"]


def test_whitespace_and_case_noise_is_not_a_change(store):
    run(store, scan(odev("a", title="Proje Ödevi", body="satır 1\nsatır 2")))
    assert types(run(store, scan(odev("a", title="  proje  ödevi ", body="satır 1   satır 2​")))) == []


def test_meta_changes_do_not_trigger_events(store):
    run(store, scan(odev("a", meta={"submitted": False})))
    assert types(run(store, scan(odev("a", meta={"submitted": True})))) == []


def test_missing_never_emits_and_becomes_gone_after_threshold(store):
    run(store, scan(odev("a"), odev("b")))
    for i in range(MISSING_TO_GONE):
        result = run(store, scan(odev("a")))
        assert result.events == []
    assert store.stored_items()[("assignment", "b")].status == "gone"
    assert store.stored_items()[("assignment", "a")].status == "active"


def test_failed_scope_never_marks_missing(store):
    run(store, scan(odev("a"), odev("b")))
    for _ in range(MISSING_TO_GONE + 1):
        run(store, scan(ok=(), failed=(SCOPE,)))
    assert all(s.status == "active" and s.missing_count == 0 for s in store.stored_items().values())


def test_scope_returning_zero_is_anomaly_not_deletion(store):
    run(store, scan(odev("a"), odev("b")))
    result = run(store, scan())  # kapsam "başarılı" ama 0 kayıt: parser bozulmuş olabilir
    assert result.anomalies and result.missing == [] and result.events == []


def test_global_drop_is_anomaly_and_suppresses_change_events(store):
    run(store, scan(*(odev(str(i)) for i in range(6))))
    result = run(store, scan(odev("0", body="başka"), odev("1")))
    assert result.anomalies
    assert result.missing == []
    assert "changed" not in types(result)


def test_new_course_is_silent_with_single_scope_event(store):
    run(store, scan(odev("a")))
    yeni = Item(kind="assignment", uid="x", scope="course:2", title="Eski ödev", course="Ders 2", due_at=DUE)
    result = run(store, scan(odev("a"), yeni, ok=(SCOPE, ("assignment", "course:2"))))
    assert types(result) == ["scope_added"]
    assert result.events[0].data["items"][0]["title"] == "Eski ödev"


def test_secondary_source_cannot_change_primary_content(store):
    run(store, scan(odev("a", body="tam açıklama")))
    takvim = odev("a", body="", rank=SECONDARY)
    result = run(store, scan(takvim, ok=()))
    assert result.events == []
    assert store.stored_items()[("assignment", "a")].rank == 0


def test_secondary_then_primary_upgrade_is_silent(store):
    run(store, scan(odev("z")))
    takvim = odev("a", body="", rank=SECONDARY)
    assert types(run(store, scan(odev("z"), takvim))) == ["new"]  # takvimden gelen yeni ödev bildirilir
    assert types(run(store, scan(odev("z"), odev("a", body="tam açıklama")))) == []


def test_new_item_from_partial_scan_is_still_notified(store):
    run(store, scan(odev("a"), ok=(SCOPE, ("assignment", "course:2"))))
    result = run(store, scan(odev("a"), odev("b"), ok=(SCOPE,), failed=(("assignment", "course:2"),)))
    assert types(result) == ["new"]


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        odev("a", due=datetime(2026, 10, 20, 23, 59))


CAL = [SCOPE, ("event", "calendar"), ("live", "calendar")]


def test_due_added_later_to_dateless_assignment_notifies(store):
    run(store, scan(odev("a", due=None, body="", rank=SECONDARY), ok=CAL))
    result = run(store, scan(odev("a"), ok=CAL))  # hoca sonradan teslim tarihi ekledi
    assert types(result) == ["due_changed"]
    assert result.events[0].data["old_due_at"] is None


def test_first_calendar_read_upgrade_is_silent(store):
    run(store, scan(odev("a", due=None, body="", rank=SECONDARY)))  # takvim hiç okunamamıştı
    assert types(run(store, scan(odev("a"), ok=CAL))) == []

from datetime import datetime, timedelta, timezone

from ekampus.reminders import Due, plan_reminders

DUE = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)
HOURS = (24, 3)


def odev(first_seen=DUE - timedelta(days=7), due=DUE, **kw) -> Due:
    return Due(kind="assignment", uid="a", title="Ödev", course="Ders", due_at=due, first_seen=first_seen, **kw)


def plan(item: Due, now: datetime):
    return {(p.data.get("hours"), p.status) for p in plan_reminders([item], HOURS, 15, now)}


def test_nothing_before_first_threshold():
    assert plan(odev(), DUE - timedelta(hours=30)) == set()


def test_24h_reminder():
    assert plan(odev(), DUE - timedelta(hours=20)) == {(24, "pending")}


def test_only_most_urgent_sent_when_both_crossed():
    # bot kapalıydı, teslime 2 saat kala açıldı: sadece 3 saatlik gider, 24'lük atlanır
    assert plan(odev(), DUE - timedelta(hours=2)) == {(24, "skipped"), (3, "pending")}


def test_threshold_skipped_if_item_learned_after_it():
    # ödev teslime 10 saat kala eklendi: 24 saatlik anlamsız, "yeni ödev" mesajı yeterli
    item = odev(first_seen=DUE - timedelta(hours=10))
    assert plan(item, DUE - timedelta(hours=9)) == {(24, "skipped")}
    assert plan(item, DUE - timedelta(hours=2)) == {(24, "skipped"), (3, "pending")}


def test_submitted_done_or_past_due_get_nothing():
    now = DUE - timedelta(hours=2)
    assert plan(odev(submitted=True), now) == set()
    assert plan(odev(done_manual=True), now) == set()
    assert plan(odev(), DUE + timedelta(minutes=1)) == set()


def test_due_change_produces_new_keys():
    now = DUE - timedelta(hours=20)
    before = {p.key for p in plan_reminders([odev()], HOURS, 15, now)}
    moved = DUE + timedelta(hours=1)
    after = {p.key for p in plan_reminders([odev(due=moved)], HOURS, 15, now)}
    assert before and after and before.isdisjoint(after)


def test_live_lesson_window():
    start = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
    ders = Due(kind="live", uid="l", title="Canlı ders", course="Ders", due_at=start, first_seen=start - timedelta(days=1))
    assert plan_reminders([ders], HOURS, 15, start - timedelta(minutes=20)) == []
    assert [p.type for p in plan_reminders([ders], HOURS, 15, start - timedelta(minutes=10))] == ["live_soon"]
    assert [p.type for p in plan_reminders([ders], HOURS, 15, start + timedelta(minutes=5))] == ["live_soon"]
    assert plan_reminders([ders], HOURS, 15, start + timedelta(minutes=15)) == []

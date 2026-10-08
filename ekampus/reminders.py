"""Zamana bağlı olaylar: teslim hatırlatmaları ve canlı ders uyarıları. Saf fonksiyonlar.

Teslim hatırlatması kuralları:
  * Sadece aktif, teslim edilmemiş ve "teslim ettim" denmemiş ödevler.
  * Birden çok eşik aynı anda geçildiyse (bot kapalıydı vs.) sadece en acil olan gönderilir,
    diğerleri 'skipped' yazılır ki sonradan da gelmesin.
  * Ödevi eşik geçtikten SONRA öğrendiysek (ör. teslime 2 saat kala eklendi) o eşik atlanır;
    "yeni ödev" mesajı zaten kalan süreyi söylüyor.
  * Anahtar teslim tarihini içerir: tarih değişirse hatırlatmalar yeni tarihe göre yeniden kurulur.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

LIVE_GRACE = timedelta(minutes=10)  # ders başladıktan sonra da bu kadar süre link gönderilebilir


@dataclass(frozen=True)
class Due:
    """Hatırlatma hesabı için gereken kayıt özeti (store.active_items çıktısından)."""

    kind: str
    uid: str
    title: str
    course: str
    due_at: datetime
    first_seen: datetime
    url: str = ""
    submitted: bool = False
    done_manual: bool = False


@dataclass(frozen=True)
class Planned:
    key: str
    type: str
    status: str  # pending | skipped
    data: dict


def plan_reminders(items: list[Due], hours: tuple[int, ...], live_min: int, now: datetime) -> list[Planned]:
    planned: list[Planned] = []
    for item in items:
        if item.kind == "assignment":
            planned.extend(_assignment(item, hours, now))
        elif item.kind == "live":
            planned.extend(_live(item, live_min, now))
    return planned


def _assignment(item: Due, hours: tuple[int, ...], now: datetime) -> list[Planned]:
    if item.submitted or item.done_manual or item.due_at <= now:
        return []
    crossed = sorted(h for h in hours if now >= item.due_at - timedelta(hours=h))
    if not crossed:
        return []
    most_urgent = crossed[0]
    result = []
    for h in crossed:
        known_before = item.first_seen <= item.due_at - timedelta(hours=h)
        status = "pending" if h == most_urgent and known_before else "skipped"
        result.append(Planned(
            key=f"reminder:{h}h:{item.uid}:{item.due_at.isoformat(timespec='minutes')}",
            type="reminder",
            status=status,
            data={"hours": h, **_data(item)},
        ))
    return result


def _live(item: Due, live_min: int, now: datetime) -> list[Planned]:
    if not (item.due_at - timedelta(minutes=live_min) <= now < item.due_at + LIVE_GRACE):
        return []
    return [Planned(
        key=f"live:{item.uid}:{item.due_at.isoformat(timespec='minutes')}",
        type="live_soon",
        status="pending",
        data=_data(item),
    )]


def _data(item: Due) -> dict:
    return {
        "kind": item.kind, "uid": item.uid, "title": item.title, "course": item.course,
        "due_at": item.due_at.isoformat(timespec="minutes"), "url": item.url,
    }

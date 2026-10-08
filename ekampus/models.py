"""Scraper'ların ürettiği ve algılama katmanının tükettiği ortak veri tipleri."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

KINDS = ("assignment", "announcement", "grade", "live", "file", "event")

# Kaynak önceliği: 0 = asıl liste (ders sayfasındaki ödev listesi gibi),
# 1 = ikincil (takvim, bildirim kutusu). İkincil kaynak mevcut kaydın içeriğini değiştiremez.
PRIMARY, SECONDARY = 0, 1

_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[​-‍﻿]")


def normalize(text: str) -> str:
    return _WS.sub(" ", _ZERO_WIDTH.sub("", text or "")).strip().casefold()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError(f"saat dilimi olmayan tarih: {dt!r}")
    return dt.astimezone(timezone.utc).isoformat(timespec="minutes")


@dataclass(frozen=True)
class Item:
    kind: str
    uid: str                      # platformun kendi kimliği
    scope: str                    # taranan kapsam, genelde ders ("course:123")
    title: str
    course: str = ""              # gösterim için ders adı
    due_at: datetime | None = None  # teslim / başlangıç zamanı (saat dilimli)
    body: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict, compare=False)  # parmak izine girer (ekler, not değeri...)
    meta: dict = field(default_factory=dict, compare=False)   # parmak izine girmez (teslim durumu...)
    rank: int = PRIMARY

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"bilinmeyen tür: {self.kind}")
        if not self.uid or not self.title.strip():
            raise ValueError(f"{self.kind}: uid ve başlık zorunlu")
        iso(self.due_at)  # saat dilimi kontrolü

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.uid)

    @property
    def due_iso(self) -> str | None:
        return iso(self.due_at)

    def fingerprint(self) -> str:
        payload = [normalize(self.title), self.due_iso, normalize(self.body), _normalize_obj(self.extra)]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_obj(value):
    if isinstance(value, str):
        return normalize(value)
    if isinstance(value, dict):
        return {k: _normalize_obj(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return sorted((_normalize_obj(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


@dataclass
class Scan:
    """Bir tarama turunun sonucu. ok_scopes: hatasız taranan (tür, kapsam) çiftleri."""

    items: list[Item] = field(default_factory=list)
    ok_scopes: set[tuple[str, str]] = field(default_factory=set)
    failed_scopes: dict[tuple[str, str], str] = field(default_factory=dict)  # kapsam → hata

    @property
    def complete(self) -> bool:
        return not self.failed_scopes


@dataclass(frozen=True)
class StoredItem:
    kind: str
    uid: str
    scope: str
    title: str
    course: str
    due_iso: str | None
    fingerprint: str
    rank: int
    status: str          # active | gone
    missing_count: int
    first_seen: str


@dataclass(frozen=True)
class Event:
    type: str            # baseline | scope_added | new | due_changed | changed | reminder | live_soon | alert
    key: str             # outbox tekilleştirme anahtarı
    data: dict

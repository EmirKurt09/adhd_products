"""Önceki durum + yeni tarama → olaylar ve durum güncellemeleri. Saf fonksiyonlar; I/O yok.

Kurallar (bkz. plan, "Algılama stratejisi"):
  * İlk tarama (hiç kapsam bilinmiyorsa) sessiz baseline'dır; tek bir özet olayı üretir.
  * Sonradan beliren yeni kapsam (ör. yeni ders) da sessiz alınır; tek "scope_added" olayı üretir.
  * Kaybolan kayıt asla olay üretmez; sadece hatasız taranmış kapsamda MISSING_TO_GONE tur
    görünmezse sessizce 'gone' olur. Hatalı/anomalili kapsamlarda hiçbir şey eksik sayılmaz.
  * İkincil kaynak (takvim vb.) mevcut kaydın içeriğini değiştiremez, sadece "görüldü" der.
  * Anomali: önceden kaydı olan bir kapsam 0 dönerse ya da toplam yarıdan fazla düşerse
    o tur hiçbir kayıt eksik sayılmaz ve değişiklik olayı üretilmez.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime

from .models import Event, Item, Scan, StoredItem

MISSING_TO_GONE = 3
GLOBAL_DROP_MIN_ITEMS = 4      # bu kadardan az kayıtta toplam düşüş kontrolü yapılmaz
GLOBAL_DROP_RATIO = 0.5
CHANGE_KINDS = {"assignment", "announcement", "grade", "live"}  # içerik değişikliği bildirilenler
DUE_KINDS = {"assignment", "live", "event"}                      # tarih değişikliği bildirilenler
CALENDAR_SCOPE = ("event", "calendar")  # takvim en az bir kez başarıyla okunduysa bilinen kapsamlarda olur


@dataclass
class Diff:
    events: list[Event] = field(default_factory=list)
    upserts: list[Item] = field(default_factory=list)      # içeriği yazılacak kayıtlar
    seen: list[tuple[str, str]] = field(default_factory=list)  # sadece last_seen güncellenecekler
    missing: list[tuple[str, str]] = field(default_factory=list)
    new_scopes: list[tuple[str, str]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def merge_sources(items: list[Item]) -> list[Item]:
    """Aynı kayıt birden çok kaynaktan geldiyse en öncelikli (en düşük rank) olanı tut."""
    best: dict[tuple[str, str], Item] = {}
    for item in items:
        current = best.get(item.key)
        if current is None or item.rank < current.rank:
            best[item.key] = item
    return list(best.values())


def item_data(item: Item) -> dict:
    return {
        "kind": item.kind, "uid": item.uid, "scope": item.scope, "title": item.title,
        "course": item.course, "due_at": item.due_iso, "url": item.url, "body": item.body[:1500],
        "extra": item.extra, "meta": item.meta,
    }


def diff(stored: dict[tuple[str, str], StoredItem], known_scopes: set[tuple[str, str]], scan: Scan) -> Diff:
    result = Diff()
    items = merge_sources(scan.items)
    first_run = not known_scopes
    calendar_known = CALENDAR_SCOPE in known_scopes

    # ── Kapsamlar: yeni olanlar sessiz baseline ────────────────────────────
    scan_scopes = set(scan.ok_scopes) | {(i.kind, i.scope) for i in items}
    new_scopes = sorted(scan_scopes - known_scopes)
    result.new_scopes = new_scopes
    silent_scopes = set(new_scopes)

    # ── Anomali tespiti ───────────────────────────────────────────────────
    active_by_scope = Counter((s.kind, s.scope) for s in stored.values() if s.status == "active")
    now_by_scope = Counter((i.kind, i.scope) for i in items)
    anomalous: set[tuple[str, str]] = set()
    for scope in scan.ok_scopes:
        if active_by_scope[scope] >= 1 and now_by_scope[scope] == 0:
            anomalous.add(scope)
            result.anomalies.append(f"{scope[0]}/{scope[1]}: önceden {active_by_scope[scope]} kayıt vardı, şimdi 0")
    prev_total = sum(active_by_scope[s] for s in scan.ok_scopes)
    now_total = sum(now_by_scope[s] for s in scan.ok_scopes)
    global_anomaly = prev_total >= GLOBAL_DROP_MIN_ITEMS and now_total < prev_total * GLOBAL_DROP_RATIO
    if global_anomaly:
        result.anomalies.append(f"toplam kayıt {prev_total} → {now_total} düştü")

    # ── Kayıt bazında karşılaştırma ───────────────────────────────────────
    present = set()
    for item in items:
        present.add(item.key)
        old = stored.get(item.key)
        scope = (item.kind, item.scope)

        if old is None:
            result.upserts.append(item)
            if not first_run and scope not in silent_scopes:
                result.events.append(Event("new", f"new:{item.kind}:{item.uid}", item_data(item)))
            continue

        if item.rank > old.rank:
            result.seen.append(item.key)  # ikincil kaynak asıl kaydın içeriğine dokunamaz
            continue
        result.upserts.append(item)
        if item.rank < old.rank:
            # Kaynak yükseltmesi sessizdir; tek istisna: takvim zaten izleniyorken tarihsiz bir ödeve
            # sonradan teslim tarihi eklenmesi (ilk takvim okumasında bu kural toplu sahte bildirim üretmesin)
            if calendar_known and item.kind in DUE_KINDS and item.due_iso and old.due_iso is None:
                data = {**item_data(item), "old_due_at": None, "content_changed": False}
                result.events.append(Event("due_changed", f"due:{item.kind}:{item.uid}:{item.due_iso}", data))
            continue
        if old.status != "active" or scope in anomalous or global_anomaly:
            continue  # geri dönen kayıt ya da şüpheli tur: sessiz güncelle

        due_changed = item.kind in DUE_KINDS and old.due_iso != item.due_iso and item.due_iso is not None
        if due_changed:
            data = {**item_data(item), "old_due_at": old.due_iso, "content_changed": _content_changed(old, item)}
            result.events.append(Event("due_changed", f"due:{item.kind}:{item.uid}:{item.due_iso}", data))
        elif item.kind in CHANGE_KINDS and old.fingerprint != item.fingerprint():
            data = {**item_data(item), "old_title": old.title}
            result.events.append(Event("changed", f"changed:{item.kind}:{item.uid}:{item.fingerprint()}", data))

    # ── Kaybolanlar: sadece hatasız ve şüphesiz kapsamlarda say ───────────
    if not global_anomaly:
        for key, old in stored.items():
            scope = (old.kind, old.scope)
            if key in present or old.status != "active":
                continue
            if scope in scan.ok_scopes and scope not in anomalous and scope not in scan.failed_scopes:
                result.missing.append(key)

    # ── Baseline / yeni kapsam olayları ───────────────────────────────────
    if first_run:
        counts = Counter(i.kind for i in items)
        open_assignments = [item_data(i) for i in items if i.kind == "assignment" and not i.meta.get("submitted")]
        result.events.append(Event("baseline", "baseline", {
            "counts": dict(counts), "open_assignments": open_assignments,
            "failed_scopes": {f"{k}/{s}": e for (k, s), e in scan.failed_scopes.items()},
        }))
    else:
        for kind, scope in new_scopes:
            scope_items = [item_data(i) for i in items if (i.kind, i.scope) == (kind, scope)]
            if scope_items:
                result.events.append(Event("scope_added", f"scope:{kind}:{scope}", {
                    "kind": kind, "scope": scope, "course": scope_items[0]["course"], "items": scope_items,
                }))
    return result


def _content_changed(old: StoredItem, item: Item) -> bool:
    """Tarih dışında bir şey de değişti mi? (Tarihi eski haline çekip parmak izini karşılaştır.)"""
    old_due = datetime.fromisoformat(old.due_iso) if old.due_iso else None
    return replace(item, due_at=old_due).fingerprint() != old.fingerprint

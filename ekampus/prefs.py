"""Bildirim tercihleri (uyarı yöneticisi). store.kv içinde JSON olarak durur."""

from __future__ import annotations

import json

from .store import Store

CATEGORIES: list[tuple[str, str]] = [
    ("assignment", "Yeni ödev"),
    ("changes", "Ödev değişikliği"),
    ("reminders", "Teslim hatırlatma"),
    ("grades", "Notlar"),
    ("announcements", "Duyurular"),
    ("files", "Materyaller"),
    ("calendar", "Canlı ders / etkinlik"),
    ("digest", "Sabah özeti"),
    ("assistant", "Asistan uyarıları"),
    ("system", "Sistem uyarıları"),
]
FAIL_AFTER_CHOICES = (1, 2, 3, 5)
DEFAULTS: dict = {key: True for key, _ in CATEGORIES} | {"fail_after": 2, "night": True}

_KIND_CATEGORY = {
    "assignment": "assignment", "grade": "grades", "announcement": "announcements",
    "file": "files", "live": "calendar", "event": "calendar",
}


def load(store: Store) -> dict:
    try:
        saved = json.loads(store.get("prefs", "{}"))
    except json.JSONDecodeError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save(store: Store, prefs: dict) -> None:
    store.set("prefs", json.dumps({k: prefs[k] for k in DEFAULTS}))


def toggle(store: Store, key: str) -> dict:
    prefs = load(store)
    if key == "fail_after":
        choices = FAIL_AFTER_CHOICES
        current = prefs["fail_after"] if prefs["fail_after"] in choices else 2
        prefs["fail_after"] = choices[(choices.index(current) + 1) % len(choices)]
    elif key in DEFAULTS:
        prefs[key] = not prefs[key]
    save(store, prefs)
    return prefs


def category_of(event_type: str, payload: dict) -> str | None:
    """Bir bildirimin ait olduğu kategori. None: her zaman gönderilir (kurulum özeti, kritik uyarılar)."""
    if event_type in ("baseline", "scope_added"):
        return None
    if event_type == "alert":
        return None if payload.get("critical") else payload.get("category", "system")
    if event_type == "reminder":
        return "reminders"
    if event_type == "live_soon":
        return "calendar"
    kind = payload.get("kind", "")
    if event_type in ("changed", "due_changed") and kind == "assignment":
        return "changes"
    return _KIND_CATEGORY.get(kind)

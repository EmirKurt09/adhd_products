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
FEATURE_KEYS = ("llm", "jev", "pushover")  # anahtara bağlı özelliklerin aç/kapa durumu (bkz. features.py)
DEFAULTS: dict = ({key: True for key, _ in CATEGORIES} | {"fail_after": 2, "night": True}
                  | {key: True for key in FEATURE_KEYS})

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


def _name_list(store: Store, key: str) -> list[str]:
    try:
        return [str(x) for x in json.loads(store.get(key, "[]"))]
    except json.JSONDecodeError:
        return []


def muted_courses(store: Store) -> list[str]:
    """Bildirimi tamamen kapatılmış dersler (ders adı, sitedeki yazılışıyla)."""
    return _name_list(store, "muted_courses")


def set_course_muted(store: Store, course: str, muted: bool) -> list[str]:
    names = [c for c in muted_courses(store) if c != course] + ([course] if muted else [])
    store.set("muted_courses", json.dumps(names, ensure_ascii=False))
    return names


def course_muted(store: Store, course: str | None) -> bool:
    return bool(course) and course.casefold() in {c.casefold() for c in muted_courses(store)}


def muted_assignment_reminders(store: Store) -> set[str]:
    """Otomatik teslim hatırlatması susturulmuş ödevlerin uid'leri."""
    return set(_name_list(store, "muted_reminders"))


def set_assignment_reminders_muted(store: Store, uid: str, muted: bool) -> None:
    uids = (muted_assignment_reminders(store) | {uid}) if muted else (muted_assignment_reminders(store) - {uid})
    store.set("muted_reminders", json.dumps(sorted(uids)))


def set_fail_after(store: Store, failures: int) -> dict:
    """Siteye üst üste kaç kez erişilemeyince uyarı gelsin (1-10)."""
    prefs = load(store)
    prefs["fail_after"] = max(1, min(10, int(failures)))
    save(store, prefs)
    return prefs


def set_value(store: Store, key: str, value: bool) -> dict:
    """Aç/kapa ayarını doğrudan belirler (ajan "duyuruları kapat" dediğinde)."""
    prefs = load(store)
    if key not in DEFAULTS or key == "fail_after":
        raise KeyError(key)
    prefs[key] = bool(value)
    save(store, prefs)
    return prefs


def category_of(event_type: str, payload: dict) -> str | None:
    """Bir bildirimin ait olduğu kategori. None: her zaman gönderilir (kurulum özeti, kritik uyarılar)."""
    if event_type in ("baseline", "scope_added", "note"):  # note: kullanıcının açıkça kurduğu hatırlatma
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

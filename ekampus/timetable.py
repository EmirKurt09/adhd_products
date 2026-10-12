"""Haftalık ders programı: saat hesapları ve derse çıkış bildirimi planı. I/O yok; dersler store'dan dict olarak gelir.

Ders: {id, code, name, weekday (0=Pazartesi), start "HH:MM", end "HH:MM", room, campus, teacher, alert}
alert: None → kural (öğleden sonraki derslere, o gün zaten o kampüste değilse), 1 → her zaman, 0 → hiç.
campus boşsa ana kampüs sayılır.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
LEAD = timedelta(minutes=75)        # dersten bu kadar önce "yola çık" bildirimi
MORNING_BEFORE = dtime(12, 0)       # bu saatten önce başlayan derse bildirim gitmez (zaten evden çıkılıyor)
PLAN_AHEAD = timedelta(hours=26)
FIELDS = ("code", "name", "weekday", "start", "end", "room", "campus", "teacher")
TEXT_MAX = {"code": 20, "name": 120, "room": 40, "campus": 40, "teacher": 80}

_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇâÂîÎûÛ", "iissgguuooccaaiiuu")
_DAY_ALIASES = {"pzt": 0, "sal": 1, "car": 2, "crs": 2, "per": 3, "prs": 3, "cum": 4, "cmt": 5, "ctesi": 5, "paz": 6}
_CLOCK = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def fold(text: str) -> str:
    """Türkçe karakter ve büyük/küçük harf farkı olmadan, boşluksuz karşılaştırma için."""
    return re.sub(r"[\s\-_.]+", "", str(text or "").translate(_FOLD).casefold())


def parse_clock(text) -> str:
    m = _CLOCK.match(str(text or ""))
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError(f"saat 'HH:MM' biçiminde olmalı: {text!r}")
    return f"{int(m[1]):02d}:{m[2]}"


def parse_weekday(value) -> int:
    if isinstance(value, int) or str(value).strip().isdigit():
        day = int(value)
        if 0 <= day <= 6:
            return day
        raise ValueError("gün 0 (Pazartesi) ile 6 (Pazar) arasında olmalı")
    key = fold(value)
    names = [fold(n) for n in WEEKDAYS]
    if key in names:
        return names.index(key)
    if key in _DAY_ALIASES:
        return _DAY_ALIASES[key]
    matches = [i for i, n in enumerate(names) if len(key) >= 3 and n.startswith(key)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"bilinmeyen gün: {value!r}")


def clean(fields: dict, partial: bool = False) -> dict:
    """Ders alanlarını doğrular ve normalize eder. partial: sadece verilen alanlar (güncelleme)."""
    out: dict = {}
    for key in FIELDS:
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if key == "weekday":
            out[key] = parse_weekday(value)
        elif key in ("start", "end"):
            out[key] = parse_clock(value)
        else:
            out[key] = " ".join(str(value).split())[:TEXT_MAX[key]]
    if not partial:
        missing = [k for k in ("code", "name", "weekday", "start", "end") if not out.get(k) and out.get(k) != 0]
        if missing:
            raise ValueError("eksik alan: " + ", ".join(missing))
    return out


def check_times(start: str, end: str) -> None:
    if end <= start:
        raise ValueError("bitiş saati başlangıçtan sonra olmalı")


def label(lesson: dict) -> str:
    code, name = lesson.get("code") or "", lesson.get("name") or ""
    return f"{code} {name}".strip() if code and fold(code) not in fold(name) else (name or code)


def place(lesson: dict) -> str:
    """Sınıf ve (ana kampüs değilse) kampüs."""
    room = lesson.get("room") or "sınıf bilinmiyor"
    return f"{room} ({lesson['campus']})" if lesson.get("campus") else room


@dataclass(frozen=True)
class Occurrence:
    lesson: dict
    start: datetime
    end: datetime

    @property
    def date(self) -> date:
        return self.start.date()


def _at(day: date, clock: str, tz) -> datetime:
    h, m = map(int, clock.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=tz)


def occurrences(lessons: list[dict], since: datetime, days: int, tz) -> list[Occurrence]:
    """since'tan itibaren `days` gün içinde bitmemiş ders örnekleri, başlangıca göre sıralı."""
    local = since.astimezone(tz)
    found = []
    for offset in range(days + 1):
        day = local.date() + timedelta(days=offset)
        for lesson in lessons:
            if lesson["weekday"] != day.weekday():
                continue
            occ = Occurrence(lesson, _at(day, lesson["start"], tz), _at(day, lesson["end"], tz))
            if occ.end > local and occ.start < local + timedelta(days=days):
                found.append(occ)
    return sorted(found, key=lambda o: (o.start, o.lesson.get("id", 0)))


def now_or_next(lessons: list[dict], now: datetime, tz,
                skipped: set[tuple[int, str]] = frozenset()) -> tuple[Occurrence | None, Occurrence | None]:
    """(şu an süren ders, sıradaki ders); iptal edilen (skipped) örnekler atlanır. Program boşsa ikisi de None."""
    upcoming = [o for o in occurrences(lessons, now, 8, tz) if (o.lesson.get("id"), o.date.isoformat()) not in skipped]
    current = next((o for o in upcoming if o.start <= now < o.end), None)
    following = next((o for o in upcoming if o.start > now), None)
    return current, following


def find(lessons: list[dict], query: str) -> list[dict]:
    key = fold(query)
    if not key:
        return []
    return [l for l in lessons if key in fold(l.get("code", "")) + fold(l.get("name", ""))]


def next_occurrence(lesson: dict, now: datetime, tz) -> Occurrence | None:
    """Dersin bitmemiş en yakın örneği (bugün sürüyorsa bugünkü)."""
    found = occurrences([lesson], now, 7, tz)
    return found[0] if found else None


@dataclass(frozen=True)
class Departure:
    occurrence: Occurrence
    at: datetime  # bildirimin gönderileceği zaman

    @property
    def key(self) -> str:
        o = self.occurrence
        return f"depart:{o.lesson['id']}:{o.date.isoformat()}"


def wants_alert(occ: Occurrence, same_day: list[Occurrence]) -> bool:
    """Kural: sabah dersi değilse ve o gün daha önce aynı kampüste (iptal edilmemiş) ders yoksa."""
    forced = occ.lesson.get("alert")
    if forced is not None:
        return bool(forced)
    if occ.start.time() < MORNING_BEFORE:
        return False
    campus = fold(occ.lesson.get("campus") or "")
    return not any(o.end <= occ.start and fold(o.lesson.get("campus") or "") == campus for o in same_day)


def departures(lessons: list[dict], skipped: set[tuple[int, str]], now: datetime, tz,
               lead: timedelta = LEAD, ahead: timedelta = PLAN_AHEAD) -> list[Departure]:
    """Önümüzdeki saatlerde başlayacak dersler için "yola çık" bildirimleri.
    skipped: {(ders id, 'YYYY-MM-DD')}, öğrencinin iptal ettiği (o haftaki) dersler. Başlamış derse bildirim kurulmaz."""
    def live(o: Occurrence) -> bool:
        return (o.lesson["id"], o.date.isoformat()) not in skipped

    # aynı günün önceki dersleri de görünsün diye bir gün geriden başlanır
    window = occurrences(lessons, now - timedelta(days=1), ahead.days + 3, tz)
    plan = []
    for occ in window:
        if occ.start <= now or occ.start > now + ahead or not live(occ):
            continue
        same_day = [o for o in window if o.date == occ.date and o is not occ and live(o)]
        if wants_alert(occ, same_day):
            plan.append(Departure(occ, occ.start - lead))
    return plan


def view(occ: Occurrence, now: datetime, tz) -> dict:
    """Ajan araçları için tek ders örneği."""
    lesson = occ.lesson
    minutes = int((occ.start - now).total_seconds() // 60)
    out = {"id": lesson.get("id"), "ders": label(lesson), "sınıf": lesson.get("room") or "bilinmiyor",
           "gün": WEEKDAYS[occ.start.weekday()], "tarih": occ.date.isoformat(),
           "saat": f"{lesson['start']}-{lesson['end']}"}
    if lesson.get("campus"):
        out["kampüs"] = lesson["campus"]
    if lesson.get("teacher"):
        out["öğretim_üyesi"] = lesson["teacher"]
    if minutes > 0:
        out["başlamasına"] = _span(minutes)
    elif occ.end > now:
        out["bitmesine"] = _span(int((occ.end - now).total_seconds() // 60) + 1)
    return out


def _span(minutes: int) -> str:
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} gün"] * bool(days) + [f"{hours} sa"] * bool(hours) + [f"{mins} dk"] * bool(mins and not days)
    return " ".join(parts) or "1 dk"

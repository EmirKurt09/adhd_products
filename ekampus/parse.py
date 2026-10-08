"""Sayfa ayrıştırıcıları: HTML/JSON → yapılar. Saf fonksiyonlar (ağ yok), fixture'larla test edilir.

Parser canary: her ayrıştırıcı sayfada beklediği bir yapı işaretini arar. Bulamazsa sessizce boş liste
döndürmek yerine ParseError fırlatır; böylece "site değişti" durumu "yeni bir şey yok" ile karışmaz.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

from .config import STUDENT_URL
from .models import normalize


class ParseError(Exception):
    pass


_SPACE = re.compile(r"[ \t\r\f\v]+")
_GRADE = re.compile(r"Sonuç\s*:\s*([0-9]+(?:[.,][0-9]+)?)", re.I)
_ID = re.compile(r"/(\d+)(?:[/?#]|$)")
_COUNT_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_EMPTY_MARKERS = ("kaydı bulunamadı", "kayıt bulunamadı", "bulunamadı")

# Takvim TypeName → bizim türümüz. Bilinmeyen türler "event" olur: yeni bir tür asla kaçmaz.
LIVE_TYPES = ("vclass", "virtualclass", "virtual", "live", "meeting", "perculus", "zoom", "bbb", "teams")
EXAM_TYPES = ("exam", "quiz", "sinav", "test")


def text_of(node: Tag | None) -> str:
    return " ".join(node.get_text(" ").split()) if node else ""


def html_to_text(html: str | None) -> str:
    """Açıklama HTML'ini satır sonlarını koruyarak düz metne çevirir; linkleri metnin yanına yazar."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for a in soup.find_all("a", href=True):
        label = text_of(a)
        href = a["href"]
        a.replace_with(f"{label} ({href})" if label and label != href else href)
    for block in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "tr"]):
        block.insert_after(NavigableString("\n"))
        if block.name == "li":
            block.insert_before(NavigableString("• "))
    lines = [_SPACE.sub(" ", line).strip() for line in soup.get_text().splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _id_from(href: str) -> str | None:
    match = _ID.search(urlsplit(href).path + "/")
    return match.group(1) if match else None


def _is_empty_page(soup: BeautifulSoup) -> bool:
    main = soup.select_one(".pc-content") or soup.body or soup
    text = normalize(text_of(main))
    return any(marker in text for marker in _EMPTY_MARKERS)


# ── Eğitimlerim (/Course) ─────────────────────────────────────────────────────

@dataclass
class Course:
    id: str
    code: str
    name: str
    section: str = ""
    progress: int | None = None
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # {"Ödev": {"Toplam": 2, "Teslim Edilen": 2}}


def parse_course_list(html: str) -> list[Course]:
    soup = BeautifulSoup(html, "html.parser")
    courses: dict[str, Course] = {}
    for row in soup.select("table tr"):
        link = row.select_one('a[href*="/Course/Details/"][title]')
        if not link:
            continue
        course_id = _id_from(link["href"])
        if not course_id or course_id in courses:
            continue
        cells = row.find_all("td")
        section_link = [a for a in row.select('a[href*="/Course/Details/"]') if a is not link]
        pie = row.select_one("[data-percent]")
        counts: dict[str, dict[str, int]] = {}
        for pop in row.select("[data-bs-title][data-bs-content]"):
            label = text_of(BeautifulSoup(pop["data-bs-title"], "html.parser"))
            values = {}
            for part in re.split(r"<br\s*/?>", pop["data-bs-content"]):
                if ":" in part:
                    key, _, value = part.partition(":")
                    if value.strip().isdigit():
                        values[key.strip()] = int(value.strip())
            if label and values:
                counts[label] = values
        try:
            progress = int(pie["data-percent"]) if pie else None
        except ValueError:
            progress = None
        courses[course_id] = Course(
            id=course_id,
            code=link.get("title", "").strip() or (text_of(cells[0]) if cells else ""),
            name=text_of(link),
            section=text_of(section_link[0]) if section_link else "",
            progress=progress,
            counts=counts,
        )
    if not courses and not _is_empty_page(soup):
        raise ParseError("Eğitimlerim tablosu tanınmadı")
    return list(courses.values())


# ── Ders detayı (/Course/Details/{id}) ────────────────────────────────────────

@dataclass
class Activity:
    kind: str            # assignment | exam | live | <yol segmenti>
    id: str
    title: str
    url: str
    status: str = ""     # rozet metni ("Teslim Edildi", "Sonuç : 99,00" ...)
    grade: str | None = None
    submitted: bool = False


@dataclass
class Content:
    title: str
    url: str
    section: str
    icon: str = ""       # pdf, zip, video, link ...
    viewed: bool = False
    content_id: str | None = None  # sadece henüz açılmamış (enroll) içerikte bilinir


@dataclass
class CourseDetail:
    id: str
    code: str = ""
    name: str = ""
    section: str = ""
    instructors: list[str] = field(default_factory=list)
    contents: list[Content] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)


def _activity_kind(path: str) -> str:
    first = path.strip("/").split("/")[0].lower()
    if first == "assignment":
        return "assignment"
    if any(t in first for t in EXAM_TYPES):
        return "exam"
    if any(t in first for t in LIVE_TYPES):
        return "live"
    return first or "other"


def _icon_type(node: Tag | None) -> str:
    classes = " ".join(node.get("class", [])) if node else ""
    match = re.search(r"fa-file-(\w+)|fa-(video|link|youtube|play|globe|image)", classes)
    return (match.group(1) or match.group(2)) if match else ""


def parse_course_detail(html: str, course_id: str) -> CourseDetail:
    soup = BeautifulSoup(html, "html.parser")
    detail = CourseDetail(id=course_id)
    found_info = False
    for card in soup.select("div.card"):
        header = card.select_one(".card-header")
        if not header:
            continue
        heading = text_of(header.select_one("h5") or header)
        body = card.select_one(".card-body")
        if not body:
            continue

        if heading.startswith("Eğitim Bilgileri"):
            found_info = True
            for item in body.select("li"):
                label = text_of(item.select_one("label"))
                value_node = item.select(".flex-grow-1")
                value = text_of(value_node[-1]) if value_node else ""
                if label == "Kod":
                    detail.code = value
                elif label == "Eğitim":
                    detail.name = value
                elif label == "Şube":
                    detail.section = value
                elif label.startswith("Eğitmen"):
                    detail.instructors = [text_of(b) for b in item.select(".badge")] or [value]
        elif heading.startswith("Aktiviteler"):
            for item in body.select("li"):
                link = item.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                activity_id = _id_from(href)
                if not activity_id:
                    continue
                status = " ".join(text_of(b) for b in item.select(".badge"))
                grade_match = _GRADE.search(status)
                detail.activities.append(Activity(
                    kind=_activity_kind(urlsplit(href).path),
                    id=activity_id,
                    title=text_of(link),
                    url=urljoin(STUDENT_URL, href),
                    status=status,
                    grade=grade_match.group(1) if grade_match else None,
                    submitted=bool(grade_match) or "teslim edildi" in normalize(status),
                ))
        else:
            section = _COUNT_SUFFIX.sub("", heading)
            for item in body.select("li"):
                link = item.select_one('a[href*="/content/" i]')
                if not link:
                    continue
                href = link["href"]
                is_enroll = "/content/enroll/" in href.lower()
                detail.contents.append(Content(
                    title=text_of(link),
                    url=urljoin(STUDENT_URL, href),
                    section=section,
                    icon=_icon_type(item.select_one("i")),
                    viewed=not is_enroll,
                    content_id=_id_from(href) if is_enroll else None,
                ))
    if not found_info:
        raise ParseError(f"Ders {course_id}: 'Eğitim Bilgileri' kartı bulunamadı")
    return detail


# ── Takvim (/Schedule/Data) ───────────────────────────────────────────────────

@dataclass
class CalendarEntry:
    type: str            # sitenin TypeName değeri
    kind: str            # assignment | live | event
    uid: str
    course: str
    name: str
    start: datetime | None
    end: datetime | None
    description: str     # düz metin
    url: str


def _local(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def calendar_kind(type_name: str) -> str:
    t = (type_name or "").lower()
    if t == "assignment":
        return "assignment"
    if any(x in t for x in LIVE_TYPES):
        return "live"
    return "event"


def parse_schedule(body: str, tz: ZoneInfo) -> list[CalendarEntry]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise ParseError("Takvim yanıtı JSON değil") from None
    if not isinstance(data, list):
        raise ParseError("Takvim yanıtı liste değil")
    entries = []
    for raw in data:
        if not isinstance(raw, dict) or "Start" not in raw:
            raise ParseError("Takvim kaydı beklenen alanları içermiyor")
        type_name = str(raw.get("TypeName") or "")
        url = urljoin(STUDENT_URL, raw["Url"]) if raw.get("Url") else ""
        native_id = _id_from(raw["Url"]) if raw.get("Url") else None
        name = (raw.get("ScheduleName") or raw.get("Title") or "").strip()
        if native_id:
            uid = native_id if type_name.lower() == "assignment" else f"{type_name.lower()}:{native_id}"
        else:
            uid = f"{type_name.lower()}:" + hashlib.sha1(f"{raw.get('Title')}|{raw.get('Start')}".encode()).hexdigest()[:12]
        entries.append(CalendarEntry(
            type=type_name,
            kind=calendar_kind(type_name),
            uid=uid,
            course=(raw.get("CourseName") or "").strip(),
            name=name,
            start=_local(raw.get("Start"), tz),
            end=_local(raw.get("End"), tz),
            description=html_to_text(raw.get("Description")),
            url=url,
        ))
    return entries


# ── Duyurular (/Announce) ─────────────────────────────────────────────────────

@dataclass
class Announcement:
    uid: str
    title: str
    body: str
    course: str = ""
    date: str = ""
    url: str = ""


_DATE_TEXT = re.compile(r"\b\d{1,2}[./ ](?:\d{1,2}|[A-Za-zÇĞİÖŞÜçğıöşü]+)[./ ]\d{2,4}(?:\s+\d{1,2}:\d{2})?\b")


def parse_announcements(html: str) -> list[Announcement]:
    """Duyuru listesi. Henüz örnek görülmediği için yapıyı genel arar; tanıyamazsa ParseError verir."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one(".pc-content") or soup.body or soup
    candidates = [
        a for a in main.select('a[href*="nnounce" i], [data-url*="nnounce" i]')
        if "refresh" not in (a.get("href", "") + a.get("data-url", "")).lower()
        and "page-header-title" not in a.get("class", [])
        and not a.find_parent(class_="breadcrumb")
    ]
    items: dict[str, Announcement] = {}
    for node in candidates:
        href = node.get("data-url") or node.get("href", "")
        container = node.find_parent(["li", "tr", "article"]) or node.find_parent(class_="card") or node
        title = text_of(node) or text_of(container)[:120]
        body = text_of(container)
        if title and body.startswith(title):
            body = body[len(title):].strip()
        date_match = _DATE_TEXT.search(body)
        native = _id_from(href)
        uid = native or hashlib.sha1(f"{title}|{body[:300]}".encode()).hexdigest()[:16]
        items.setdefault(uid, Announcement(
            uid=uid, title=title, body=body, date=date_match.group(0) if date_match else "",
            url=urljoin(STUDENT_URL, href) if href else "",
        ))
    if not items and not _is_empty_page(soup):
        raise ParseError("Duyurular sayfasında içerik var ama yapı tanınmadı")
    return list(items.values())


# ── Sadece istek üzerine açılan sayfalar ──────────────────────────────────────

@dataclass
class AssignmentDetail:
    description: str
    attachments: list[tuple[str, str]]   # (ad, url)
    due_text: str = ""
    limit: str = ""
    status: str = ""


def parse_assignment_detail(html: str) -> AssignmentDetail:
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one(".card-body .alert")
    if not box:
        raise ParseError("Ödev açıklaması bulunamadı")
    attachments = [
        (text_of(a), a["href"]) for a in box.find_parent(class_="card-body").select('a[href*="/Files/"]')
        if "AssignmentSession" not in a["href"]
    ]
    fields = {}
    for item in soup.select("li.list-group-item"):
        label = text_of(item.select_one("label"))
        if label:
            fields[label] = text_of(item.select(".flex-shrink-0")[-1])
    description = box.decode_contents()
    for a in box.select('a[href*="/Files/"]'):
        description = description.replace(str(a), "")
    return AssignmentDetail(
        description=html_to_text(description),
        attachments=attachments,
        due_text=fields.get("Teslim Tarihi", ""),
        limit=fields.get("Limit", ""),
        status=fields.get("Durum", ""),
    )


def parse_content_file(html: str) -> str | None:
    """İçerik sayfasındaki asıl dosyanın adresi (pdf.js iframe'i ya da doğrudan dosya linki)."""
    soup = BeautifulSoup(html, "html.parser")
    for frame in soup.select("iframe[src]"):
        query = parse_qs(urlsplit(frame["src"]).query)
        if query.get("file"):
            return unquote(query["file"][0])
    for a in soup.select('a[href*="/Files/Uploads/"]'):
        if "/Company/" not in a["href"]:
            return urljoin(STUDENT_URL, a["href"])
    match = re.search(r"https://storage\.[^\"'\s]+/Files/Uploads/[^\"'\s]+/Course/[^\"'\s]+", html)
    return match.group(0) if match else None

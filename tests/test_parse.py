"""Ayrıştırıcılar ve kaynak birleştirme. Fixture'lar sitenin yapısını taklit eder, kişisel veri içermez."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ekampus import parse as P
from ekampus.models import PRIMARY, SECONDARY
from ekampus.scan import ScanReport, build_items

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Istanbul")


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_course_list():
    courses = P.parse_course_list(fixture("course_list.html"))
    assert [(c.id, c.code, c.name, c.section, c.progress) for c in courses] == [
        ("111", "BIL101", "Test Dersi", "A", 40), ("222", "BIL102", "Boş Ders", "B", 0),
    ]
    assert courses[0].counts == {"İçerik": {"Toplam": 5, "İzlenen": 2}, "Ödev": {"Toplam": 2, "Teslim Edilen": 1}}


def test_course_list_canary():
    with pytest.raises(P.ParseError):
        P.parse_course_list("<html><body><div class='pc-content'>Bakım çalışması</div></body></html>")


def test_course_detail():
    d = P.parse_course_detail(fixture("course_detail.html"), "111")
    assert (d.code, d.name, d.section, d.instructors) == ("BIL101", "Test Dersi", "A", ["HOCA BİR"])
    acts = {a.id: a for a in d.activities}
    assert acts["71"].grade == "87,50" and acts["71"].submitted
    assert acts["72"].grade is None and acts["72"].submitted
    assert not acts["73"].submitted
    files = {c.title: c for c in d.contents}
    assert files["Hafta 1 Notları"].viewed is False and files["Hafta 1 Notları"].content_id == "501"
    assert files["Örnek Kodlar"].viewed is True and files["Örnek Kodlar"].icon == "archive"
    assert files["Örnek Kodlar"].section == "Genel"


def test_course_detail_canary():
    with pytest.raises(P.ParseError):
        P.parse_course_detail("<html><body><div class='card'><div class='card-header'><h5>X</h5></div></div></body></html>", "1")


def test_schedule_kinds_and_timezone():
    entries = {e.uid: e for e in P.parse_schedule(fixture("schedule.json"), TZ)}
    assert entries["72"].kind == "assignment"
    assert entries["72"].end.isoformat() == "2026-10-15T23:59:00+03:00"
    assert entries["72"].description == "Birinci satır\nİkinci satır link (https://ornek.com)"
    assert entries["virtualclass:55"].kind == "live"
    assert entries["exam:9"].kind == "event"


def test_announcements_empty_and_canary():
    assert P.parse_announcements(fixture("announce_empty.html")) == []
    with pytest.raises(P.ParseError):
        P.parse_announcements(fixture("announce_unknown.html"))


def test_announcements_generic_list():
    html = """<html><body><div class="pc-content"><ul>
      <li><a href="/Announce/Details/12">Vize tarihleri</a> 12.10.2026 14:00 Vizeler 1 Kasım'da.</li>
      <li><a href="/Announce/Details/13">Ders iptali</a> Yarınki ders yok.</li></ul></div></body></html>"""
    items = {a.uid: a for a in P.parse_announcements(html)}
    assert items["12"].title == "Vize tarihleri" and items["12"].date == "12.10.2026 14:00"
    assert items["13"].body == "Yarınki ders yok."


def test_build_items_merges_sources():
    report = ScanReport(
        courses=P.parse_course_list(fixture("course_list.html")),
        details={"111": P.parse_course_detail(fixture("course_detail.html"), "111")},
        calendar=P.parse_schedule(fixture("schedule.json"), TZ),
    )
    items = {(i.kind, i.uid): i for i in build_items(report, [], schedule_ok=True)}

    b = items[("assignment", "72")]
    assert b.rank == PRIMARY and b.due_at is not None and b.meta["submitted"] is True
    assert b.scope == "course:111" and b.course == "Test Dersi"
    a = items[("assignment", "71")]  # takvimde yok → sadece ders sayfası → ikincil, tarihsiz
    assert a.rank == SECONDARY and a.due_at is None
    assert items[("grade", "assignment:71")].extra == {"value": "87,50"}
    assert ("grade", "assignment:72") not in items
    assert items[("live", "virtualclass:55")].scope == "calendar"
    assert items[("event", "exam:9")].title == "Vize"
    assert sum(1 for k in items if k[0] == "file") == 2


def test_file_uid_survives_viewing():
    """İçerik açılınca link enroll/{id} → details/{başka id} olur; kimlik değişmemeli."""
    report = ScanReport(courses=P.parse_course_list(fixture("course_list.html")))
    before = P.parse_course_detail(fixture("course_detail.html"), "111")
    after_html = fixture("course_detail.html").replace("/content/enroll/501?ceid=111&GroupId=9", "/content/details/9002?GroupId=9")
    after = P.parse_course_detail(after_html, "111")
    uid = lambda d: {i.title: i.uid for i in build_items(ScanReport(courses=report.courses, details={"111": d}), [], True) if i.kind == "file"}
    assert uid(before) == uid(after)

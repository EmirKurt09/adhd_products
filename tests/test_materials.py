"""Ders materyalleri menüsü: ders seçimi, bölümlere göre liste, sayfalama ve dosya adları."""

from pathlib import Path

import pytest

from ekampus import messages as M
from ekampus import parse as P
from ekampus.engine import material_filename
from ekampus.scan import ScanReport, build_items

FIX = Path(__file__).parent / "fixtures"


def material(uid: str, title: str, course_id: str = "1", course: str = "Ağlar", section: str = "Genel",
             icon: str = "pdf", order: int | None = 0, viewed: bool = True) -> dict:
    meta = {"viewed": viewed} | ({"order": order} if order is not None else {})
    return {"kind": "file", "uid": f"{course_id}:{uid}", "scope": f"course:{course_id}", "title": title,
            "course": course, "extra": {"section": section, "type": icon}, "meta": meta, "first_seen": uid}


@pytest.mark.parametrize("title,url,content_type,expected", [
    ("lecture 3", "https://storage.x/Files/Course/880/bil441-lec03.pdf", "", "lecture 3.pdf"),
    ("BIL453 Hafta 1", "https://storage.x/Files/Course/882/bil453hafta1.zip", "", "BIL453 Hafta 1.zip"),
    ("Ödev: 1/2 \"son\"", "https://storage.x/a/odev.PDF", "", "Ödev 1 2 son.pdf"),
    ("rapor.pdf", "https://storage.x/a/rapor.pdf", "", "rapor.pdf"),          # uzantı iki kez eklenmez
    ("slayt", "https://storage.x/download?id=5", "application/pdf", "slayt.pdf"),  # uzantı içerik türünden
    ("", "https://storage.x/a/ders%20notu.docx", "", "ders notu.docx"),        # başlık yoksa dosyanın adı
])
def test_material_filename(title, url, content_type, expected):
    assert material_filename(title, url, content_type) == expected


def test_courses_view_counts_and_buttons():
    rows = [material("a", "l1"), material("b", "l2", viewed=False),
            material("c", "Kitap", course_id="2", course="Sistem Programlama")]
    view = M.materials_courses_view(rows)
    assert "Ağlar: 2 materyal, 1 açılmamış" in view.text and "Sistem Programlama: 1 materyal" in view.text
    assert [b.data for row in view.buttons for b in row] == ["fc:1:0", "fc:2:0"]  # ada göre sıralı


def test_courses_view_empty():
    assert "Henüz materyal yok" in M.materials_courses_view([]).text


def test_list_groups_by_section_in_site_order_with_formats():
    rows = [material("c", "Hafta 2 notları", section="2.Hafta", icon="powerpoint", order=2),
            material("a", "Ders planı", section="Genel", icon="pdf", order=0),
            material("b", "Kodlar", section="Genel", icon="archive", order=1, viewed=False)]
    view = M.materials_list_view("1", rows, 0)
    text = view.text
    assert text.index("<b>Genel</b>") < text.index("Ders planı") < text.index("Kodlar") < text.index("<b>2.Hafta</b>")
    assert "2. Kodlar · ZIP · açılmadı" in text and "3. Hafta 2 notları · PowerPoint" in text
    assert view.buttons[0][0].text == "1. PDF · Ders planı" and view.buttons[0][0].data == "file:1:a"
    assert view.buttons[-1][0].data == "fc:list"


def test_list_pagination():
    rows = [material(f"{i:02d}", f"Ders {i}", order=i) for i in range(20)]
    first = M.materials_list_view("1", rows, 0)
    assert "sayfa 1/3" in first.text and "1. Ders 0" in first.text and "9. Ders 8" not in first.text
    nav = [b.data for b in first.buttons[-2]]
    assert nav == ["fc:1:1"]
    last = M.materials_list_view("1", rows, 2)
    assert "17. Ders 16" in last.text and [b.data for b in last.buttons[-2]] == ["fc:1:1"]
    assert M.materials_list_view("1", rows, 99).text == last.text  # taşan sayfa son sayfaya düşer


def test_callback_data_fits_telegram_limit():
    rows = [material("abcdef123456-2", "x" * 200, course_id="9999999")]
    view = M.materials_list_view("9999999", rows, 0)
    assert all(len((b.data or "").encode()) <= 64 for row in view.buttons for b in row)


def test_unknown_format_is_generic_and_items_without_order_still_listed():
    rows = [material("a", "Eski", icon="", order=None), material("b", "Yeni", icon="pdf", order=0)]
    text = M.materials_list_view("1", rows, 0).text
    assert text.index("Yeni") < text.index("Eski") and "Eski · Dosya" in text


def test_scan_records_site_order():
    report = ScanReport(courses=P.parse_course_list((FIX / "course_list.html").read_text(encoding="utf-8")),
                        details={"111": P.parse_course_detail((FIX / "course_detail.html").read_text(encoding="utf-8"), "111")})
    files = [i for i in build_items(report, [], True) if i.kind == "file"]
    assert [(i.title, i.meta["order"]) for i in files] == [("Hafta 1 Notları", 0), ("Örnek Kodlar", 1)]

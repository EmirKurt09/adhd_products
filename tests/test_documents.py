"""Ödev ayrıntısı, ekler ve PDF okuma (ağ ve site yok; indirme sahte)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pdfmaker import make_pdf

from ekampus import documents as D
from ekampus.agent import Toolbox, Turn
from ekampus.detect import diff
from ekampus.engine import Download, Engine
from ekampus.models import Item, Scan
from ekampus.parse import AssignmentDetail
from ekampus.store import Store

SLIDES = make_pdf(["Midterm on October 20\nRoom B-204", "Chapter 2 TCP handshake", "Chapter 3 Routing"])


@pytest.fixture
def box(settings):
    engine = Engine(settings, Store(":memory:"))
    now = datetime.now(timezone.utc)
    items = [
        Item(kind="assignment", uid="7", scope="course:1", title="Lab Raporu", course="Ağlar", due_at=now + timedelta(days=2)),
        Item(kind="file", uid="1:1", scope="course:1", title="lecture 1", course="Ağlar", extra={"icon": "pdf"}),
        Item(kind="file", uid="1:2", scope="course:1", title="lab.zip", course="Ağlar", extra={"icon": "archive"}),
    ]
    scan = Scan(items=items, ok_scopes={("assignment", "course:1"), ("file", "course:1")})
    engine.store.apply(scan, diff({}, set(), scan), now)
    engine.fetches = []

    async def fetch_material(uid):
        engine.fetches.append(uid)
        return Download("lab.zip", b"PK\x03\x04zip", "https://x/lab.zip") if uid == "1:2" else \
            Download("lecture 1.pdf", SLIDES, "https://x/l1.pdf")

    async def assignment_detail(uid):
        return AssignmentDetail("Raporu yükleyin.", [("Odev.pdf", "https://x/odev.pdf"), ("veri.csv", "https://x/v.csv")],
                                limit="10 MB")

    async def download(url, title=""):
        engine.fetches.append(url)
        return Download(title, make_pdf(["Submit as PDF before Friday"]), url)

    engine.fetch_material, engine.assignment_detail, engine.download = fetch_material, assignment_detail, download
    return Toolbox(settings, engine.store, engine=engine)


def call(box, name, args, turn=None):
    turn = turn or Turn()
    return asyncio.run(box.call(name, args, turn)), turn


def test_window_pages_through_long_documents():
    pages = [f"sayfa {i} " + "x" * 3000 for i in range(1, 8)]
    text, last = D.window(pages, 1)
    assert text.startswith("[Sayfa 1]") and last == 2  # 9000 karakterlik bütçeye iki sayfa sığar
    text, last = D.window(pages, 7)
    assert text.startswith("[Sayfa 7]") and last == 7
    with pytest.raises(D.NotReadable):
        D.pdf_pages(b"%PDF-1.4 bozuk")


def test_read_material_pdf_and_cache(box):
    result, turn = call(box, "read_document", {"source": "material", "uid": "1:1"})
    assert result["sayfa_sayısı"] == 3 and "Room B-204" in result["metin"] and result["devamı"] is None
    assert turn.actions == []  # okumak durum değiştirmez
    again, _ = call(box, "read_document", {"source": "material", "uid": "1:1", "page_from": 3})
    assert again["okunan_sayfalar"] == "3-3" and again["metin"].startswith("[Sayfa 3]")
    assert box.engine.fetches == ["1:1"]  # ikinci soruda siteye gidilmedi


def test_non_pdf_and_scanned(box):
    assert call(box, "read_document", {"source": "material", "uid": "1:2"})[0]["hata"] == "şimdilik sadece PDF okunabiliyor"

    async def scanned(uid):
        return Download("tarama.pdf", make_pdf([""]), "https://x/t.pdf")

    box.engine.fetch_material = scanned
    box.store.db.execute("UPDATE items SET fingerprint = 'yeni' WHERE uid = '1:1'")  # önbellek anahtarı değişsin
    assert "taranmış" in call(box, "read_document", {"source": "material", "uid": "1:1"})[0]["hata"]


def test_assignment_details_attachments_and_reading(box):
    details, _ = call(box, "assignment_details", {"uid": "7"})
    assert details["dosya_sınırı"] == "10 MB" and details["açıklama"] == "Raporu yükleyin."
    assert details["ekler"] == [{"no": 0, "ad": "Odev.pdf", "pdf": True}, {"no": 1, "ad": "veri.csv", "pdf": False}]
    assert "teklif" in details["not"]
    doc, _ = call(box, "read_document", {"source": "attachment", "uid": "7", "no": 0})
    assert "before Friday" in doc["metin"] and doc["başlık"] == "Odev.pdf"
    _, turn = call(box, "send_attachment", {"uid": "7", "no": 0})
    assert turn.attachments == [("7", 0)] and turn.actions == ["Ek gönderiliyor: Odev.pdf"]
    assert "hata" in call(box, "send_attachment", {"uid": "7", "no": 5})[0]


def test_site_tools_are_chat_only(box):
    triage = {t["function"]["name"] for t in box.specs("triage") or []}
    assert not {"assignment_details", "read_document", "send_attachment"} & triage
    result, _ = call(box, "read_document", {"source": "material", "uid": "1:1"}, Turn(mode="triage"))
    assert result == {"hata": "bu araç burada kullanılamaz"} and box.engine.fetches == []


def test_sending_a_pdf_suggests_reading_it(box):
    pdf, _ = call(box, "send_file", {"uid": "1:1"})
    zipped, _ = call(box, "send_file", {"uid": "1:2"})
    assert "teklif" in pdf["not"] and "not" not in zipped
    files = box.run_read("list_files", {})
    assert {f["title"]: f["biçim"] for f in files} == {"lecture 1": "PDF", "lab.zip": "ZIP"}

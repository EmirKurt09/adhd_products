import pytest

from ekampus.university import UniversityError, parse_listing, parse_detail, public_url


LIST = '''<div class="post-item"><h2 class="post-title"><a href="/duyuru/">Örnek sınav duyurusu</a></h2>
<div class="post-date">Ekim 12, 2026</div><div class="post-excerpt">JEV'e gitmeyecek metin</div></div>'''


def test_listing_reads_title_date_url_and_pagination():
    rows, next_page = parse_listing(LIST + '<a class="next page-numbers" href="/category/duyurular/page/2/">Sonraki</a>')
    assert rows[0].title == "Örnek sınav duyurusu"
    assert rows[0].published == "2026-10-12"
    assert rows[0].url == "https://ticaret.edu.tr/duyuru/"
    assert next_page.endswith("/page/2/")
    assert not hasattr(rows[0], "body")


@pytest.mark.parametrize("html", ["<html>bakım</html>", LIST.replace("Ekim 12, 2026", "belirsiz"),
                                   LIST.replace('class="post-date"', 'class="missing"')])
def test_broken_listing_is_not_silent_baseline(html):
    with pytest.raises(UniversityError):
        parse_listing(html)


def test_detail_extracts_body_and_deduplicated_pdf_only():
    html = '''<nav>Menü</nav><div class="col"><div class="header">tarih</div><h2 class="title">Başlık</h2>
    <p>Öğrenciler için açıklama</p><a href="/uploads/ek.PDF">Program</a>
    <a href="/uploads/ek.PDF#page=1">Tekrar</a><a href="/afis.jpg">Afiş</a></div>
    <h2>Diğer duyurular</h2><a href="/diger.pdf">Başka ek</a>'''
    body, files = parse_detail(html, "https://ticaret.edu.tr/duyuru/")
    assert "Öğrenciler için açıklama" in body
    assert "Menü" not in body and "Diğer duyurular" not in body and "tarih" not in body
    assert len(files) == 1
    assert files[0]["url"] == "https://ticaret.edu.tr/uploads/ek.PDF"


def test_pdf_only_detail_does_not_need_text_extraction():
    body, files = parse_detail('<div><h2 class="title">Başlık</h2><a href="/ek.pdf">Tıklayınız</a></div>',
                               "https://ticaret.edu.tr/duyuru/")
    assert body == "Tıklayınız" and len(files) == 1


@pytest.mark.parametrize("url", ["https://evil.test/a.pdf", "file:///secret", "https://ticaret.edu.tr.evil.test/a",
                                 "https://user:pass@ticaret.edu.tr/a"])
def test_public_url_rejects_other_destinations(url):
    with pytest.raises(UniversityError):
        public_url(url)

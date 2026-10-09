"""Belge okuma: PDF'den sayfa sayfa metin çıkarır (pypdf). Ajan materyali ya da ödev ekini okuyup soruları cevaplar.

Taranmış (resim) PDF'lerde metin olmayabilir; o zaman bunu açıkça söyleriz, tahmin etmeyiz.
"""

from __future__ import annotations

import io
import logging
import re

from pypdf import PdfReader

log = logging.getLogger(__name__)

MAX_PAGES = 300
WINDOW_CHARS = 9000  # tek araç cevabında modele giden en fazla metin


class NotReadable(Exception):
    pass


def is_pdf(data: bytes | None, filename: str = "") -> bool:
    return bool(data) and (data[:5] == b"%PDF-" or filename.lower().endswith(".pdf"))


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text or "")
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_pages(data: bytes, max_pages: int = MAX_PAGES) -> list[str]:
    """Her sayfanın metni (sayfa sırasıyla). Okunamayan sayfa boş metin olur."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise NotReadable("PDF şifreli")
        pages = []
        for page in reader.pages[:max_pages]:
            try:
                pages.append(_clean(page.extract_text() or ""))
            except Exception as e:  # noqa: BLE001 - tek sayfanın hatası belgenin tamamını bozmasın
                log.info("PDF sayfası okunamadı: %s", e)
                pages.append("")
        return pages
    except NotReadable:
        raise
    except Exception as e:  # noqa: BLE001 - bozuk dosya
        raise NotReadable(f"PDF açılamadı: {type(e).__name__}") from None


def window(pages: list[str], start: int, budget: int = WINDOW_CHARS) -> tuple[str, int]:
    """start (1'den başlar) sayfasından itibaren bütçeye sığan sayfalar: (metin, okunan son sayfa)."""
    start = min(max(1, start), max(1, len(pages)))
    parts: list[str] = []
    used = 0
    last = start - 1
    for number in range(start, len(pages) + 1):
        block = f"[Sayfa {number}]\n{pages[number - 1]}"
        if parts and used + len(block) > budget:
            break
        parts.append(block[:budget])
        used += len(block)
        last = number
    return "\n\n".join(parts), last

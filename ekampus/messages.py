"""Telegram mesaj metinleri (HTML parse mode). Saf fonksiyonlar: veri → (metin, butonlar).

Biçim ilkesi: süs emojisi yok. Durum ve aciliyet yazıyla ve kalınlıkla anlatılır.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

GUNLER = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
GUNLER_UZUN = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
AYLAR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
AYLAR_UZUN = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
              "Kasım", "Aralık"]

KIND_LABEL = {
    "assignment": "ödev", "announcement": "duyuru", "grade": "not", "live": "canlı ders",
    "file": "ders materyali", "event": "etkinlik",
}


@dataclass
class Button:
    text: str
    data: str | None = None   # callback verisi
    url: str | None = None


@dataclass
class Message:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    silent: bool = False      # bildirim sesi olmadan


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def fmt_dt(dt: datetime | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    if dt is None:
        return "tarih yok"
    local = dt.astimezone(tz)
    today = (now or datetime.now(tz)).astimezone(tz).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"bugün {clock}"
    if local.date() == today + timedelta(days=1):
        return f"yarın {clock}"
    if local.date() == today - timedelta(days=1):
        return f"dün {clock}"
    return f"{local.day} {AYLAR[local.month - 1]} {GUNLER[local.weekday()]} {clock}"


def remaining(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "süresi geçti"
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    if days >= 2:
        return f"{days} gün kaldı"
    if days == 1:
        return f"1 gün {hours} sa kaldı"
    if hours >= 1:
        return f"{hours} sa {minutes} dk kaldı"
    return f"{minutes} dk kaldı"


def left(dt: datetime | None, now: datetime) -> str:
    """Kalan süre; 24 saatten azsa kalın (göz ilk ona gitsin)."""
    text = remaining(dt, now)
    if dt is not None and 0 < (dt - now).total_seconds() <= 24 * 3600:
        return f"<b>{text}</b>"
    return text


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _course(data: dict) -> str:
    return f"\n<i>{escape(data['course'])}</i>" if data.get("course") else ""


def _link_row(data: dict, label: str = "Sitede aç") -> list[Button]:
    return [Button(label, url=data["url"])] if data.get("url", "").startswith("https://") else []


def assignment_buttons(data: dict) -> list[list[Button]]:
    rows = [[Button("Detay", data=f"det:{data['uid']}"), Button("Teslim ettim", data=f"done:{data['uid']}")]]
    link = _link_row(data)
    if link:
        rows.append(link)
    return rows


# ── Olay → mesaj ──────────────────────────────────────────────────────────────

def render_event(event_type: str, data: dict, tz: ZoneInfo, now: datetime) -> Message:
    kind = data.get("kind", "")
    title = escape(data.get("title", ""))
    due = parse_dt(data.get("due_at"))

    if event_type == "new" and kind == "assignment":
        lines = [f"<b>Yeni ödev:</b> {title}{_course(data)}"]
        lines.append(f"Teslim: <b>{fmt_dt(due, tz, now)}</b>" + (f" · {left(due, now)}" if due else ""))
        start = parse_dt((data.get("extra") or {}).get("start"))
        if start and start > now:
            lines.append(f"Başlangıç: {fmt_dt(start, tz, now)}")
        if data.get("body"):
            lines.append(f"\n{escape(clip(data['body'], 500))}")
        return Message("\n".join(lines), assignment_buttons(data))

    if event_type == "new" and kind == "announcement":
        lines = [f"<b>Yeni duyuru:</b> {title}{_course(data)}"]
        if data.get("body"):
            lines.append(f"\n{escape(clip(data['body'], 1200))}")
        return Message("\n".join(lines), [_link_row(data)] if _link_row(data) else [])

    if event_type == "new" and kind == "grade":
        value = escape((data.get("extra") or {}).get("value", "?"))
        return Message(f"<b>Not girildi:</b> {title} → <b>{value}</b>{_course(data)}",
                       [_link_row(data)] if _link_row(data) else [])

    if event_type == "new" and kind == "file":
        section = (data.get("extra") or {}).get("section")
        text = f"<b>Yeni ders materyali:</b> {title}{_course(data)}" + (f"\nBölüm: {escape(section)}" if section else "")
        return Message(text, [[Button("Gönder", data=f"file:{data['uid']}")]], silent=True)

    if event_type == "new" and kind == "live":
        return Message(
            f"<b>Yeni canlı ders:</b> {title}{_course(data)}\nZaman: {fmt_dt(due, tz, now)}",
            [_link_row(data, "Ders sayfası")] if _link_row(data) else [],
        )

    if event_type == "new":
        type_name = (data.get("extra") or {}).get("type") or KIND_LABEL.get(kind, kind)
        return Message(
            f"<b>Yeni {escape(type_name.lower())}:</b> {title}{_course(data)}\nZaman: {fmt_dt(due, tz, now)}",
            [_link_row(data)] if _link_row(data) else [],
        )

    if event_type == "due_changed":
        old = parse_dt(data.get("old_due_at"))
        label = KIND_LABEL.get(kind, kind)
        if old is None:
            head = f"<b>Teslim tarihi eklendi:</b> {title}"
        else:
            head = f"<b>{label.capitalize()} tarihi değişti:</b> {title}"
        lines = [head + _course(data)]
        if old is not None:
            lines.append(f"Eski: <s>{fmt_dt(old, tz, now)}</s>")
        lines.append(f"Yeni: <b>{fmt_dt(due, tz, now)}</b> · {left(due, now)}")
        if data.get("content_changed"):
            lines.append("Açıklama da güncellendi.")
        buttons = assignment_buttons(data) if kind == "assignment" else ([_link_row(data)] if _link_row(data) else [])
        return Message("\n".join(lines), buttons)

    if event_type == "changed":
        if kind == "grade":
            value = escape((data.get("extra") or {}).get("value", "?"))
            return Message(f"<b>Not güncellendi:</b> {title} → <b>{value}</b>{_course(data)}")
        label = KIND_LABEL.get(kind, kind)
        lines = [f"<b>{label.capitalize()} güncellendi:</b> {title}{_course(data)}"]
        if data.get("body"):
            lines.append(f"\n{escape(clip(data['body'], 600))}")
        buttons = assignment_buttons(data) if kind == "assignment" else []
        return Message("\n".join(lines), buttons)

    if event_type == "reminder":
        hours = data.get("hours")
        return Message(
            f"<b>Hatırlatma, {hours} saat kaldı:</b> {title}{_course(data)}\n"
            f"Teslim: <b>{fmt_dt(due, tz, now)}</b> · {left(due, now)}\n"
            "Teslim ettiysen butona bas, bu ödev için hatırlatmayı keserim.",
            assignment_buttons(data),
        )

    if event_type == "live_soon":
        return Message(
            f"<b>Canlı ders başlıyor:</b> {title}{_course(data)}\nZaman: {fmt_dt(due, tz, now)}",
            [_link_row(data, "Derse git")] if _link_row(data) else [],
        )

    if event_type == "scope_added":
        items = data.get("items", [])
        names = "\n".join(f"• {escape(i['title'])}" for i in items[:10])
        more = f"\n… ve {len(items) - 10} tane daha" if len(items) > 10 else ""
        label = KIND_LABEL.get(data.get("kind", ""), "kayıt")
        return Message(
            f"<b>İzlemeye alındı:</b> {escape(data.get('course') or data.get('scope', ''))}\n"
            f"Mevcut {len(items)} {label}:\n{names}{more}",
            silent=True,
        )

    if event_type == "baseline":
        return render_baseline(data, tz, now)

    if event_type == "alert":
        return Message(data.get("text", "Uyarı"))

    if event_type == "note":
        return Message(f"<b>Hatırlatma:</b> {escape(clip(data.get('text', ''), 1000))}")

    if event_type == "explain":
        return Message(f"<b>Kısaca:</b> {title}{_course(data)}\n{escape(clip(data.get('text', ''), 1500))}", silent=True)

    return Message(f"{escape(event_type)}: {title}")


def render_baseline(data: dict, tz: ZoneInfo, now: datetime) -> Message:
    counts = data.get("counts", {})
    parts = [f"{n} {KIND_LABEL.get(k, k)}" for k, n in sorted(counts.items())]
    lines = ["<b>İzleme başladı.</b>", "Şu an sitede: " + (", ".join(parts) or "kayıt yok") + "."]
    lines.append("Bundan sonra gelen her yeni şeyi buraya yazacağım.")
    open_items = sorted(data.get("open_assignments", []), key=lambda d: d.get("due_at") or "9999")
    if open_items:
        lines.append("\n<b>Açık ödevlerin:</b>")
        for d in open_items[:10]:
            due = parse_dt(d.get("due_at"))
            lines.append(f"• {escape(d['title'])} · {fmt_dt(due, tz, now)}" + (f" · {left(due, now)}" if due else ""))
    if data.get("failed_scopes"):
        lines.append("\nBazı bölümler okunamadı, bir sonraki turda tekrar denenecek.")
    return Message("\n".join(lines))


def render_group(event_type: str, kind: str, payloads: list[dict], tz: ZoneInfo, now: datetime) -> Message:
    """Aynı türden çok sayıda olay tek mesajda (ör. dönem başında 12 yeni materyal)."""
    label = KIND_LABEL.get(kind, kind)
    head = {"new": f"<b>{len(payloads)} yeni {label}</b>", "changed": f"<b>{len(payloads)} {label} güncellendi</b>",
            "due_changed": f"<b>{len(payloads)} {label} tarihi değişti</b>"}.get(event_type, f"{len(payloads)} olay")
    lines = [head]
    for d in payloads[:25]:
        due = parse_dt(d.get("due_at"))
        suffix = f" · {fmt_dt(due, tz, now)}" if due else ""
        course = f" <i>({escape(d['course'])})</i>" if d.get("course") else ""
        lines.append(f"• {escape(d.get('title', ''))}{course}{suffix}")
    if len(payloads) > 25:
        lines.append(f"… ve {len(payloads) - 25} tane daha")
    return Message("\n".join(lines))


# ── Komut görünümleri ─────────────────────────────────────────────────────────

def assignments_view(rows: list[dict], tz: ZoneInfo, now: datetime, title: str = "<b>Ödevler</b>") -> Message:
    open_rows = [r for r in rows if not r["submitted"] and not r["done_manual"] and (r["due"] is None or r["due"] > now)]
    done_rows = [r for r in rows if r not in open_rows]
    open_rows.sort(key=lambda r: (r["due"] is None, r["due"] or now))
    lines = [title]
    if not open_rows:
        lines.append("\nAçık ödevin yok.")
    else:
        lines.append(f"\n<b>Açık ({len(open_rows)})</b>")
        for r in open_rows:
            lines.append(
                f"• <b>{escape(r['title'])}</b> · {escape(r['course'])}\n"
                f"   {fmt_dt(r['due'], tz, now)}" + (f" · {left(r['due'], now)}" if r["due"] else "")
            )
    recent_done = [r for r in done_rows if r["due"] is None or r["due"] > now - timedelta(days=14)]
    if recent_done:
        lines.append(f"\n<b>Teslim edilen / süresi geçen ({len(recent_done)})</b>")
        for r in sorted(recent_done, key=lambda r: r["due"] or now, reverse=True)[:8]:
            state = "" if r["submitted"] or r["done_manual"] else " · <b>teslim edilmedi</b>"
            grade = f" · not: {escape(r['grade'])}" if r.get("grade") else ""
            lines.append(f"• {escape(r['title'])} · {escape(r['course'])}{grade}{state}")
    buttons = [[Button(clip(r["title"], 32), data=f"det:{r['uid']}")] for r in open_rows[:6]]
    return Message("\n".join(lines), buttons)


AGENDA_LABEL = {"assignment": "Teslim", "live": "Canlı ders", "event": "Etkinlik"}


def agenda_view(rows: list[dict], tz: ZoneInfo, now: datetime, days: int, title: str) -> Message:
    """rows: {'kind','title','course','due','submitted','done_manual'} — tarih sırasına göre ajanda."""
    end = now + timedelta(days=days)
    upcoming = sorted((r for r in rows if r["due"] and now - timedelta(hours=1) <= r["due"] <= end), key=lambda r: r["due"])
    lines = [title]
    if not upcoming:
        lines.append("\nBu aralıkta bir şey yok.")
    current_day = None
    for r in upcoming:
        local = r["due"].astimezone(tz)
        if local.date() != current_day:
            current_day = local.date()
            lines.append(f"\n<b>{local.day} {AYLAR_UZUN[local.month - 1]} {GUNLER_UZUN[local.weekday()]}</b>")
        label = AGENDA_LABEL.get(r["kind"], "")
        state = " · teslim edildi" if r.get("submitted") or r.get("done_manual") else ""
        lines.append(f"{local.strftime('%H:%M')}  {label}: {escape(r['title'])} <i>({escape(r['course'])})</i>{state}")
    return Message("\n".join(lines))


def help_text(llm_off_reason: str | None = None) -> str:
    """llm_off_reason: LLM çalışmıyorsa nedeni ("ayarlardan kapalı", "çalışmıyor: .env'de LLM_API_KEY yok")."""
    lines = [
        "<b>e-Kampüs asistanın</b>",
        "Yeni ödev, duyuru, not, materyal ve canlı dersleri buraya yazarım; teslimlerden önce hatırlatırım.",
        "",
        "/bugun — bugün ve yarın",
        "/hafta — önümüzdeki 7 gün",
        "/odevler — açık ödevler",
        "/notlar — notların",
        "/duyurular — son duyurular",
        "/dosyalar — ders materyalleri: ders seç, dosyayı al",
        "/dersler — derslerin ve ilerleme",
        "/takvim — 30 günlük takvim",
        "/yenile — siteyi şimdi kontrol et",
        "/bildirimler — uyarı yöneticisi (aç/kapa, sessiz, geçmiş)",
        "/ayarlar — LLM, JEV ve Pushover'ı aç/kapa",
        "/hatirlatmalar — kurduğun hatırlatmalar",
        "/durum — sistem durumu",
        "/sessiz 2s — 2 saat acil olmayanları beklet (/sessiz kapat)",
        "",
    ]
    if llm_off_reason is None:
        lines.append("Bana normal cümleyle de yazabilirsin: <i>“bu hafta neye odaklanayım?”</i>, "
                     "<i>“cuma 18'e kadar rahatsız etme”</i>")
        lines.append("/llmlog — LLM son cevapta hangi veriye baktı (/llmlog liste, /llmlog 3)")
        lines.append("/hafiza — LLM'in senin hakkında hatırladıkları")
        lines.append("/unut — LLM sohbet geçmişini sil (hafıza kalır)")
    else:
        hint = " → /ayarlar" if "ayarlardan" in llm_off_reason else ""
        lines.append(f"LLM {escape(llm_off_reason, quote=False)}{hint}. Şimdilik “ödev”, “bugün”, “not” gibi kelimeler yeter.")
    lines.append("\nMateryaldeki “Gönder” butonu dosyayı sitede açar, yani içerik “görüldü” sayılır.")
    return "\n".join(lines)


def features_lines(states: list) -> list[str]:
    """Her özelliğin tek satırlık durumu: açık / ayarlardan kapalı / çalışmıyor: .env'de X yok."""
    return [f"{escape(st.label, quote=False)}: {escape(st.describe(), quote=False)}" for st in states]


def settings_view(states: list, memory_count: int | None = None, reminder_count: int | None = None) -> Message:
    lines = ["<b>Ayarlar</b>", "", "<b>Özellikler</b>", *features_lines(states)]
    if any(st.missing for st in states):
        lines.append("\nAnahtarı olmayan özellik açılamaz; .env'e ekleyip botu yeniden başlat.")
    lines.append("\nAçıp kapatmak için butona dokun.")
    buttons = [[_btn(f"{st.label}: {'anahtar yok' if st.missing else on_off(st.enabled)}", f"feat:{st.key}")]
               for st in states]
    extra = []
    if memory_count is not None:
        extra.append(_btn(f"Hafıza ({memory_count})", "mem:list"))
    if reminder_count is not None:
        extra.append(_btn(f"Hatırlatmalarım ({reminder_count})", "rem:list"))
    if extra:
        buttons.append(extra)
    buttons.append([_btn("Bildirim ayarları", "am:show"), _btn("Yenile", "set:show")])
    return Message("\n".join(lines), buttons)


def digest_view(rows: list[dict], fresh: dict[str, list], tz: ZoneInfo, now: datetime,
                last_ok: datetime | None) -> Message:
    """Sabah özeti: bugün, yaklaşanlar, açık ödevler, son 24 saatte gelenler ve sistemin nabzı."""
    local = now.astimezone(tz)
    lines = [f"<b>Günaydın.</b> {local.day} {AYLAR_UZUN[local.month - 1]} {GUNLER_UZUN[local.weekday()]}"]

    def pending(r: dict) -> bool:
        return r["kind"] != "assignment" or not (r.get("submitted") or r.get("done_manual"))

    today = [r for r in rows if r["due"] and r["due"].astimezone(tz).date() == local.date() and r["due"] >= now and pending(r)]
    week = [r for r in rows if r["due"] and now < r["due"] <= now + timedelta(days=7) and r not in today and pending(r)]
    if today:
        lines.append("\n<b>Bugün</b>")
        lines += [f"• {r['due'].astimezone(tz):%H:%M} {escape(r['title'])} <i>({escape(r['course'])})</i>" for r in today]
    if week:
        lines.append("\n<b>Bu hafta</b>")
        lines += [f"• {fmt_dt(r['due'], tz, now)} · {escape(r['title'])} <i>({escape(r['course'])})</i>" for r in week[:10]]
    if not today and not week:
        lines.append("\nÖnümüzdeki 7 günde teslim ya da etkinlik yok.")
    open_count = sum(1 for r in rows if r["kind"] == "assignment" and pending(r) and (r["due"] is None or r["due"] > now))
    if open_count:
        lines.append(f"\nAçık ödev: <b>{open_count}</b> → /odevler")
    news = [f"{len(v)} {KIND_LABEL[k]}" for k, v in fresh.items() if v]
    if news:
        lines.append("Son 24 saatte gelen: " + ", ".join(news))
    if last_ok:
        lines.append(f"Son başarılı kontrol: {fmt_dt(last_ok, tz, now)}")
    else:
        lines.append("<b>Henüz başarılı kontrol yok</b>, /durum'a bak.")
    return Message("\n".join(lines))


# ── Uyarı yöneticisi ──────────────────────────────────────────────────────────

HISTORY_LABEL = {"due_changed": "Tarih değişti", "changed": "Güncellendi", "reminder": "Hatırlatma",
                 "live_soon": "Canlı ders", "scope_added": "İzlemeye alındı", "alert": "Sistem",
                 "note": "Hatırlatman", "explain": "Açıklama"}


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "hiç"
    minutes = int((now - dt).total_seconds() // 60)
    if minutes < 1:
        return "az önce"
    if minutes < 60:
        return f"{minutes} dk önce"
    if minutes < 60 * 24:
        return f"{minutes // 60} sa önce"
    return f"{minutes // (60 * 24)} gün önce"


def on_off(value) -> str:
    return "açık" if value else "kapalı"


def alert_manager_view(status: dict, prefs: dict, categories: list[tuple[str, str]], tz: ZoneInfo,
                       now: datetime, night: str) -> Message:
    lines = ["<b>Uyarı yöneticisi</b>", "", "<b>Sistem</b>"]
    last_ok = status.get("last_ok")
    if status.get("fail_streak"):
        lines.append(f"e-Kampüs: <b>{status['fail_streak']} kontroldür girilemiyor</b>")
        if status.get("fail_reason"):
            lines.append(f"   Neden: {escape(status['fail_reason'])}")
        lines.append(f"   Son başarılı: {ago(last_ok, now)}")
    elif last_ok:
        lines.append(f"e-Kampüs: erişim normal, son kontrol {ago(last_ok, now)}")
    else:
        lines.append("e-Kampüs: henüz başarılı kontrol yok")
    if status.get("guard_blocked"):
        lines.append(f"Giriş: <b>KİLİTLİ</b> ({escape(status.get('guard_reason') or '')}) → /girisdene")
    else:
        lines.append("Giriş: sorun yok")
    if status.get("parse_problems"):
        lines.append("Okunamayan bölüm: " + escape(", ".join(status["parse_problems"])))
    lines.append(f"Bildirim: son 24 saatte {status.get('sent_24h', 0)}, bekleyen {status.get('pending', 0)}")
    if status.get("alert_channel"):
        lines.append(f"Sistem uyarıları: {escape(status['alert_channel'])}")
    muted = status.get("muted_until")
    if muted and muted > now:
        level = ("tam sessiz, sadece kritik giriş uyarıları gelir" if status.get("mute_full")
                 else "acil olanlar yine gelir")
        lines.append(f"Sessiz: {fmt_dt(muted, tz, now)} kadar ({level})")
    lines.append(f"Gece modu ({night}): {on_off(prefs.get('night'))}")
    lines.append("\nAyarı değiştirmek için butona dokun.")

    toggles = [_btn(f"{label}: {on_off(prefs.get(key))}", f"pref:{key}") for key, label in categories]
    buttons = [toggles[i:i + 2] for i in range(0, len(toggles), 2)]
    buttons.append([_btn(f"Erişim uyarısı: {prefs.get('fail_after')} hatada", "pref:fail_after"),
                    _btn(f"Gece modu: {on_off(prefs.get('night'))}", "pref:night")])
    if muted and muted > now:
        buttons.append([_btn("Sessizi kapat", "mute:off")])
    else:
        buttons.append([_btn("Sessiz 1 sa", "mute:1"), _btn("Sessiz 4 sa", "mute:4"),
                        _btn("Sabaha kadar sessiz", "mute:morning")])
    buttons.append([_btn("Son bildirimler", "am:hist"), _btn("Yenile", "am:show")])
    return Message("\n".join(lines), buttons)


def _btn(text: str, data: str) -> Button:
    return Button(text, data=data)


def memory_view(rows: list[dict], confirm_clear: bool = False) -> Message:
    """LLM'in kalıcı hafızası: ne biliyor, tek tek ya da hepsini sil."""
    lines = ["<b>Hafıza</b>", "LLM'in senin hakkında aklında tuttukları; her cevapta bunlara bakar."]
    if not rows:
        lines.append("\nHenüz bir şey yok. Kalıcı bir tercih söylediğinde (<i>“Ağlar'ı bıraktım”</i>) kendisi kaydeder.")
    lines += [f"\n#{r['id']} {escape(r['text'])}" for r in rows]
    if confirm_clear:
        lines.append("\n<b>Hafızadaki her şey silinsin mi?</b>")
        return Message("\n".join(lines), [[_btn("Evet, hepsini sil", "mem:clear!"), _btn("Vazgeç", "mem:list")]])
    deletes = [_btn(f"Sil #{r['id']}", f"mem:del:{r['id']}") for r in rows]
    buttons = [deletes[i:i + 4] for i in range(0, len(deletes), 4)]
    bottom = [_btn("Hepsini sil", "mem:clear")] if rows else []
    buttons.append(bottom + [_btn("Ayarlar", "set:show")])
    return Message("\n".join(lines), buttons)


def notes_view(notes: list[dict], tz: ZoneInfo, now: datetime) -> Message:
    """Kurulmuş kişisel hatırlatmalar; her birinin yanında iptal butonu."""
    lines = ["<b>Hatırlatmaların</b>"]
    if not notes:
        lines.append("\nKurulu hatırlatma yok. Bana yazman yeter: <i>“yarın 10'da raporu hatırlat”</i>")
    buttons = []
    for note in notes[:20]:
        at = parse_dt(note.get("at"))
        lines.append(f"\n#{note['id']} {fmt_dt(at, tz, now)} · {remaining(at, now)}\n{escape(clip(note.get('text', ''), 200))}")
        buttons.append([_btn(f"İptal #{note['id']}: {clip(note.get('text', ''), 30)}", f"rem:del:{note['id']}")])
    buttons.append([_btn("Ayarlar", "set:show")])
    return Message("\n".join(lines), buttons)


def history_entry(row) -> tuple[str, str]:
    """Gönderilmiş bir bildirimin türü ve başlığı (düz metin)."""
    payload = json.loads(row["payload"])
    title = payload.get("title") or re.sub(r"<[^>]+>", "", payload.get("text", "")).split("\n")[0]
    if row["type"] == "baseline":
        return "İzleme başladı", ""
    if row["type"] == "new":
        return f"Yeni {KIND_LABEL.get(payload.get('kind', ''), 'kayıt')}", title
    return HISTORY_LABEL.get(row["type"], row["type"]), title


def history_view(rows: list, tz: ZoneInfo, now: datetime) -> Message:
    lines = ["<b>Son bildirimler</b>"]
    if not rows:
        lines.append("\nHenüz bildirim gönderilmedi.")
    for row in rows:
        label, title = history_entry(row)
        sent = datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None
        stamp = sent.astimezone(tz).strftime("%d.%m %H:%M") if sent else "?"
        lines.append(f"{stamp}  {label}" + (f": {escape(clip(title, 60))}" if title else ""))
    return Message("\n".join(lines), [[Button("Geri", data="am:show")]])


# ── LLM kayıtları (/llmlog) ───────────────────────────────────────────────────

def _args_text(args: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in (args or {}).items())


DECISION_LABEL = {
    "requires_submission": "Teslim gerektiriyor", "exam_related": "Sınavla ilgili", "schedule_change": "Tarih değişikliği",
    "action_required": "Eylem gerekiyor", "needs_explanation": "Açıklama gerekli", "push_now": "Hemen uyar",
}


def llm_log_view(entry: dict, index: int, total: int, tz: ZoneInfo, now: datetime) -> Message:
    """index 0 = en yeni. Modelin hangi araçla neye baktığını ve ne cevap verdiğini gösterir."""
    at = parse_dt(entry.get("created_at"))
    lines = [f"<b>LLM kaydı {index + 1}/{total}</b> · {fmt_dt(at, tz, now)} · {escape(entry.get('kind', ''))}",
             f"Model: {escape(entry.get('model', '?'))} · {entry.get('tokens', 0)} token · {entry.get('duration_s', '?')} sn"]
    if entry.get("question"):
        lines.append(f"Soru: <i>{escape(clip(entry['question'], 200))}</i>")
    steps = entry.get("steps", [])
    if entry.get("decisions"):  # JEV kararı: soru başına olasılık ve yapılan eylem
        lines.append("\n<b>Kararlar</b>")
        for qid, p in entry["decisions"].items():
            lines.append(f"• {escape(DECISION_LABEL.get(qid, qid))}: %{round(p * 100)}")
        if entry.get("actions"):
            lines.append("Yapılan: " + escape(", ".join(entry["actions"])))
    elif not steps:
        lines.append("\nAraç çağırmadı; sadece kendisine verilen metinle cevapladı.")
    else:
        lines.append(f"\n<b>Baktığı veriler ({len(steps)} araç çağrısı)</b>")
        for i, step in enumerate(steps[:8], 1):
            count = f"{step['count']} kayıt, " if step.get("count") is not None else ""
            lines.append(f"{i}. {escape(step['tool'])}({escape(_args_text(step.get('args')))}) → {count}{step.get('chars', 0)} karakter")
            lines.append(f"<code>{escape(clip(step.get('preview', ''), 220))}</code>")
        if len(steps) > 8:
            lines.append(f"… ve {len(steps) - 8} çağrı daha (tamamı JSON'da)")
    if entry.get("error"):
        lines.append(f"\n<b>Hata:</b> {escape(entry['error'])}")
    elif entry.get("answer") is not None:
        lines.append(f"\n<b>Cevap</b> ({len(entry['answer'])} karakter)\n{escape(clip(entry['answer'], 400))}")
    nav = []
    if index + 1 < total:
        nav.append(Button("Daha eski", data=f"llm:{index + 1}"))
    if index > 0:
        nav.append(Button("Daha yeni", data=f"llm:{index - 1}"))
    buttons = [nav] if nav else []
    buttons.append([Button("Tam veriyi gönder (JSON)", data=f"llmraw:{entry['id']}"), Button("Liste", data="llm:list")])
    return Message("\n".join(lines), buttons)


def llm_log_list_view(entries: list[dict], tz: ZoneInfo, now: datetime) -> Message:
    lines = ["<b>Son LLM kayıtları</b>"]
    if not entries:
        lines.append("\nHenüz kayıt yok. Bota bir soru sorunca burada görünür.")
    buttons = []
    for i, e in enumerate(entries):
        at = parse_dt(e.get("created_at"))
        tools = "JEV" if e.get("decisions") else (", ".join(dict.fromkeys(s["tool"] for s in e.get("steps", []))) or "araç yok")
        state = " · hata" if e.get("error") else ""
        lines.append(f"{i + 1}. {fmt_dt(at, tz, now)} · {escape(e.get('kind', ''))} · {e.get('tokens', 0)} token{state}\n"
                     f"   {escape(clip(e.get('question', ''), 60))} <i>[{escape(tools)}]</i>")
        buttons.append(Button(str(i + 1), data=f"llm:{i}"))
    return Message("\n".join(lines), [buttons[j:j + 5] for j in range(0, len(buttons), 5)])


# ── Ders materyalleri menüsü (/dosyalar) ──────────────────────────────────────

FORMAT_LABEL = {
    "pdf": "PDF", "archive": "ZIP", "zip": "ZIP", "word": "Word", "powerpoint": "PowerPoint", "excel": "Excel",
    "video": "Video", "play": "Video", "youtube": "Video", "link": "Bağlantı", "globe": "Bağlantı",
    "image": "Görsel", "audio": "Ses", "code": "Kod", "alt": "Belge", "lines": "Belge",
}
MATERIALS_PAGE = 8


def format_label(icon: str | None) -> str:
    return FORMAT_LABEL.get((icon or "").lower(), "Dosya")


def _material_order(row: dict) -> tuple:
    order = row["meta"].get("order")
    return (order is None, order if order is not None else 0, row.get("first_seen", ""), row["title"])


def materials_courses_view(rows: list[dict]) -> Message:
    """Materyali olan derslerin listesi; derse dokununca o dersin materyalleri açılır."""
    courses: dict[str, dict] = {}
    for r in rows:
        course_id = r["scope"].split(":", 1)[-1]
        c = courses.setdefault(course_id, {"name": r["course"], "total": 0, "unopened": 0})
        c["total"] += 1
        c["unopened"] += 0 if r["meta"].get("viewed") else 1
    lines = ["<b>Ders materyalleri</b>"]
    if not courses:
        lines.append("\nHenüz materyal yok. Hoca yükleyince burada görünür ve sana haber veririm.")
        return Message("\n".join(lines))
    lines.append("Bir ders seç:\n")
    ordered = sorted(courses.items(), key=lambda kv: kv[1]["name"])
    for _, c in ordered:
        unopened = f", {c['unopened']} açılmamış" if c["unopened"] else ""
        lines.append(f"• {escape(c['name'])}: {c['total']} materyal{unopened}")
    buttons = [[Button(f"{clip(c['name'], 34)} ({c['total']})", data=f"fc:{cid}:0")] for cid, c in ordered]
    return Message("\n".join(lines), buttons)


def materials_list_view(course_id: str, rows: list[dict], page: int) -> Message:
    """Bir dersin materyalleri: bölümlere göre, sitedeki sırayla; her materyal bir buton."""
    rows = sorted(rows, key=_material_order)
    pages = max(1, -(-len(rows) // MATERIALS_PAGE))
    page = min(max(page, 0), pages - 1)
    start = page * MATERIALS_PAGE
    shown = rows[start:start + MATERIALS_PAGE]
    course = rows[0]["course"] if rows else ""
    head = f"<b>{escape(course)}</b> · {len(rows)} materyal" + (f" · sayfa {page + 1}/{pages}" if pages > 1 else "")
    lines = [head]
    section = None
    for n, r in enumerate(shown, start + 1):
        current = r["extra"].get("section") or "Diğer"
        if current != section:
            section = current
            lines.append(f"\n<b>{escape(section)}</b>")
        state = "" if r["meta"].get("viewed") else " · açılmadı"
        lines.append(f"{n}. {escape(r['title'])} · {format_label(r['extra'].get('type'))}{state}")
    lines.append("\nDokunduğun materyali dosya olarak gönderirim. Bu, içeriği sitede “görüldü” yapar.")
    buttons = [[Button(f"{n}. {format_label(r['extra'].get('type'))} · {clip(r['title'], 34)}", data=f"file:{r['uid']}")]
               for n, r in enumerate(shown, start + 1)]
    nav = []
    if page > 0:
        nav.append(Button("Önceki", data=f"fc:{course_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(Button("Sonraki", data=f"fc:{course_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button("Derslere dön", data="fc:list")])
    return Message("\n".join(lines), buttons)

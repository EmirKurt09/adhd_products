"""Telegram botu: komutlar, butonlar, serbest metin (LLM) ve arka plan işleri.

Bot yalnızca TELEGRAM_OWNER_CHAT_ID ile konuşur. Bu değer boşsa "kurulum modunda" açılır:
/start yazan kişiye sadece kendi chat id'sini söyler, başka hiçbir veri göstermez.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from html import escape

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from . import __version__
from . import messages as M
from . import prefs as PR
from .browser import AuthGuard
from .config import Settings
from .engine import Engine, describe_failure, utcnow
from .llm import Assistant, make_assistant
from .lock import InstanceLock
from .store import Store

log = logging.getLogger(__name__)

KEYBOARD = ReplyKeyboardMarkup(
    [["Bugün", "Ödevler"], ["Hafta", "Notlar"], ["Yenile", "Bildirimler", "Durum"]],
    resize_keyboard=True, is_persistent=True,
)
COMMANDS = [
    ("bugun", "Bugün ve yarın"), ("hafta", "Önümüzdeki 7 gün"), ("odevler", "Açık ödevler"),
    ("notlar", "Notlar"), ("duyurular", "Son duyurular"), ("dosyalar", "Son ders materyalleri"),
    ("dersler", "Dersler ve ilerleme"), ("takvim", "30 günlük takvim"), ("yenile", "Siteyi şimdi kontrol et"),
    ("bildirimler", "Uyarı yöneticisi: aç/kapa, sessiz, geçmiş"), ("durum", "Sistem durumu"),
    ("sessiz", "Bildirimleri beklet: /sessiz 2s"), ("unut", "Sohbet geçmişini sil"),
    ("llmlog", "LLM son cevapta neye baktı: /llmlog, /llmlog 3, /llmlog liste"),
    ("yardim", "Yardım"),
]
REFRESH_COOLDOWN = timedelta(seconds=60)
MAX_TEXT = 4000


# ── Ortak yardımcılar ─────────────────────────────────────────────────────────

class Ctx:
    """bot_data içinde taşınan bağımlılıklar."""

    def __init__(self, settings: Settings, store: Store, engine: Engine, assistant: Assistant | None):
        self.s = settings
        self.store = store
        self.engine = engine
        self.assistant = assistant
        self.last_refresh: datetime | None = None


def deps(context: ContextTypes.DEFAULT_TYPE) -> Ctx:
    return context.application.bot_data["ctx"]


def markup(buttons: list[list[M.Button]]) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(b.text, callback_data=b.data, url=b.url) for b in row] for row in buttons if row]
    return InlineKeyboardMarkup(rows) if rows else None


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def send(bot, chat_id: int, message: M.Message, **kwargs):
    text = M.clip(message.text, MAX_TEXT)
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup(message.buttons),
                                      disable_notification=message.silent, **kwargs)
    except BadRequest as e:
        if "parse" not in str(e).lower() and "entities" not in str(e).lower():
            raise
        log.warning("HTML biçimi reddedildi, düz metin gönderiliyor: %s", e)
        return await bot.send_message(chat_id, _plain(text), parse_mode=None, reply_markup=markup(message.buttons),
                                      disable_notification=message.silent, **kwargs)


async def reply(update: Update, message: M.Message) -> None:
    await send(update.get_bot(), update.effective_chat.id, message)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Komutlar ──────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    await update.effective_chat.send_message(M.help_text(c.assistant is not None), reply_markup=KEYBOARD)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    now = now_utc()
    rows = c.store.items(("assignment", "live", "event"), order="due_at")
    view = M.agenda_view(rows, c.s.tz, now, days=2, title="<b>Bugün ve yarın</b>")
    open_week = [r for r in rows if r["kind"] == "assignment" and not (r["submitted"] or r["done_manual"])
                 and r["due"] and now < r["due"] <= now + timedelta(days=7)]
    if open_week:
        view.text += f"\n\nBu hafta teslim edilmemiş {len(open_week)} ödev var → /odevler"
    await reply(update, view)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    rows = c.store.items(("assignment", "live", "event"), order="due_at")
    await reply(update, M.agenda_view(rows, c.s.tz, now_utc(), days=7, title="<b>Önümüzdeki 7 gün</b>"))


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    rows = c.store.items(("assignment", "live", "event"), order="due_at")
    await reply(update, M.agenda_view(rows, c.s.tz, now_utc(), days=30, title="<b>30 günlük takvim</b>"))


async def cmd_assignments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    await reply(update, M.assignments_view(c.store.items(("assignment",), order="due_at"), c.s.tz, now_utc()))


async def cmd_grades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    rows = c.store.items(("grade",))
    if not rows:
        await reply(update, M.Message("Henüz girilmiş not yok."))
        return
    by_course: dict[str, list[dict]] = {}
    for r in rows:
        by_course.setdefault(r["course"], []).append(r)
    lines = ["<b>Notların</b>"]
    for course, items in sorted(by_course.items()):
        lines.append(f"\n<b>{escape(course)}</b>")
        lines += [f"• {escape(r['title'])}: <b>{escape(str(r['extra'].get('value')))}</b>" for r in items]
    await reply(update, M.Message("\n".join(lines)))


async def cmd_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    rows = c.store.items(("announcement",), limit=10)
    if not rows:
        await reply(update, M.Message("Sitede şu an duyuru yok. Yeni bir duyuru gelirse hemen yazarım."))
        return
    lines = ["<b>Son duyurular</b>"]
    for r in rows:
        date = f" · {escape(r['extra'].get('date'))}" if r["extra"].get("date") else ""
        course = f" <i>({escape(r['course'])})</i>" if r["course"] else ""
        lines.append(f"\n<b>{escape(r['title'])}</b>{course}{date}\n{escape(M.clip(r['body'], 300))}")
    await reply(update, M.Message("\n".join(lines)))


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    query = " ".join(context.args or []).casefold()
    rows = [r for r in c.store.items(("file",), limit=300) if not query or query in r["course"].casefold()]
    if not rows:
        await reply(update, M.Message("Materyal bulunamadı."))
        return
    lines = ["<b>Son ders materyalleri</b>" + (f" · {escape(query)}" if query else "")]
    for r in rows[:15]:
        seen = "" if r["meta"].get("viewed") else " · <b>açılmadı</b>"
        lines.append(f"• {escape(r['title'])} <i>({escape(r['course'])})</i>{seen}")
    lines.append("\nDersle süzmek için: /dosyalar ağlar\nAşağıdakilere dokunursan dosyayı gönderirim:")
    buttons = [[M.Button(M.clip(r["title"], 32), data=f"file:{r['uid']}")] for r in rows[:6]]
    await reply(update, M.Message("\n".join(lines), buttons))


async def cmd_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    courses = json.loads(c.store.get("courses", "[]"))
    if not courses:
        await reply(update, M.Message("Henüz ders listesi okunmadı; ilk kontrol bitince gelir."))
        return
    lines = ["<b>Derslerin</b>"]
    for co in courses:
        counts = co.get("counts", {})
        parts = []
        for label, values in counts.items():
            total = values.get("Toplam")
            done = next((v for k, v in values.items() if k != "Toplam"), None)
            parts.append(f"{label} {done}/{total}" if done is not None else f"{label} {total}")
        progress = f" · %{co['progress']}" if co.get("progress") is not None else ""
        lines.append(f"\n<b>{escape(co['code'])}</b> {escape(co['name'])}{progress}"
                     + (f"\n    {escape(', '.join(parts))}" if parts else ""))
    await reply(update, M.Message("\n".join(lines)))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    now = now_utc()
    last = json.loads(c.store.get("last_scan", "{}"))
    lines = [f"<b>Durum</b> · v{__version__}"]
    if last:
        at = M.parse_dt(last.get("at"))
        if last.get("ok"):
            lines.append(f"Son kontrol: {M.fmt_dt(at, c.s.tz, now)}, başarılı ({last.get('duration_s')} sn)")
        else:
            lines.append(f"Son kontrol: {M.fmt_dt(at, c.s.tz, now)}, <b>başarısız</b>\n"
                         f"   {escape(describe_failure(last.get('error', '')))}\n"
                         f"   <code>{escape(last.get('error', '')[:200])}</code>")
        if last.get("failed"):
            lines.append("Okunamayan: " + escape(", ".join(last["failed"])))
    else:
        lines.append("Henüz kontrol yapılmadı.")
    last_ok = M.parse_dt(c.store.get("last_ok_at"))
    if last_ok and not last.get("ok"):
        lines.append(f"Son başarılı: {M.fmt_dt(last_ok, c.s.tz, now)}")
    jobs = context.job_queue.get_jobs_by_name("scan")
    if jobs and jobs[0].next_t:
        lines.append(f"Sonraki kontrol: {M.fmt_dt(jobs[0].next_t, c.s.tz, now)}")
    guard = AuthGuard(c.s.auth_guard_path, c.s.username, c.s.password).status()
    lines.append("Giriş: " + (f"KİLİTLİ ({escape(guard.get('reason') or '')}) → /girisdene" if guard.get("blocked") else "açık"))
    stats = c.store.outbox_stats()
    lines.append(f"Bekleyen bildirim: {stats['pending']}" + (f" (gönderilemeyen {stats['failing']})" if stats["failing"] else ""))
    hold = c.engine.hold_reason(now)
    if hold == "sessiz":
        lines.append(f"Sessiz: {M.fmt_dt(c.engine.muted_until(), c.s.tz, now)} kadar")
    elif hold == "gece":
        lines.append("Gece modu: acil olmayanlar sabah gelir")
    if c.assistant:
        lines.append(f"LLM: {escape(c.s.llm_provider)} · bugün kalan bütçe {max(0, c.assistant.budget_left())} token")
    else:
        lines.append("LLM: kapalı (.env'e LLM_API_KEY ekleyince açılır)")
    await reply(update, M.Message("\n".join(lines)))


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    now = now_utc()
    if c.last_refresh and now - c.last_refresh < REFRESH_COOLDOWN:
        await reply(update, M.Message("Az önce kontrol ettim, bir dakika sonra tekrar dene."))
        return
    c.last_refresh = now
    note = await update.effective_chat.send_message("Siteyi kontrol ediyorum…")
    outcome = await c.engine.run_scan("elle")
    if outcome.ok:
        text = f"Kontrol tamam ({outcome.duration_s:.0f} sn). " + (
            f"{outcome.events} yeni olay, hemen gönderiyorum." if outcome.events else "Yeni bir şey yok.")
    else:
        text = f"Kontrol başarısız:\n<code>{escape(outcome.error[:300])}</code>"
    await note.edit_text(text)
    await flush_job(context)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    arg = " ".join(context.args or []).strip().lower()
    if arg in ("kapat", "kapa", "off", "bitir", "0"):
        c.engine.set_mute(None)
        await reply(update, M.Message("Sessiz mod kapandı; bekleyen bildirimler geliyor."))
        await flush_job(context)
        return
    match = re.fullmatch(r"(\d+)\s*(dk|d|m|sa|s|h|g)?", arg or "2s")
    if not match:
        await reply(update, M.Message("Kullanım: /sessiz 30dk · /sessiz 2s · /sessiz 1g · /sessiz kapat"))
        return
    amount, unit = int(match.group(1)), match.group(2) or "s"
    delta = {"dk": timedelta(minutes=amount), "d": timedelta(minutes=amount), "m": timedelta(minutes=amount),
             "g": timedelta(days=amount)}.get(unit, timedelta(hours=amount))
    until = now_utc() + delta
    c.engine.set_mute(until)
    await reply(update, M.Message(f"{M.fmt_dt(until, c.s.tz)} kadar sadece acil şeyleri (≤3 saat kalan teslim, "
                                  "canlı ders, sistem uyarısı) yazacağım. Gerisi sonra topluca gelir."))


async def cmd_retry_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    AuthGuard(c.s.auth_guard_path, c.s.username, c.s.password).reset()
    c.last_refresh = None
    await reply(update, M.Message("Giriş kilidini kaldırdım, şimdi bir kez deniyorum."))
    await cmd_refresh(update, context)


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps(context).store.chat_clear()
    await reply(update, M.Message("Sohbet geçmişini sildim."))


async def cmd_llmlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    arg = " ".join(context.args or []).strip().lower()
    total = c.store.llm_log_count()
    if not total:
        hint = "" if c.assistant else " LLM şu an kapalı (.env'de LLM_API_KEY yok)."
        await reply(update, M.Message("Henüz LLM kaydı yok. Bota normal cümleyle bir soru sorunca burada görünür." + hint))
        return
    if arg in ("liste", "list", "hepsi"):
        await reply(update, M.llm_log_list_view(c.store.llm_log_recent(10), c.s.tz, now_utc()))
        return
    index = int(arg) - 1 if arg.isdigit() and int(arg) > 0 else 0
    index = min(index, total - 1)
    await reply(update, M.llm_log_view(c.store.llm_log_at(index), index, total, c.s.tz, now_utc()))


async def send_llm_raw(chat_id: int, log_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    entry = deps(context).store.llm_log_by_id(log_id)
    if not entry:
        await context.bot.send_message(chat_id, "Bu kayıt artık yok (son 30 kayıt tutuluyor).")
        return
    body = json.dumps(entry, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    await context.bot.send_document(chat_id, document=body, filename=f"llm-kayit-{log_id}.json",
                                    caption="Modele giden her şey: sistem talimatı, sohbet geçmişi, soru, araç sonuçları ve cevap.")


def manager_message(c: Ctx) -> M.Message:
    now = now_utc()
    last = json.loads(c.store.get("last_scan", "{}"))
    guard = AuthGuard(c.s.auth_guard_path, c.s.username, c.s.password).status()
    streak = int(c.store.get("fail_streak", "0"))
    stats = c.store.outbox_stats()
    status = {
        "last_ok": M.parse_dt(c.store.get("last_ok_at")),
        "fail_streak": streak,
        "fail_reason": describe_failure(last.get("error", "")) if streak and not last.get("ok") else "",
        "guard_blocked": guard.get("blocked"), "guard_reason": guard.get("reason"),
        "parse_problems": sorted(json.loads(c.store.get("parse_streaks", "{}"))),
        "pending": stats["pending"], "sent_24h": c.store.sent_since(now - timedelta(hours=24)),
        "muted_until": c.engine.muted_until(),
    }
    night = f"{c.s.night_start:%H:%M}–{c.s.night_end:%H:%M}"
    return M.alert_manager_view(status, PR.load(c.store), PR.CATEGORIES, c.s.tz, now, night)


async def cmd_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, manager_message(deps(context)))


def _next_morning(c: Ctx, now: datetime) -> datetime:
    local = now.astimezone(c.s.tz)
    target = local.replace(hour=c.s.daily_digest_time.hour, minute=c.s.daily_digest_time.minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


async def _edit(query, message: M.Message) -> None:
    try:
        await query.edit_message_text(M.clip(message.text, MAX_TEXT), reply_markup=markup(message.buttons))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ── Serbest metin ─────────────────────────────────────────────────────────────

BUTTON_ROUTES = {
    "Bugün": cmd_today, "Ödevler": cmd_assignments, "Hafta": cmd_week,
    "Notlar": cmd_grades, "Yenile": cmd_refresh, "Durum": cmd_status, "Bildirimler": cmd_manager,
}
KEYWORD_ROUTES = [
    (("ödev", "odev", "teslim"), cmd_assignments), (("bugün", "bugun", "yarın", "yarin"), cmd_today),
    (("hafta",), cmd_week), (("not",), cmd_grades), (("duyuru",), cmd_announcements),
    (("dosya", "materyal", "pdf", "slayt"), cmd_files), (("ders",), cmd_courses),
    (("takvim", "sınav", "sinav", "vize", "final"), cmd_calendar), (("yenile", "kontrol"), cmd_refresh),
    (("durum",), cmd_status), (("bildirim", "uyarı", "uyari", "alarm"), cmd_manager),
]


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    # Eski (emojili) klavye butonları da eşleşsin diye sembolleri ayıkla
    text = re.sub(r"[^\w\s/.,:;!?'’“”()\-]", "", update.message.text or "").strip()
    if text in BUTTON_ROUTES:
        await BUTTON_ROUTES[text](update, context)
        return
    if c.assistant is None:
        low = text.casefold()
        for words, handler in KEYWORD_ROUTES:
            if any(w in low for w in words):
                await handler(update, context)
                return
        await cmd_help(update, context)
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        answer = await asyncio.wait_for(c.assistant.answer(text), timeout=90)
    except Exception as e:  # noqa: BLE001 - LLM hatası kullanıcıya düzgün söylenir, bot çalışmaya devam eder
        log.warning("LLM yanıtı alınamadı: %s", e)
        await reply(update, M.Message("Şu an LLM'e ulaşamadım. Komutlar çalışıyor: /odevler, /bugun, /notlar"))
        return
    await reply(update, M.Message(escape(answer or "…")))


# ── Butonlar ──────────────────────────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    query = update.callback_query
    if not is_owner(update, c.s.telegram_owner_chat_id):
        await query.answer()
        return
    action, _, arg = (query.data or "").partition(":")
    if action == "done":
        c.store.set_done("assignment", arg, True)
        await query.answer("Tamam, bu ödev için hatırlatma yok.")
        await _swap_done_button(query, arg, done=True)
    elif action == "undo":
        c.store.set_done("assignment", arg, False)
        await query.answer("Geri aldım, hatırlatmalar açık.")
        await _swap_done_button(query, arg, done=False)
    elif action == "det":
        await query.answer("Ayrıntıları getiriyorum…")
        await show_assignment(update.effective_chat.id, arg, context)
    elif action == "file":
        await query.answer("Dosyayı indiriyorum…")
        await send_material(update.effective_chat.id, arg, context)
    elif action == "pref":
        prefs = PR.toggle(c.store, arg)
        label = dict(PR.CATEGORIES).get(arg) or {"fail_after": "Erişim uyarısı", "night": "Gece modu"}.get(arg, arg)
        state = f"{prefs[arg]} hatada" if arg == "fail_after" else ("açık" if prefs.get(arg) else "kapalı")
        await query.answer(f"{label}: {state}")
        await _edit(query, manager_message(c))
    elif action == "mute":
        now = now_utc()
        until = None if arg == "off" else _next_morning(c, now) if arg == "morning" else now + timedelta(hours=int(arg))
        c.engine.set_mute(until)
        await query.answer("Sessiz mod kapandı" if until is None else f"{M.fmt_dt(until, c.s.tz, now)} kadar sessiz")
        await _edit(query, manager_message(c))
        if until is None:
            await flush_job(context)
    elif action == "am":
        await query.answer()
        if arg == "hist":
            await _edit(query, M.history_view(c.store.recent_sent(15), c.s.tz, now_utc()))
        else:
            await _edit(query, manager_message(c))
    elif action == "llm":
        await query.answer()
        if arg == "list":
            await _edit(query, M.llm_log_list_view(c.store.llm_log_recent(10), c.s.tz, now_utc()))
        else:
            total = c.store.llm_log_count()
            index = min(int(arg or 0), max(total - 1, 0))
            entry = c.store.llm_log_at(index)
            if entry:
                await _edit(query, M.llm_log_view(entry, index, total, c.s.tz, now_utc()))
    elif action == "llmraw":
        await query.answer("JSON hazırlanıyor…")
        await send_llm_raw(update.effective_chat.id, int(arg), context)
    elif action == "att":
        await query.answer("Eki indiriyorum…")
        uid, _, index = arg.partition(":")
        await send_attachment(update.effective_chat.id, uid, int(index or 0), context)
    else:
        await query.answer()


async def _swap_done_button(query, uid: str, done: bool) -> None:
    markup_ = query.message.reply_markup
    if not markup_:
        return
    rows = []
    for row in markup_.inline_keyboard:
        new_row = []
        for b in row:
            if b.callback_data in (f"done:{uid}", f"undo:{uid}"):
                b = InlineKeyboardButton("Geri al" if done else "Teslim ettim",
                                         callback_data=f"{'undo' if done else 'done'}:{uid}")
            new_row.append(b)
        rows.append(new_row)
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(rows))
    except BadRequest:
        pass


async def show_assignment(chat_id: int, uid: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    item = c.store.item("assignment", uid)
    if not item:
        await context.bot.send_message(chat_id, "Bu ödevi bulamadım.")
        return
    now = now_utc()
    detail = None
    try:
        detail = await c.engine.assignment_detail(uid)
        c.store.set(f"att:{uid}", json.dumps(detail.attachments, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - site erişilemese de kayıttaki bilgi gösterilir
        log.warning("Ödev ayrıntısı alınamadı: %s", e)
    status = "teslim edildi" if item["submitted"] else ("“teslim ettim” işaretli" if item["done_manual"] else "<b>teslim edilmedi</b>")
    lines = [f"<b>{escape(item['title'])}</b>", f"<i>{escape(item['course'])}</i>",
             f"Teslim: <b>{M.fmt_dt(item['due'], c.s.tz, now)}</b>" + (f" · {M.left(item['due'], now)}" if item["due"] else ""),
             f"Durum: {status}" + (f" · not: <b>{escape(item['grade'])}</b>" if item.get("grade") else "")]
    if detail and detail.limit:
        lines.append(f"Dosya sınırı: {escape(detail.limit)}")
    body = (detail.description if detail and detail.description else item["body"]) or "Açıklama yok."
    lines.append(f"\n{escape(M.clip(body, 2500))}")
    attachments = (detail.attachments if detail else [])[:5]
    if attachments:
        lines.append("\nEkler aşağıda; dokunursan gönderirim.")
    buttons = [[M.Button(f"Ek: {M.clip(name, 30)}", data=f"att:{uid}:{i}")] for i, (name, _) in enumerate(attachments)]
    buttons += M.assignment_buttons({"uid": uid, "url": item["url"]})[1:]
    buttons.insert(0, [M.Button("Geri al" if item["done_manual"] else "Teslim ettim",
                                data=f"{'undo' if item['done_manual'] else 'done'}:{uid}")])
    await send(context.bot, chat_id, M.Message("\n".join(lines), buttons))


async def send_material(chat_id: int, uid: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    item = c.store.item("file", uid)
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        name, body, url = await c.engine.fetch_material(uid)
    except Exception as e:  # noqa: BLE001
        await context.bot.send_message(chat_id, f"Dosyayı alamadım: {escape(str(e)[:200])}")
        return
    await _send_file(context, chat_id, name, body, url, item["title"] if item else name, item["course"] if item else "")


async def send_attachment(chat_id: int, uid: str, index: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    attachments = json.loads(c.store.get(f"att:{uid}", "[]"))
    if index >= len(attachments):
        await context.bot.send_message(chat_id, "Ek bulunamadı; ödev ayrıntısını tekrar aç.")
        return
    label, url = attachments[index]
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        name, body, url = await c.engine.download(url)
    except Exception as e:  # noqa: BLE001
        await context.bot.send_message(chat_id, f"Eki indiremedim: {escape(str(e)[:200])}\n{escape(url)}")
        return
    await _send_file(context, chat_id, label or name, body, url, label, "")


async def _send_file(context, chat_id: int, name: str, body: bytes | None, url: str, title: str, course: str) -> None:
    caption = f"{escape(title)}" + (f"\n{escape(course)}" if course else "")
    if body is None:
        await context.bot.send_message(chat_id, f"{caption}\nDosya Telegram sınırından (50 MB) büyük, link: {escape(url)}")
        return
    await context.bot.send_document(chat_id, document=body, filename=name, caption=caption)


# ── Arka plan işleri ──────────────────────────────────────────────────────────

async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    try:
        await c.engine.run_scan()
    finally:
        delay = c.engine.next_scan_delay(now_utc())
        context.job_queue.run_once(scan_job, delay, name="scan")
    await flush_job(context)


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)

    async def deliver(message: M.Message, ctx: dict) -> None:
        sent = await send(context.bot, c.s.telegram_owner_chat_id, message)
        if c.assistant and ctx.get("type") == "new" and ctx.get("kind") == "assignment":
            context.application.create_task(_tldr(context, c, ctx, sent.message_id))

    await c.engine.flush(deliver)
    c.engine.touch()


async def _tldr(context, c: Ctx, data: dict, reply_to: int) -> None:
    try:
        summary = await asyncio.wait_for(c.assistant.tldr(data), timeout=60)
    except Exception as e:  # noqa: BLE001 - özet "en iyi çaba"dır
        log.info("TL;DR üretilemedi: %s", e)
        return
    if summary:
        await context.bot.send_message(c.s.telegram_owner_chat_id, f"<b>Kısaca</b>\n{escape(summary)}",
                                       reply_to_message_id=reply_to, disable_notification=True)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if deps(context).engine.plan_reminders(now_utc()):
        await flush_job(context)


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    c = deps(context)
    if not PR.load(c.store)["digest"]:
        return
    now = now_utc()
    rows = c.store.items(("assignment", "live", "event"), order="due_at")
    since = now - timedelta(hours=24)
    fresh = {k: [r for r in c.store.items((k,), limit=50) if M.parse_dt(r["first_seen"]) >= since]
             for k in ("announcement", "file", "grade")}
    message = M.digest_view(rows, fresh, c.s.tz, now, last_ok=M.parse_dt(c.store.get("last_ok_at")))
    if c.assistant:
        try:
            agenda = json.dumps(c.assistant.run_tool("agenda", {"days": 7}), ensure_ascii=False)
            plan = await asyncio.wait_for(c.assistant.plan_day(agenda), timeout=60)
            if plan:
                message.text += f"\n\n<b>Bugünün planı</b>\n{escape(plan)}"
        except Exception as e:  # noqa: BLE001 - özet planı "en iyi çaba"dır
            log.info("Günlük plan üretilemedi: %s", e)
    await send(context.bot, c.s.telegram_owner_chat_id, message)


# ── Kurulum ve çalıştırma ─────────────────────────────────────────────────────

def owner_filter(owner_id: int) -> filters.BaseFilter:
    """Sadece sahiple özel sohbet: sohbet türü, sohbet kimliği ve gönderen kişi üçü birden eşleşmeli."""
    return filters.ChatType.PRIVATE & filters.Chat(chat_id=owner_id) & filters.User(user_id=owner_id)


def is_owner(update: Update, owner_id: int | None) -> bool:
    chat, user = update.effective_chat, update.effective_user
    return (owner_id is not None and chat is not None and user is not None
            and chat.type == ChatType.PRIVATE and chat.id == owner_id and user.id == owner_id)


def _who(user, chat) -> str:
    if user is None:
        return escape(f"{getattr(chat, 'title', '') or ''} ({chat.id})")
    handle = f" @{user.username}" if user.username else ""
    return escape(f"{user.full_name}{handle} (id {user.id})")


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Başkasına asla cevap verme; sahibine günde bir kez haber ver."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None:
        return
    who_id = user.id if user else chat.id
    log.warning("Yetkisiz erişim denemesi yok sayıldı: chat=%s user=%s", chat.id, who_id)
    c: Ctx | None = context.application.bot_data.get("ctx")
    if c is None:
        return
    now = now_utc()
    said = (update.effective_message.text or update.effective_message.caption or "") if update.effective_message else ""
    text = (f"<b>Bota yetkisiz erişim denemesi</b>\nKim: {_who(user, chat)}\n"
            + (f"Yazdığı: <i>{escape(M.clip(said, 120))}</i>\n" if said else "")
            + "Hiçbir cevap verilmedi. Aynı kişi bugün tekrar yazarsa bildirmeyeceğim.")
    c.engine.alert(f"stranger:{who_id}:{now.astimezone(c.s.tz).date().isoformat()}", text, now, urgent=False)
    await flush_job(context)


async def on_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot bir gruba ya da kanala eklenirse hemen çık; bot sadece sahibinin özel sohbetinde yaşar."""
    member = update.my_chat_member
    chat = update.effective_chat
    if member is None or chat is None or chat.type == ChatType.PRIVATE:
        return
    if member.new_chat_member.status in ("member", "administrator", "restricted"):
        try:
            await context.bot.leave_chat(chat.id)
        except TelegramError as e:
            log.warning("Gruptan çıkılamadı (%s): %s", chat.id, e)
        log.warning("Bot bir gruba eklendi, çıkıldı: %s", chat.id)
        c: Ctx | None = context.application.bot_data.get("ctx")
        if c is not None:
            now = now_utc()
            c.engine.alert(f"group:{chat.id}", f"<b>Bot bir gruba eklendi ve hemen çıktı.</b>\nGrup: "
                           f"{escape(chat.title or str(chat.id))}\nEkleyen: {_who(member.from_user, chat)}", now)
            await flush_job(context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Bot hatası: %s", context.error, exc_info=context.error)


async def setup_mode_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    log.warning("KURULUM: chat id %s (%s) /start yazdı", chat.id, chat.full_name)
    await chat.send_message(
        f"Merhaba! Chat ID'n: <code>{chat.id}</code>\n"
        "Bunu .env içinde <b>TELEGRAM_OWNER_CHAT_ID</b> olarak yaz ve botu yeniden başlat."
    )


def build_app(settings: Settings) -> Application:
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True),
                        tzinfo=settings.tz)
    app = ApplicationBuilder().token(settings.telegram_token).defaults(defaults).build()
    app.add_error_handler(on_error)

    if not settings.telegram_owner_chat_id:
        app.add_handler(CommandHandler("start", setup_mode_start))
        return app

    store = Store(settings.db_path)
    engine = Engine(settings, store)
    app.bot_data["ctx"] = Ctx(settings, store, engine, make_assistant(settings, store))
    owner = owner_filter(settings.telegram_owner_chat_id)

    for names, handler in [
        (("start", "yardim", "help", "menu"), cmd_help), (("bugun",), cmd_today), (("hafta",), cmd_week),
        (("takvim",), cmd_calendar), (("odevler", "odev"), cmd_assignments), (("notlar",), cmd_grades),
        (("duyurular",), cmd_announcements), (("dosyalar",), cmd_files), (("dersler",), cmd_courses),
        (("durum",), cmd_status), (("yenile",), cmd_refresh), (("sessiz",), cmd_mute),
        (("bildirimler", "uyarilar", "alarm"), cmd_manager),
        (("girisdene",), cmd_retry_login), (("unut",), cmd_forget), (("llmlog",), cmd_llmlog),
    ]:
        app.add_handler(CommandHandler(list(names), handler, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~owner, on_stranger))
    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))

    jq = app.job_queue
    jq.run_once(scan_job, 5, name="scan")
    jq.run_repeating(flush_job, interval=15, first=20, name="flush")
    jq.run_repeating(reminder_job, interval=60, first=30, name="reminders")
    jq.run_daily(digest_job, time=settings.daily_digest_time.replace(tzinfo=settings.tz), name="digest")
    return app


async def _post_init(app: Application) -> None:
    # Komut menüsü sadece sahibin sohbetinde görünsün; başkaları botu açınca boş bir bot görür
    await app.bot.delete_my_commands()
    ctx: Ctx | None = app.bot_data.get("ctx")
    if ctx is None:
        log.warning("TELEGRAM_OWNER_CHAT_ID boş: bot kurulum modunda. Bota /start yaz.")
        return
    await app.bot.set_my_commands([BotCommand(n, d) for n, d in COMMANDS],
                                  scope=BotCommandScopeChat(ctx.s.telegram_owner_chat_id))
    last = M.parse_dt(ctx.store.get("start_notice_at"))
    if last is None or utcnow() - last > timedelta(hours=12):
        try:
            await app.bot.send_message(ctx.s.telegram_owner_chat_id, "e-Kampüs asistanı çalışıyor. /yardim",
                                       reply_markup=KEYBOARD, disable_notification=True)
            ctx.store.set("start_notice_at", utcnow().isoformat())
        except TelegramError as e:
            log.warning("Başlangıç mesajı gönderilemedi: %s", e)


def run(settings: Settings) -> int:
    settings.require("ekampus")
    if not settings.telegram_token:
        settings.require("telegram")
    lock = InstanceLock(settings.data_dir / "bot.lock")
    if not lock.acquire():
        log.error("Bot zaten çalışıyor (%s kilitli).", settings.data_dir / "bot.lock")
        return 1
    try:
        app = build_app(settings)
        app.post_init = _post_init
        log.info("Bot başlıyor (v%s, veri: %s)", __version__, settings.data_dir)
        app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        lock.release()
    return 0

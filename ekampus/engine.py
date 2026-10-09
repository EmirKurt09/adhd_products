"""Botun motoru: tarama turu, sağlık takibi, hatırlatma planı ve outbox gönderim politikası.

Telegram'dan bağımsızdır: mesajı gönderen fonksiyon dışarıdan verilir. Böylece bütün politika
(gruplama, sessiz saatler, acil olanlar, sağlık uyarıları) test edilebilir.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from html import escape as _esc
from pathlib import PurePosixPath
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit

from playwright.async_api import Error as PlaywrightError

from . import parse as P
from . import prefs as PR
from .browser import AuthBlocked, AuthError, AuthGuard, Campus, CaptchaRequired, LoginCooldown, LoginRejected
from .config import Settings
from .detect import diff
from .messages import Message, fmt_dt, render_event, render_group
from .models import Event
from .reminders import plan_reminders
from .scan import FetchError, Scanner
from .store import Store

log = logging.getLogger(__name__)

PARSE_ALERT_AFTER = {"announce": 1}  # kapsam başına; varsayılan 2
ANOMALY_ALERT_AFTER = 2
GROUP_THRESHOLD = 4             # aynı türden bu kadar ve fazlası tek mesajda toplanır
GROUPABLE = {"new", "changed", "due_changed"}
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024

Sender = Callable[[Message, dict], Awaitable[None]]
AlertChannel = Callable[[str, int], Awaitable[None]]  # (metin, öncelik) → sistem uyarısı kanalı (Pushover)
FindingsHook = Callable[[list[Event]], Awaitable[None]]
FINDING_TYPES = {"new", "due_changed", "changed", "scope_added"}  # LLM'in değerlendireceği bulgular


@dataclass
class ScanOutcome:
    ok: bool
    events: int = 0
    error: str = ""
    anomalies: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    findings: list[Event] = field(default_factory=list)  # yeni bulgular (LLM değerlendirmesi için)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def in_window(now_local: time, start: time, end: time) -> bool:
    return start <= now_local < end if start <= end else (now_local >= start or now_local < end)


class Engine:
    def __init__(self, settings: Settings, store: Store, alert_channel: AlertChannel | None = None):
        self.s = settings
        self.store = store
        self.alert_channel = alert_channel  # verilirse sistem uyarıları buraya gider; olmazsa Telegram'a
        self.on_findings: FindingsHook | None = None  # her taramadaki yeni bulgular (ör. LLM değerlendirmesi)
        self._background: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()        # site erişimi tek sıra: tarama ve istek üzerine açılan sayfalar
        self.flush_lock = asyncio.Lock()  # iki gönderici aynı bekleyen olayı iki kez yollamasın

    # ── Zamanlama yardımcıları ────────────────────────────────────────────
    def is_night(self, now: datetime) -> bool:
        return in_window(now.astimezone(self.s.tz).time(), self.s.night_start, self.s.night_end)

    def next_scan_delay(self, now: datetime) -> float:
        minutes = self.s.night_poll_interval_min if self.is_night(now) else self.s.poll_interval_min
        return max(60.0, minutes * 60 + random.uniform(-120, 120))

    def muted_until(self) -> datetime | None:
        raw = self.store.get("mute_until")
        return datetime.fromisoformat(raw) if raw else None

    def set_mute(self, until: datetime | None) -> None:
        self.store.set("mute_until", until.isoformat() if until else "")

    # ── Tarama turu ───────────────────────────────────────────────────────
    async def run_scan(self, reason: str = "zamanlanmış") -> ScanOutcome:
        outcome = await self._run_scan_locked(reason)
        if outcome.findings and self.on_findings is not None:
            # Arka planda: değerlendirme (LLM) taramayı ve normal bildirimleri asla bekletmez
            task = asyncio.get_running_loop().create_task(self._dispatch_findings(outcome.findings))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return outcome

    async def _dispatch_findings(self, findings: list[Event]) -> None:
        try:
            await self.on_findings(findings)
        except Exception as e:  # noqa: BLE001 - değerlendirme hatası sadece kaydedilir
            log.warning("Bulgu değerlendirmesi başarısız: %s", e)
            self.record_error("llm", f"olay değerlendirme: {type(e).__name__}: {e}")

    async def _run_scan_locked(self, reason: str) -> ScanOutcome:
        async with self.lock:
            started = utcnow()
            try:
                async with Campus(self.s) as campus:
                    scan, report = await Scanner(campus, self.s).scan()
            except LoginCooldown as e:
                return self._failure(started, f"login beklemesi: {e}", count=False)
            except (AuthBlocked, LoginRejected, CaptchaRequired) as e:
                self._auth_alert(e)
                return self._failure(started, f"{type(e).__name__}: {e}", count=False)
            except (AuthError, FetchError, PlaywrightError, OSError) as e:
                return self._failure(started, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")

            now = utcnow()
            result = diff(self.store.stored_items(), self.store.known_scopes(), scan)
            self.store.apply(scan, result, now)
            self._track_parse_errors(report.parse_errors, now)
            self._track_anomalies(result.anomalies, now)
            self._recovered(now)
            self.store.set("last_scan", json.dumps({
                "at": now.isoformat(), "ok": True, "reason": reason, "complete": scan.complete,
                "events": len(result.events), "duration_s": round((now - started).total_seconds(), 1),
                "failed": {f"{k}/{s}": e for (k, s), e in scan.failed_scopes.items()},
            }, ensure_ascii=False))
            self.store.set("last_ok_at", now.isoformat())
            self.store.set("courses", json.dumps([c.__dict__ for c in report.courses], ensure_ascii=False))
            log.info("Tarama tamam (%s): %d kayıt, %d olay, tam=%s", reason, len(scan.items), len(result.events), scan.complete)
            self.touch()
            return ScanOutcome(True, len(result.events), anomalies=result.anomalies,
                               duration_s=(now - started).total_seconds(),
                               findings=[e for e in result.events if e.type in FINDING_TYPES])

    def _failure(self, started: datetime, error: str, count: bool = True, now: datetime | None = None) -> ScanOutcome:
        now = now or utcnow()
        log.warning("Tarama başarısız: %s", error)
        self.record_error("tarama", error, now)
        self.store.set("last_scan", json.dumps({"at": now.isoformat(), "ok": False, "error": error}, ensure_ascii=False))
        if count:
            streak = int(self.store.get("fail_streak", "0")) + 1
            self.store.set("fail_streak", str(streak))
            if streak == 1:
                self.store.set("fail_since", now.isoformat())
            if streak >= PR.load(self.store)["fail_after"] and not self.store.get("fail_alerted"):
                since = self.store.get("fail_since", now.isoformat())
                last_ok = self.store.get("last_ok_at")
                lines = [f"<b>e-Kampüs'e girilemiyor</b> ({streak} kontrol üst üste)",
                         f"Neden: {describe_failure(error)}",
                         f"<code>{_esc(error[:250])}</code>"]
                if last_ok:
                    lines.append(f"Son başarılı kontrol: {fmt_dt(datetime.fromisoformat(last_ok), self.s.tz, now)}")
                lines.append("Denemeye devam ediyorum, düzelince haber vereceğim.")
                self.alert(f"fail:{since}", "\n".join(lines), now, urgent=False, priority=1)
                self.store.set("fail_alerted", "1")
        self.touch()
        return ScanOutcome(False, error=error, duration_s=(now - started).total_seconds())

    def _recovered(self, now: datetime) -> None:
        since = self.store.get("fail_since")
        if self.store.get("fail_alerted") and since:
            pending_alert = self.store.outbox_by_key(f"alert:fail:{since}")
            if pending_alert is not None and pending_alert["status"] == "pending":
                self.store.mark_status(pending_alert["id"], "skipped")  # kesinti sen görmeden bitti
            else:
                minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
                took = f"{minutes // 60} sa {minutes % 60} dk" if minutes >= 60 else f"{minutes} dk"
                self.alert(f"recovered:{since}", f"<b>e-Kampüs'e yeniden ulaşılıyor</b> (kesinti ~{took}). "
                            "Bu sürede gelenler varsa şimdi bildiriyorum.", now, urgent=False, priority=0)
        self.store.set("fail_streak", "0")
        self.store.set("fail_alerted", "")

    def _auth_alert(self, error: Exception) -> None:
        status = AuthGuard(self.s.auth_guard_path, self.s.username, self.s.password).status()
        stamp = status.get("blocked_at") or utcnow().date().isoformat()
        if isinstance(error, CaptchaRequired):
            text = ("<b>Giriş captcha istiyor.</b>\nBilgisayarda bir kez elle gir: "
                    "<code>python -m ekampus login --headed</code>\nSonra /girisdene yaz.")
        else:
            text = (f"<b>e-Kampüs girişi reddedildi.</b>\n<i>{_esc(str(error)[:200])}</i>\n"
                    "Hesabın kilitlenmesin diye tekrar denemiyorum. Şifren değiştiyse .env'i güncelle "
                    "ve botu yeniden başlat ya da /girisdene yaz.")
        self.alert(f"auth:{stamp}", text, utcnow(), critical=True, priority=2)

    def _track_parse_errors(self, errors: dict[str, str], now: datetime) -> None:
        streaks: dict[str, int] = json.loads(self.store.get("parse_streaks", "{}"))
        streaks = {scope: streaks.get(scope, 0) + 1 for scope in errors}
        for scope, n in streaks.items():
            if n == PARSE_ALERT_AFTER.get(scope, 2):
                if scope == "announce":
                    text = ("<b>Duyurular sayfasında yeni bir şey var ama biçimini okuyamadım.</b>\n"
                            "Siteye bir göz at; sayfayı inceleyip düzeltilmesi için kaydettim.")
                else:
                    text = (f"<b>{_esc(scope)} sayfasının yapısı değişmiş görünüyor.</b>\n"
                            f"<i>{_esc(errors[scope][:200])}</i>\nO bölüm düzelene kadar eksik izlenebilir.")
                self.alert(f"parse:{scope}:{now.date().isoformat()}", text, now, priority=1)
        self.store.set("parse_streaks", json.dumps(streaks))

    def _track_anomalies(self, anomalies: list[str], now: datetime) -> None:
        streak = int(self.store.get("anomaly_streak", "0")) + 1 if anomalies else 0
        self.store.set("anomaly_streak", str(streak))
        if streak == ANOMALY_ALERT_AFTER:
            self.alert(f"anomaly:{now.date().isoformat()}", "<b>Sitede beklenmedik bir azalma var.</b>\n"
                        + _esc("; ".join(anomalies)[:400]) + "\nHiçbir şeyi silinmiş saymıyorum; durum düzelince normale döner.", now,
                       priority=1)

    def alert(self, key: str, text: str, now: datetime, *, urgent: bool = True, critical: bool = False,
              priority: int = 0) -> None:
        """urgent: gece/sessiz modda da gönder. critical: uyarı yöneticisinden kapatılamaz (giriş sorunları).
        priority: Pushover önceliği (-1 sessiz, 0 normal, 1 yüksek, 2 acil: onaylanana kadar tekrar çalar)."""
        payload = {"text": text, "urgent": urgent, "critical": critical, "priority": priority}
        self.store.enqueue(Event("alert", f"alert:{key}", payload), now)

    # ── Hata artışı ve çökme takibi ───────────────────────────────────────
    def record_error(self, source: str, message: str, now: datetime | None = None) -> None:
        """Her hatayı kaydeder; son 1 saatte ERROR_SPIKE_PER_HOUR kadar birikirse (saatte en fazla bir) uyarır."""
        now = now or utcnow()
        cutoff = now - timedelta(hours=1)
        errors = [e for e in json.loads(self.store.get("errors", "[]")) if datetime.fromisoformat(e[0]) > cutoff]
        errors.append([now.isoformat(), source, message[:200]])
        self.store.set("errors", json.dumps(errors[-100:], ensure_ascii=False))
        if len(errors) < self.s.error_spike_per_hour:
            return
        last = self.store.get("spike_alerted_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=1):
            return
        counts = defaultdict(int)
        for _, src, _ in errors:
            counts[src] += 1
        lines = [f"<b>Hata artışı:</b> son 1 saatte {len(errors)} hata"]
        lines += [f"• {_esc(src)}: {n}" for src, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        lines.append(f"Son hata ({_esc(source)}): <i>{_esc(message[:200])}</i>")
        self.alert(f"spike:{now.isoformat(timespec='minutes')}", "\n".join(lines), now, urgent=False, priority=2)
        self.store.set("spike_alerted_at", now.isoformat())

    def on_start(self, now: datetime | None = None) -> None:
        """Önceki çalışma düzgün kapanmadıysa (çökme, bellek dolması, takılma) haber ver."""
        now = now or utcnow()
        try:
            last_alive = datetime.fromtimestamp(self.s.heartbeat_path.stat().st_mtime, timezone.utc)
        except OSError:
            last_alive = None
        alive = f"\nSon canlılık işareti: {fmt_dt(last_alive, self.s.tz, now)}" if last_alive else ""
        marker = self.s.watchdog_marker_path
        if marker.exists():
            marker.unlink(missing_ok=True)
            self.alert(f"restart:{now.isoformat(timespec='seconds')}",
                       "<b>Bot takıldığı için yeniden başlatıldı</b> ve tekrar çalışıyor." + alive, now, urgent=False,
                       priority=1)
        elif self.store.get("running") == "1":
            self.alert(f"crash:{now.isoformat(timespec='seconds')}",
                       "<b>Bot beklenmedik şekilde kapanmıştı</b> ve yeniden başladı." + alive, now,
                       urgent=False, priority=2)
        self.store.set("running", "1")

    # ── Sahibine öne çıkan uyarı (LLM'in soyut "notify_owner" aracının arkası) ──
    def _assistant_key(self, now: datetime) -> str:
        return f"assistant_alerts:{now.astimezone(self.s.tz).date().isoformat()}"

    def notify_quota(self, now: datetime | None = None) -> int:
        """Bugün kalan uyarı hakkı; kullanıcı "Asistan uyarıları"nı kapattıysa 0."""
        now = now or utcnow()
        if not PR.load(self.store).get("assistant", True):
            return 0
        return max(0, self.s.llm_alerts_per_day - int(self.store.get(self._assistant_key(now), "0")))

    def notify_owner(self, title: str, message: str, reason: str = "", now: datetime | None = None) -> dict:
        """Uyarıyı kuyruğa koyar; kanal (Pushover/Telegram) ve öncelik burada belirlenir, çağırana söylenmez."""
        now = now or utcnow()
        title = " ".join(str(title or "").split())[:80]
        message = str(message or "").strip()[:600]
        if not title or not message:
            return {"durum": "gönderilmedi", "neden": "başlık ve mesaj gerekli"}
        left = self.notify_quota(now)
        if left <= 0:
            return {"durum": "gönderilmedi", "neden": "bugünkü uyarı hakkı doldu ya da kullanıcı bu uyarıları kapattı"}
        digest = hashlib.sha1(f"{title}|{message}".encode()).hexdigest()[:12]
        day = now.astimezone(self.s.tz).date().isoformat()
        text = f"<b>Asistan: {_esc(title)}</b>\n{_esc(message)}"
        payload = {"text": text, "urgent": True, "critical": False, "priority": 1, "category": "assistant",
                   "reason": str(reason or "")[:300]}
        if not self.store.enqueue(Event("alert", f"alert:assistant:{day}:{digest}", payload), now):
            return {"durum": "gönderilmedi", "neden": "aynı uyarı bugün zaten gönderildi"}
        used = int(self.store.get(self._assistant_key(now), "0")) + 1
        self.store.set(self._assistant_key(now), str(used))
        return {"durum": "gönderildi", "bugün_kalan_hak": max(0, self.s.llm_alerts_per_day - used)}

    def on_clean_exit(self) -> None:
        self.store.set("running", "0")

    def touch(self) -> None:
        try:
            self.s.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.s.heartbeat_path.write_text(utcnow().isoformat(), encoding="utf-8")
        except OSError:
            pass

    # ── Hatırlatmalar ─────────────────────────────────────────────────────
    def plan_reminders(self, now: datetime) -> int:
        planned = plan_reminders(self.store.due_items(), self.s.reminder_hours, self.s.live_lesson_reminder_min, now)
        return self.store.enqueue_planned(planned, now)

    # ── Outbox gönderimi ──────────────────────────────────────────────────
    def hold_reason(self, now: datetime) -> str | None:
        muted = self.muted_until()
        if muted and muted > now:
            return "sessiz"
        if self.is_night(now) and PR.load(self.store)["night"]:
            return "gece"
        return None

    @staticmethod
    def is_urgent(row) -> bool:
        if row["type"] == "alert":
            return json.loads(row["payload"]).get("urgent", True)
        if row["type"] in ("live_soon", "baseline"):
            return True
        if row["type"] == "reminder":
            return json.loads(row["payload"]).get("hours", 99) <= 3
        return False

    async def flush(self, send: Sender, now: datetime | None = None) -> int:
        async with self.flush_lock:
            return await self._flush(send, now or utcnow())

    async def _flush(self, send: Sender, now: datetime) -> int:
        rows = self.store.pending(now)
        if not rows:
            return 0
        preferences = PR.load(self.store)
        enabled = []
        for row in rows:  # kapalı kategoriler kuyrukta birikmesin: 'muted' olarak kapat
            category = PR.category_of(row["type"], json.loads(row["payload"]))
            if category is not None and not preferences.get(category, True):
                self.store.mark_status(row["id"], "muted")
            else:
                enabled.append(row)
        held = self.hold_reason(now)
        rows = [r for r in enabled if not held or self.is_urgent(r)]

        groups: dict[tuple[str, str], list] = defaultdict(list)
        singles = []
        for row in rows:
            payload = json.loads(row["payload"])
            if row["type"] in GROUPABLE:
                groups[(row["type"], payload.get("kind", ""))].append((row, payload))
            else:
                singles.append((row, payload))
        for members in groups.values():
            if len(members) < GROUP_THRESHOLD:
                singles.extend(members)
        singles.sort(key=lambda rp: rp[0]["id"])

        sent = 0
        for (event_type, kind), members in groups.items():
            if len(members) >= GROUP_THRESHOLD:
                message = render_group(event_type, kind, [p for _, p in members], self.s.tz, now)
                if await self._deliver(send, message, {"type": "group", "kind": kind}, [r for r, _ in members], now):
                    sent += len(members)
        for row, payload in singles:
            message = render_event(row["type"], payload, self.s.tz, now)
            sender = self._alert_sender(send) if row["type"] == "alert" else send
            if await self._deliver(sender, message, {"type": row["type"], **payload}, [row], now):
                sent += 1
        return sent

    def _alert_sender(self, telegram: Sender) -> Sender:
        """Sistem uyarısı: önce Pushover; gönderilemezse kaybolmasın diye Telegram'a düş."""
        async def send(message: Message, context: dict) -> None:
            if self.alert_channel is not None:
                try:
                    await self.alert_channel(message.text, int(context.get("priority", 0)))
                    return
                except Exception as e:  # noqa: BLE001 - kanal hatası Telegram'a düşülerek tolere edilir
                    log.warning("Pushover'a gönderilemedi, Telegram'a düşülüyor: %s", e)
                    self.record_error("pushover", f"{type(e).__name__}: {e}")
            await telegram(message, context)
        return send

    async def _deliver(self, send: Sender, message: Message, context: dict, rows: list, now: datetime) -> bool:
        try:
            await send(message, context)
        except Exception as e:  # noqa: BLE001 - gönderim hatası asla döngüyü durdurmamalı
            log.warning("Bildirim gönderilemedi (%s): %s", context.get("type"), e)
            for row in rows:
                self.store.mark_failed(row["id"], f"{type(e).__name__}: {e}", now)
            self.record_error("gönderim", f"{context.get('type')}: {type(e).__name__}: {e}", now)
            return False
        for row in rows:
            self.store.mark_sent(row["id"], now)
        self.touch()
        return True

    # ── İstek üzerine site erişimi ────────────────────────────────────────
    async def assignment_detail(self, uid: str) -> P.AssignmentDetail:
        async with self.lock, Campus(self.s) as campus:
            scanner = Scanner(campus, self.s)
            return P.parse_assignment_detail(await scanner.get_with_session(f"/assignment/details/{int(uid)}"))

    async def download(self, url: str, title: str = "") -> Download:
        async with self.lock, Campus(self.s) as campus:
            return await _download(campus, url, title)

    async def fetch_material(self, uid: str) -> Download:
        """Ders materyalini açar (kullanıcı isteğiyle; içerik "görüldü" sayılır) ve dosyasını indirir."""
        item = self.store.item("file", uid)
        if not item:
            raise LookupError("materyal bulunamadı")
        async with self.lock, Campus(self.s) as campus:
            scanner = Scanner(campus, self.s)
            await scanner.ensure_session()
            response = await campus.context.request.get(item["url"], timeout=60_000)
            file_url = P.parse_content_file(await response.text())
            if not file_url:
                raise NotAFile(response.url)
            return await _download(campus, file_url, item["title"])


@dataclass
class Download:
    filename: str
    body: bytes | None   # None: Telegram'ın 50 MB sınırından büyük, sadece link gönderilir
    url: str


class NotAFile(Exception):
    """Materyal indirilebilir bir dosya değil (video, bağlantı vb.); args[0] içeriğin sitedeki adresi."""


_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def material_filename(title: str, url: str, content_type: str = "") -> str:
    """Telegram'da görünecek dosya adı: materyalin başlığı + dosyanın gerçek uzantısı."""
    ext = PurePosixPath(unquote(urlsplit(url).path)).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    base = re.sub(r"\s+", " ", _UNSAFE_NAME.sub(" ", title or "")).strip(" .")[:80]
    if not base:
        base = PurePosixPath(unquote(urlsplit(url).path)).stem or "dosya"
    return base if base.lower().endswith(ext) else base + ext


async def _download(campus: Campus, url: str, title: str = "") -> Download:
    head = await campus.context.request.head(url, timeout=30_000)
    size = int(head.headers.get("content-length", "0") or 0)
    filename = material_filename(title, url, head.headers.get("content-type", ""))
    if size > TELEGRAM_FILE_LIMIT:
        return Download(filename, None, url)
    response = await campus.context.request.get(url, timeout=120_000)
    if response.status >= 400:
        raise FetchError(f"dosya indirilemedi: HTTP {response.status}")
    body = await response.body()
    return Download(filename, body if len(body) <= TELEGRAM_FILE_LIMIT else None, url)



def describe_failure(error: str) -> str:
    """Teknik hatayı insan diline çevirir: sorun site mi, ağ mı, giriş mi?"""
    low = error.lower()
    if any(s in low for s in ("err_name_not_resolved", "err_internet_disconnected", "getaddrinfo")):
        return "İnternet bağlantısı yok gibi görünüyor (adres çözülemedi)."
    if any(s in low for s in ("timeout", "zaman aşımı", "timed out")):
        return "e-Kampüs yanıt vermiyor (zaman aşımı); site yavaş ya da kapalı olabilir."
    match = re.search(r"http (\d{3})", low)
    if match:
        code = int(match.group(1))
        if code >= 500:
            return f"e-Kampüs sunucu hatası veriyor (HTTP {code}); büyük ihtimalle sitede sorun var."
        return f"e-Kampüs isteği reddetti (HTTP {code})."
    if any(s in low for s in ("err_connection", "connection refused", "econnreset")):
        return "e-Kampüs bağlantıyı kabul etmiyor; site kapalı olabilir."
    if "ders listesi okunamadı" in low:
        return "Site açılıyor ama ders listesi okunamadı; bakım sayfası olabilir."
    if "yönlendirme" in low or "beklenmeyen adres" in low:
        return "Giriş yönlendirmesi tamamlanamadı; giriş sisteminde sorun olabilir."
    if "login beklemesi" in low:
        return "Kısa süre önce giriş denendi, güvenlik için bekleniyor."
    return "Beklenmeyen bir hata oluştu."

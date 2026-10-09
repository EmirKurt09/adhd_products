"""SQLite durumu: kayıtlar, kapsamlar, outbox ve küçük anahtar-değer ayarları.

Outbox: olaylar durum güncellemesiyle AYNI transaction'da yazılır. Anahtar UNIQUE olduğu için aynı
olay iki kez kuyruğa giremez; kayıt sadece Telegram başarıyla iletince 'sent' olur. Böylece bildirim
ne kaybolur ne de iki kez gelir.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .detect import MISSING_TO_GONE, Diff
from .models import Event, Item, Scan, StoredItem
from .reminders import Due, Planned

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    kind TEXT NOT NULL,
    uid TEXT NOT NULL,
    scope TEXT NOT NULL,
    title TEXT NOT NULL,
    course TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}',
    meta TEXT NOT NULL DEFAULT '{}',
    rank INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    missing_count INTEGER NOT NULL DEFAULT 0,
    done_manual INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, uid)
);
CREATE TABLE IF NOT EXISTS scopes (
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_ok TEXT,
    PRIMARY KEY (kind, scope)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    pages TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

LLM_LOG_KEEP = 30
MEMORY_MAX = 50        # kalıcı hafızadaki en fazla not (her LLM çağrısına eklendiği için sınırlı)
MEMORY_TEXT_MAX = 300
DOCUMENTS_KEEP = 20    # okunmuş PDF metinlerinin önbelleği (tekrar sorulunca siteye gitmemek için)

MAX_BACKOFF = timedelta(minutes=30)


class MemoryFull(Exception):
    pass


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ── Okuma ─────────────────────────────────────────────────────────────
    def stored_items(self) -> dict[tuple[str, str], StoredItem]:
        rows = self.db.execute(
            "SELECT kind, uid, scope, title, course, due_at, fingerprint, rank, status, missing_count, first_seen FROM items"
        )
        return {
            (r["kind"], r["uid"]): StoredItem(
                kind=r["kind"], uid=r["uid"], scope=r["scope"], title=r["title"], course=r["course"],
                due_iso=r["due_at"], fingerprint=r["fingerprint"], rank=r["rank"], status=r["status"],
                missing_count=r["missing_count"], first_seen=r["first_seen"],
            )
            for r in rows
        }

    def known_scopes(self) -> set[tuple[str, str]]:
        return {(r["kind"], r["scope"]) for r in self.db.execute("SELECT kind, scope FROM scopes")}

    def due_items(self) -> list[Due]:
        rows = self.db.execute(
            "SELECT kind, uid, title, course, due_at, first_seen, url, meta, done_manual FROM items "
            "WHERE status = 'active' AND due_at IS NOT NULL AND kind IN ('assignment', 'live')"
        )
        return [
            Due(
                kind=r["kind"], uid=r["uid"], title=r["title"], course=r["course"],
                due_at=datetime.fromisoformat(r["due_at"]), first_seen=datetime.fromisoformat(r["first_seen"]),
                url=r["url"], submitted=bool(json.loads(r["meta"]).get("submitted")),
                done_manual=bool(r["done_manual"]),
            )
            for r in rows
        ]

    # ── Tarama sonucunu uygula (tek transaction) ──────────────────────────
    def apply(self, scan: Scan, result: Diff, now: datetime) -> None:
        ts = _ts(now)
        with self.db:
            for item in result.upserts:
                self._upsert(item, ts)
            for kind, uid in result.seen:
                self.db.execute(
                    "UPDATE items SET last_seen = ?, missing_count = 0, status = 'active' WHERE kind = ? AND uid = ?",
                    (ts, kind, uid),
                )
            for kind, uid in result.missing:
                self.db.execute(
                    "UPDATE items SET missing_count = missing_count + 1, "
                    "status = CASE WHEN missing_count + 1 >= ? THEN 'gone' ELSE status END "
                    "WHERE kind = ? AND uid = ?",
                    (MISSING_TO_GONE, kind, uid),
                )
            for kind, scope in result.new_scopes:
                self.db.execute(
                    "INSERT OR IGNORE INTO scopes (kind, scope, first_seen) VALUES (?, ?, ?)", (kind, scope, ts)
                )
            for kind, scope in scan.ok_scopes:
                self.db.execute("UPDATE scopes SET last_ok = ? WHERE kind = ? AND scope = ?", (ts, kind, scope))
            for event in result.events:
                self._enqueue(event.key, event.type, event.data, now)

    def _upsert(self, item: Item, ts: str) -> None:
        self.db.execute(
            """
            INSERT INTO items (kind, uid, scope, title, course, due_at, body, url, extra, meta, rank,
                               fingerprint, status, missing_count, first_seen, last_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            ON CONFLICT (kind, uid) DO UPDATE SET
                scope = excluded.scope, title = excluded.title, course = excluded.course,
                due_at = excluded.due_at, body = excluded.body, url = excluded.url, extra = excluded.extra,
                meta = json_patch(items.meta, excluded.meta), rank = excluded.rank, fingerprint = excluded.fingerprint,
                status = 'active', missing_count = 0, last_seen = excluded.last_seen,
                updated_at = CASE WHEN items.fingerprint != excluded.fingerprint
                                  THEN excluded.updated_at ELSE items.updated_at END
            """,
            (
                item.kind, item.uid, item.scope, item.title, item.course, item.due_iso, item.body, item.url,
                json.dumps(item.extra, ensure_ascii=False), json.dumps(item.meta, ensure_ascii=False),
                item.rank, item.fingerprint(), ts, ts, ts,
            ),
        )

    # ── Outbox ────────────────────────────────────────────────────────────
    def enqueue(self, event: Event, now: datetime, not_before: datetime | None = None) -> bool:
        """not_before: bu zamandan önce gönderilmez (kişisel hatırlatmalar)."""
        with self.db:
            return self._enqueue(event.key, event.type, event.data, now, not_before=not_before)

    def enqueue_planned(self, planned: list[Planned], now: datetime) -> int:
        added = 0
        with self.db:
            for p in planned:
                added += self._enqueue(p.key, p.type, p.data, now, status=p.status)
        return added

    def _enqueue(self, key: str, type_: str, data: dict, now: datetime, status: str = "pending",
                 not_before: datetime | None = None) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO outbox (key, type, payload, status, created_at, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, type_, json.dumps(data, ensure_ascii=False), status, _ts(now), _ts(not_before or now)),
        )
        return cur.rowcount == 1

    # ── Kişisel hatırlatmalar (outbox'ta 'note' olayları) ─────────────────
    def notes_pending(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, payload FROM outbox WHERE type = 'note' AND status = 'pending' ORDER BY next_attempt_at, id"
        )
        return [{"id": r["id"], **json.loads(r["payload"])} for r in rows]

    def cancel_note(self, outbox_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE outbox SET status = 'skipped' WHERE id = ? AND type = 'note' AND status = 'pending'", (outbox_id,)
            )
        return cur.rowcount == 1

    def pending(self, now: datetime, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (_ts(now), limit),
        ))

    def mark_sent(self, outbox_id: int, now: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET status = 'sent', sent_at = ? WHERE id = ?", (_ts(now), outbox_id))

    def mark_failed(self, outbox_id: int, error: str, now: datetime) -> None:
        row = self.db.execute("SELECT attempts FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        delay = min(timedelta(minutes=2 ** min(attempts - 1, 5)), MAX_BACKOFF)
        with self.db:
            self.db.execute(
                "UPDATE outbox SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                (attempts, error[:500], _ts(now + delay), outbox_id),
            )

    def mark_status(self, outbox_id: int, status: str) -> None:
        """pending dışı son durum: muted (kategori kapalı), skipped (artık anlamsız)."""
        with self.db:
            self.db.execute("UPDATE outbox SET status = ? WHERE id = ?", (status, outbox_id))

    def outbox_by_key(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM outbox WHERE key = ?", (key,)).fetchone()

    def recent_sent(self, limit: int = 15) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM outbox WHERE status = 'sent' ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,)
        ))

    def sent_since(self, since: datetime) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM outbox WHERE status = 'sent' AND sent_at >= ?", (_ts(since),)).fetchone()
        return row["n"]

    # ── Kullanıcı işaretleri ve ayarlar ───────────────────────────────────
    def set_done(self, kind: str, uid: str, done: bool = True) -> None:
        with self.db:
            self.db.execute("UPDATE items SET done_manual = ? WHERE kind = ? AND uid = ?", (int(done), kind, uid))

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ── Görünümler (bot komutları ve LLM araçları için) ────────────────────
    def item(self, kind: str, uid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM items WHERE kind = ? AND uid = ?", (kind, uid)).fetchone()
        return _row_dict(row) if row else None

    def find(self, uid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM items WHERE uid = ? ORDER BY kind LIMIT 1", (uid,)).fetchone()
        return _row_dict(row) if row else None

    def items(self, kinds: tuple[str, ...], *, active_only: bool = True, order: str = "first_seen DESC",
              limit: int = 500) -> list[dict]:
        marks = ",".join("?" * len(kinds))
        where = f"kind IN ({marks})" + (" AND status = 'active'" if active_only else "")
        order_sql = {"first_seen DESC": "first_seen DESC", "due_at": "due_at IS NULL, due_at"}[order]
        rows = self.db.execute(f"SELECT * FROM items WHERE {where} ORDER BY {order_sql} LIMIT ?", (*kinds, limit))
        return [_row_dict(r) for r in rows]

    def search(self, text: str, limit: int = 20) -> list[dict]:
        like = f"%{text}%"
        rows = self.db.execute(
            "SELECT * FROM items WHERE status = 'active' AND (title LIKE ? OR body LIKE ? OR course LIKE ?) "
            "ORDER BY first_seen DESC LIMIT ?", (like, like, like, limit),
        )
        return [_row_dict(r) for r in rows]

    def outbox_stats(self) -> dict:
        row = self.db.execute(
            # kurulmuş ama zamanı gelmemiş kişisel hatırlatmalar "bekleyen bildirim" sayılmaz
            "SELECT SUM(status = 'pending' AND type != 'note') AS pending, MAX(sent_at) AS last_sent, "
            "SUM(status = 'pending' AND attempts > 0) AS failing FROM outbox"
        ).fetchone()
        return {"pending": row["pending"] or 0, "failing": row["failing"] or 0, "last_sent": row["last_sent"]}

    # ── LLM sohbet geçmişi ────────────────────────────────────────────────
    # Geçici çözüm: son N mesajı tutup her soruda tekrar gönderiyoruz ki model bağlamı kaybedip
    # boşa dolaşmasın. Özetleme / kalıcı hafıza gibi geliştirmeler sonraya bırakıldı.
    def chat_add(self, role: str, content: str, now: datetime, keep: int = 20) -> None:
        with self.db:
            self.db.execute("INSERT INTO chat (role, content, created_at) VALUES (?, ?, ?)", (role, content, _ts(now)))
            self.db.execute("DELETE FROM chat WHERE id NOT IN (SELECT id FROM chat ORDER BY id DESC LIMIT ?)", (keep,))

    def chat_recent(self, limit: int) -> list[dict]:
        rows = self.db.execute("SELECT role, content FROM chat ORDER BY id DESC LIMIT ?", (limit,))
        history = [dict(r) for r in reversed(list(rows))]
        while history and history[0]["role"] != "user":  # pencere bir cevabın ortasından başlamasın
            history.pop(0)
        return history

    def chat_clear(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM chat")

    # ── Kalıcı hafıza (/hafiza) ───────────────────────────────────────────
    # LLM'in öğrenci hakkında kalıcı notları: tercihler, planlar, bilgiler. Sohbet geçmişinden bağımsızdır,
    # /unut onu silmez; her LLM çağrısında sistem talimatına eklenir.
    def memory_add(self, text: str, now: datetime) -> tuple[int, bool]:
        """(id, yeni mi). Aynı not zaten varsa onun id'si döner. Hafıza doluysa MemoryFull."""
        text = " ".join(str(text or "").split())[:MEMORY_TEXT_MAX]
        if not text:
            raise ValueError("boş not")
        existing = self.memory_list()
        same = next((m for m in existing if m["text"].casefold() == text.casefold()), None)
        if same:
            return same["id"], False
        if len(existing) >= MEMORY_MAX:
            raise MemoryFull(f"hafıza dolu ({MEMORY_MAX} not); önce eski bir notu sil")
        with self.db:
            cur = self.db.execute("INSERT INTO memory (text, created_at) VALUES (?, ?)", (text, _ts(now)))
        return cur.lastrowid, True

    def memory_list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, text, created_at FROM memory ORDER BY id")]

    def memory_delete(self, memory_id: int) -> str | None:
        """Silinen notun metni; yoksa None."""
        row = self.db.execute("SELECT text FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        with self.db:
            self.db.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
        return row["text"]

    def memory_clear(self) -> int:
        with self.db:
            return self.db.execute("DELETE FROM memory").rowcount

    # ── Ajan için salt okunur sorgu ───────────────────────────────────────
    def read_only_query(self, sql: str, limit: int = 50, functions: dict | None = None,
                        max_seconds: float = 2.0) -> tuple[list[tuple], list[str]]:
        """SQLite yetkilendiricisiyle sadece okuma: yazma, ATTACH, PRAGMA ve şema değişikliği reddedilir;
        uzun süren sorgu yarıda kesilir. (satırlar, sütun adları)"""
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorize(action, *_):
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        deadline = time.monotonic() + max_seconds
        for name, fn in (functions or {}).items():
            self.db.create_function(name, 1, fn, deterministic=True)
        self.db.set_authorizer(authorize)
        self.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cur = self.db.execute(sql)
            rows = [tuple(r) for r in cur.fetchmany(limit)]
            return rows, [d[0] for d in cur.description or []]
        finally:
            self.db.set_authorizer(None)
            self.db.set_progress_handler(None, 0)

    # ── Okunmuş belgeler (PDF metni, sayfa sayfa) ─────────────────────────
    def doc_get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT title, pages FROM documents WHERE key = ?", (key,)).fetchone()
        return {"title": row["title"], "pages": json.loads(row["pages"])} if row else None

    def doc_put(self, key: str, title: str, pages: list[str], now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO documents (key, title, pages, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET title = excluded.title, pages = excluded.pages, "
                "created_at = excluded.created_at",
                (key, title, json.dumps(pages, ensure_ascii=False), _ts(now)),
            )
            self.db.execute(f"DELETE FROM documents WHERE key NOT IN "
                            f"(SELECT key FROM documents ORDER BY created_at DESC LIMIT {DOCUMENTS_KEEP})")

    # ── LLM kayıtları (/llmlog) ───────────────────────────────────────────
    def llm_log_add(self, trace: dict, now: datetime) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO llm_log (created_at, data) VALUES (?, ?)",
                                  (_ts(now), json.dumps(trace, ensure_ascii=False, default=str)))
            self.db.execute(f"DELETE FROM llm_log WHERE id NOT IN (SELECT id FROM llm_log ORDER BY id DESC LIMIT {LLM_LOG_KEEP})")
        return cur.lastrowid

    def llm_log_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM llm_log").fetchone()["n"]

    def llm_log_at(self, index: int) -> dict | None:
        """index 0 = en yeni kayıt."""
        row = self.db.execute("SELECT * FROM llm_log ORDER BY id DESC LIMIT 1 OFFSET ?", (index,)).fetchone()
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["data"])} if row else None

    def llm_log_by_id(self, log_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM llm_log WHERE id = ?", (log_id,)).fetchone()
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["data"])} if row else None

    def llm_log_recent(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT * FROM llm_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "created_at": r["created_at"], **json.loads(r["data"])} for r in rows]


def _row_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["extra"] = json.loads(d.get("extra") or "{}")
    d["meta"] = json.loads(d.get("meta") or "{}")
    d["due"] = datetime.fromisoformat(d["due_at"]) if d.get("due_at") else None
    d["submitted"] = bool(d["meta"].get("submitted"))
    d["grade"] = d["meta"].get("grade")
    return d

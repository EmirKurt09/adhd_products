"""Bulgu yönlendirici: her yeni bulguyu önce JEV'e sorar, sonra kodda karar verir.

    push_now ≥ JEV_PUSH_HIGH          → uyarı (şablon metin, öncelik 1; günlük hak ve aç/kapa geçerli)
    JEV_PUSH_LOW < push_now < HIGH    → kararsız: Grok bağlama bakıp karar verir
    push_now ≤ JEV_PUSH_LOW           → uyarı yok
    needs_explanation ≥ JEV_EXPLAIN_MIN → Grok kısa açıklama yazar, asıl bildirimden sonra Telegram'a gider

JEV hata verirse o bulgu Grok'a düşer; hiçbir bulgu karar verilmeden kalmaz.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .jev import JevDecision, build_state
from .messages import fmt_dt, parse_dt, remaining
from .models import Event

log = logging.getLogger(__name__)

ROUTED_TYPES = {"new", "due_changed", "changed"}
MAX_PER_SCAN = 30


@dataclass(frozen=True)
class Route:
    push: str      # send | ask_llm | none
    explain: bool


def decide(decision: JevDecision, settings: Settings) -> Route:
    p = decision.p("push_now")
    push = "send" if p >= settings.jev_push_high else ("none" if p <= settings.jev_push_low else "ask_llm")
    return Route(push, decision.p("needs_explanation") >= settings.jev_explain_min)


def push_text(event: Event, decision: JevDecision, item: dict | None, settings: Settings,
              now: datetime) -> tuple[str, str]:
    """JEV metin üretmez: uyarı metni bulgudan ve JEV etiketlerinden şablonla kurulur."""
    data = event.data
    lines = [data.get("course") or ""]
    if decision.labels:
        lines.append(" · ".join(decision.labels))
    due = parse_dt(data.get("due_at"))
    if due:
        line = f"Teslim: {fmt_dt(due, settings.tz, now)} · {remaining(due, now)}"
        if data.get("kind") == "assignment":
            submitted = (data.get("meta") or {}).get("submitted") or (item and (item.get("submitted") or item.get("done_manual")))
            line += " · teslim edildi" if submitted else " · teslim edilmedi"
        lines.append(line)
    return str(data.get("title") or "Önemli bulgu"), "\n".join(x for x in lines if x)


class FindingRouter:
    def __init__(self, settings: Settings, engine, assistant, jev):
        self.s = settings
        self.engine = engine
        self.assistant = assistant
        self.jev = jev

    async def route(self, findings: list[Event]) -> None:
        now = datetime.now(timezone.utc)
        uncertain: list[Event] = []
        notes: list[str] = []
        for event in [e for e in findings if e.type in ROUTED_TYPES][:MAX_PER_SCAN]:
            data = event.data
            item = self.engine.store.item(data.get("kind"), data.get("uid")) if data.get("uid") else None
            state = build_state(event, item, now)
            started = time.monotonic()
            try:
                decision = await self.jev.ask(state)
            except Exception as e:  # noqa: BLE001 - JEV yoksa karar Grok'a kalır
                log.warning("JEV kararı alınamadı (%s): %s", data.get("title"), e)
                self.engine.record_error("jev", f"{type(e).__name__}: {e}")
                uncertain.append(event)
                notes.append(f"{data.get('title')}: hızlı karar alınamadı")
                self._log(event, state, None, ["JEV hatası → Grok karar verecek"], started, error=str(e))
                continue

            route = decide(decision, self.s)
            actions = []
            if route.push == "send":
                title, message = push_text(event, decision, item, self.s, now)
                result = self.engine.notify_owner(title, message, reason=f"JEV push_now={decision.p('push_now'):.2f}")
                actions.append("uyarı gönderildi" if result["durum"] == "gönderildi" else f"uyarı yok ({result['neden']})")
            elif route.push == "ask_llm":
                uncertain.append(event)
                labels = ", ".join(decision.labels) or "etiket yok"
                notes.append(f"{data.get('title')}: {labels}; uyarı olasılığı %{round(decision.p('push_now') * 100)}")
                actions.append("kararsız → Grok karar verecek")
            else:
                actions.append("uyarı gerekmiyor")

            if route.explain:
                actions.append(await self._explain(event, decision))
            self._log(event, state, decision, actions, started)

        if uncertain and self.assistant is not None:
            await self.assistant.triage(uncertain, notes=notes)

    async def _explain(self, event: Event, decision: JevDecision) -> str:
        if self.assistant is None:
            return "açıklama atlandı (LLM yok)"
        try:
            text = await self.assistant.explain(event, decision.labels)
        except Exception as e:  # noqa: BLE001 - açıklama "en iyi çaba"dır
            self.engine.record_error("llm", f"açıklama: {type(e).__name__}: {e}")
            return "açıklama yazılamadı"
        if text and self.engine.enqueue_explanation(event, text):
            return "açıklama yazıldı"
        return "açıklama yok"

    def _log(self, event: Event, state: dict, decision: JevDecision | None, actions: list[str], started: float,
             error: str | None = None) -> None:
        trace = {
            "kind": "JEV karar", "question": str(event.data.get("title") or ""), "model": decision.model if decision else "",
            "tokens": decision.tokens if decision else 0, "steps": [], "duration_s": round(time.monotonic() - started, 2),
            "decisions": decision.probs if decision else {}, "labels": decision.labels if decision else [],
            "actions": actions, "state": state,
        }
        if error:
            trace["error"] = error[:300]
        try:
            self.engine.store.llm_log_add(trace, datetime.now(timezone.utc))
        except Exception as e:  # noqa: BLE001 - kayıt kararı engellemez
            log.warning("JEV kaydı yazılamadı: %s", e)

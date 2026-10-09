"""Ajanın riskli eylemleri için JEV kontrolü. Döngüde insan yoktur.

Ajan bir ayarı kapatmak, sessiz moda almak, hafızadan silmek gibi riskli bir eylem yapmak istediğinde, önce
JEV'e iki soru sorulur:
    explicitly_requested  öğrenci bunu (aynı hedef, uyumlu zaman/kapsam) açıkça istedi mi?
    risky                 bu eylem öğrencinin teslim, sınav, not ya da önemli bir duyuruyu kaçırmasına
                          veya kayıtlı bilginin silinmesine yol açabilir mi?
Karar kodda verilir: açıkça istendiyse yapılır; istenmediği belirsizken riskliyse durdurulur, değilse yapılır.
JEV kapalıysa ya da ulaşılamıyorsa eylem yapılır (bugünkü davranış) ve bu kayda geçer.
Her kontrol /llmlog'da "JEV eylem kontrolü" olarak görünür.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .jev import JevDecision

log = logging.getLogger(__name__)

REQUESTED_MIN = 0.6
RISKY_MIN = 0.5

QUESTIONS: dict[str, dict] = {
    "explicitly_requested": {"type": "noul", "instructions": (
        "A student is chatting with their study-assistant bot. 'student_message' is what the student just wrote; "
        "'conversation' is the recent chat, where the assistant may have proposed something the student is now "
        "confirming. Did the student clearly ask for 'proposed_action': the same kind of action, on the same target, "
        "with a compatible time or scope? Answer yes only if a reasonable person would say the student asked for "
        "exactly this. Texts may be in Turkish.")},
    "risky": {"type": "noul", "instructions": (
        "Regardless of who asked for it: could 'proposed_action' make the student miss a deadline, an exam, a grade "
        "or an important announcement, keep notifications silent for a long time, or delete information they saved? "
        "Texts may be in Turkish.")},
}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


class ActionGuard:
    def __init__(self, jev, engine):
        self.jev = jev
        self.engine = engine

    async def check(self, turn, tool, args: dict) -> Verdict:
        if self.jev is None or not self.engine.feature_on("jev"):
            return Verdict(True, "JEV kapalı, kontrol yapılmadı")
        state = {
            "conversation": list(getattr(turn, "context", []) or [])[-4:],
            "student_message": (getattr(turn, "user_text", "") or "")[:600],
            "proposed_action": {"tool": tool.name, "what_it_does": tool.description[:300], "arguments": args},
        }
        started = time.monotonic()
        try:
            decision = await self.jev.ask(state, QUESTIONS)
        except Exception as e:  # noqa: BLE001 - kontrol yapılamazsa eylem engellenmez, kayda geçer
            log.warning("Eylem kontrolü yapılamadı (%s): %s", tool.name, e)
            self.engine.record_error("jev", f"eylem kontrolü: {type(e).__name__}: {e}")
            verdict = Verdict(True, "JEV'e ulaşılamadı, kontrol yapılmadı")
            self._log(tool.name, args, state, None, verdict, started, error=str(e))
            return verdict
        verdict = decide(decision)
        self._log(tool.name, args, state, decision, verdict, started)
        return verdict

    def _log(self, name: str, args: dict, state: dict, decision: JevDecision | None, verdict: Verdict,
             started: float, error: str | None = None) -> None:
        trace = {
            "kind": "JEV eylem kontrolü", "question": f"{name}({json.dumps(args, ensure_ascii=False)})"[:300],
            "model": decision.model if decision else "", "tokens": decision.tokens if decision else 0, "steps": [],
            "duration_s": round(time.monotonic() - started, 2), "decisions": decision.probs if decision else {},
            "actions": [("izin verildi: " if verdict.allowed else "durduruldu: ") + verdict.reason], "state": state,
        }
        if error:
            trace["error"] = error[:300]
        try:
            self.engine.store.llm_log_add(trace, datetime.now(timezone.utc))
        except Exception as e:  # noqa: BLE001
            log.warning("Eylem kontrolü kaydedilemedi: %s", e)


def decide(decision: JevDecision) -> Verdict:
    requested, risky = decision.p("explicitly_requested"), decision.p("risky")
    if requested >= REQUESTED_MIN:
        return Verdict(True, f"öğrenci açıkça istedi (%{round(requested * 100)})")
    if risky >= RISKY_MIN:
        return Verdict(False, f"öğrencinin bunu açıkça istediğinden emin olunamadı (%{round(requested * 100)}) "
                              f"ve riskli (%{round(risky * 100)})")
    return Verdict(True, f"düşük risk (%{round(risky * 100)})")

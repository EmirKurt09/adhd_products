"""TypeSafe JEV: her yeni bulgu için hızlı, tipli kararlar (System One modeli).

JEV metin üretmez; bir "state" ve evet/hayır soruları alır, her soru için olasılık döndürür. Belgelerine göre
tarih ve sayı hesabında güvenilmez: süreler ve tarih karşılaştırmaları burada kodda hesaplanıp `facts` olarak
verilir, JEV'e sadece anlam soruları sorulur. Türkçe desteği belgelenmediği için talimatlar İngilizce, içerik
olduğu gibi (Türkçe) gider. https://docs.typesafe.ai
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .messages import parse_dt
from .models import Event

API_URL = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUSES = {429, 529}
MAX_ATTEMPTS = 3
TEXT_LIMIT = 1500

QUESTIONS: dict[str, dict] = {
    "requires_submission": {"type": "noul", "instructions": (
        "Does `finding.title` or `finding.text` ask the student to submit, upload or hand in something "
        "(homework, report, file, project)? The text may be in Turkish (teslim, yükleyin, gönderin).")},
    "exam_related": {"type": "noul", "instructions": (
        "Is this finding about an exam, quiz, midterm or final: its date, place, rules or results? "
        "The text may be in Turkish (sınav, vize, final, quiz).")},
    "schedule_change": {"type": "noul", "instructions": (
        "Does this finding change, or announce a change to, a deadline, an exam date, a class time or a place? "
        '`finding.event` equal to "due_changed" means the deadline itself changed.')},
    "action_required": {"type": "noul", "instructions": (
        "Does the student need to take a concrete action because of this finding "
        "(submit, register, prepare, attend, reply)?")},
    "needs_explanation": {"type": "noul", "instructions": (
        "Would a short plain-language explanation of what is being asked help the student? True for long, complex "
        "or instruction-heavy texts, assignments with specific requirements and announcements with rules. False for "
        "self-explanatory items such as a single lecture file, a grade or a one-line notice.")},
    "push_now": {"type": "noul", "instructions": (
        "Should the student get an alert on their phone right now because missing or delaying this could cost them? "
        "Use `facts` for timing and do not compute dates yourself. Typical true cases: `facts.due_within_24h` is true "
        "and `facts.submitted` is false; `facts.due_moved_earlier` is true; an exam date or place change; an urgent "
        "instruction. False for routine materials, far-away deadlines, already submitted work and ordinary grades.")},
}
LABELS = {
    "requires_submission": "Teslim gerektiriyor",
    "exam_related": "Sınavla ilgili",
    "schedule_change": "Tarih değişikliği",
    "action_required": "Eylem gerekiyor",
}


class JevError(Exception):
    pass


@dataclass
class JevDecision:
    probs: dict[str, float]
    model: str = ""
    tokens: int = 0
    labels: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.labels = [label for qid, label in LABELS.items() if self.probs.get(qid, 0) >= 0.5]

    def p(self, qid: str) -> float:
        return self.probs.get(qid, 0.0)


def build_state(event: Event, item: dict | None, now: datetime) -> dict:
    """Bulgu + kodda hesaplanmış gerçekler. Kimlik bilgisi ya da iç kimlikler girmez."""
    data = event.data
    kind = data.get("kind", "")
    due = parse_dt(data.get("due_at"))
    old = parse_dt(data.get("old_due_at"))
    hours = round((due - now).total_seconds() / 3600, 1) if due else None
    submitted = None
    if kind == "assignment":
        submitted = bool((data.get("meta") or {}).get("submitted")) or bool(
            item and (item.get("submitted") or item.get("done_manual")))
    facts = {
        "hours_until_due": max(hours, 0) if hours is not None else None,
        "deadline_passed": hours is not None and hours <= 0,
        "due_within_24h": hours is not None and 0 < hours <= 24,
        "due_within_72h": hours is not None and 0 < hours <= 72,
        "submitted": submitted,
        "due_added": event.type == "due_changed" and old is None,
        "due_moved_earlier": bool(event.type == "due_changed" and old and due and due < old),
        "due_moved_later": bool(event.type == "due_changed" and old and due and due > old),
        "grade": (data.get("extra") or {}).get("value"),
    }
    finding = {
        "event": event.type, "kind": kind, "course": data.get("course") or "", "title": data.get("title") or "",
        "section": (data.get("extra") or {}).get("section"), "text": (data.get("body") or "")[:TEXT_LIMIT],
    }
    return {"finding": {k: v for k, v in finding.items() if v not in (None, "")},
            "facts": {k: v for k, v in facts.items() if v is not None}}


class JevClient:
    def __init__(self, api_key: str, model: str = "jev-latest", *,
                 transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep):
        self._key = api_key
        self.model = model
        self._transport = transport
        self._sleep = sleep

    async def ask(self, state: dict, questions: dict[str, dict] | None = None) -> JevDecision:
        questions = questions or QUESTIONS
        payload = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self._key}"}
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                response = await client.post(API_URL, json=payload, headers=headers)
                if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                    await self._sleep(2 ** attempt)
                    continue
                break
        if response.status_code != 200:
            raise JevError(f"HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        probs = {}
        for qid, answer in (body.get("answers") or {}).items():
            if answer.get("type") == "noul" and answer.get("noul") is not None:
                probs[qid] = float(answer["noul"])
        missing = sorted(set(questions) - set(probs))
        if missing:
            raise JevError(f"cevapsız soru(lar): {', '.join(missing)}")
        usage = body.get("usage") or {}
        return JevDecision(probs, body.get("model", ""), int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))


def make_jev(settings) -> JevClient | None:
    return JevClient(settings.typesafe_api_key, settings.jev_model) if settings.typesafe_api_key else None

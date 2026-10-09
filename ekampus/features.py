"""Anahtara bağlı özellikler: LLM, JEV ve Pushover.

Bir özellik ancak .env'de anahtarı varsa çalışır; anahtarı olan özellik /ayarlar'dan açılıp kapatılabilir.
Karar her kullanımda yeniden verilir, böylece ayar değişince botu yeniden başlatmak gerekmez.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import prefs as PR
from .config import Settings
from .store import Store

FEATURES: list[tuple[str, str]] = [
    ("llm", "LLM asistanı"),
    ("jev", "JEV karar katmanı"),
    ("pushover", "Pushover"),
]
LABELS = dict(FEATURES)


@dataclass(frozen=True)
class FeatureState:
    key: str
    label: str
    missing: tuple[str, ...]  # .env'de eksik anahtarlar
    enabled: bool             # kullanıcı /ayarlar'dan açık bırakmış mı

    @property
    def has_key(self) -> bool:
        return not self.missing

    @property
    def usable(self) -> bool:
        return self.has_key and self.enabled

    def describe(self) -> str:
        if self.missing:
            return f"çalışmıyor: .env'de {', '.join(self.missing)} yok"
        return "açık" if self.enabled else "ayarlardan kapalı"


def state(settings: Settings, store: Store | None, key: str) -> FeatureState:
    enabled = PR.load(store).get(key, True) if store is not None else True
    return FeatureState(key, LABELS[key], tuple(settings.missing(key)), bool(enabled))


def states(settings: Settings, store: Store | None) -> list[FeatureState]:
    return [state(settings, store, key) for key, _ in FEATURES]


def usable(settings: Settings, store: Store | None, key: str) -> bool:
    return state(settings, store, key).usable


def unavailable(settings: Settings, store: Store | None) -> list[FeatureState]:
    """Anahtarı olmadığı için çalışamayan özellikler (kullanıcıya söylenmesi gerekenler)."""
    return [s for s in states(settings, store) if s.missing]

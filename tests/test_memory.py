"""Kalıcı hafıza: LLM'in öğrenci hakkında tuttuğu notlar (/hafiza)."""

from datetime import datetime, timezone

import pytest

from ekampus import messages as M
from ekampus.store import MEMORY_MAX, MemoryFull, Store

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)


def test_add_dedupes_and_normalizes():
    store = Store(":memory:")
    first, new = store.memory_add("  Ağlar   dersini bıraktı ", NOW)
    assert new and store.memory_list()[0]["text"] == "Ağlar dersini bıraktı"
    assert store.memory_add("AĞLAR dersini bıraktı", NOW) == (first, False)  # büyük/küçük harf farkı aynı not
    with pytest.raises(ValueError):
        store.memory_add("   ", NOW)
    assert len(store.memory_add("x" * 999, NOW)) == 2 and len(store.memory_list()[-1]["text"]) == 300


def test_limit_and_delete():
    store = Store(":memory:")
    for i in range(MEMORY_MAX):
        store.memory_add(f"not {i}", NOW)
    with pytest.raises(MemoryFull):
        store.memory_add("bir tane daha", NOW)
    first = store.memory_list()[0]["id"]
    assert store.memory_delete(first) == "not 0" and store.memory_delete(first) is None
    store.memory_add("bir tane daha", NOW)
    assert store.memory_clear() == MEMORY_MAX and store.memory_list() == []


def test_chat_history_clear_keeps_memory():
    store = Store(":memory:")
    store.memory_add("cuma günleri çalışıyor", NOW)
    store.chat_add("user", "selam", NOW)
    store.chat_clear()  # /unut
    assert [m["text"] for m in store.memory_list()] == ["cuma günleri çalışıyor"]


def test_memory_view():
    store = Store(":memory:")
    store.memory_add("vize <20 Ekim>", NOW)
    view = M.memory_view(store.memory_list())
    assert "#1 vize &lt;20 Ekim&gt;" in view.text
    assert view.buttons[0][0].data == "mem:del:1" and view.buttons[-1][0].data == "mem:clear"
    confirm = M.memory_view(store.memory_list(), confirm_clear=True)
    assert [b.data for b in confirm.buttons[0]] == ["mem:clear!", "mem:list"]
    assert "Henüz bir şey yok" in M.memory_view([]).text

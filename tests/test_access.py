"""Bota sadece sahibi erişebilmeli: özel sohbet + doğru sohbet + doğru kişi."""

from datetime import datetime, timezone

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from ekampus.bot import is_owner, owner_filter

OWNER = 111111111
STRANGER = 999


def message_update(chat: Chat, user: User, text: str = "/odevler") -> Update:
    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=msg)


def callback_update(chat: Chat, user: User) -> Update:
    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user, text="x")
    return Update(update_id=2, callback_query=CallbackQuery(id="1", from_user=user, chat_instance="c", message=msg, data="done:1"))


owner_user = User(id=OWNER, first_name="Sahip", is_bot=False)
stranger_user = User(id=STRANGER, first_name="Yabancı", is_bot=False)
owner_private = Chat(id=OWNER, type="private")
stranger_private = Chat(id=STRANGER, type="private")
group = Chat(id=-100123, type="supergroup", title="Grup")


@pytest.mark.parametrize("chat,user,allowed", [
    (owner_private, owner_user, True),          # sahip, kendi özel sohbeti
    (stranger_private, stranger_user, False),   # başkası kendi sohbetinden
    (group, owner_user, False),                 # sahip bile olsa grupta değil
    (group, stranger_user, False),
    (owner_private, stranger_user, False),      # tutarsız güncelleme: sohbet doğru, kişi yanlış
])
def test_owner_filter_and_callback_guard(chat, user, allowed):
    assert bool(owner_filter(OWNER).check_update(message_update(chat, user))) is allowed
    assert is_owner(callback_update(chat, user), OWNER) is allowed


def test_no_owner_configured_allows_nobody():
    assert is_owner(callback_update(owner_private, owner_user), None) is False


def test_stranger_alert_once_per_day(settings):
    from ekampus.engine import Engine
    from ekampus.store import Store

    engine = Engine(settings, Store(":memory:"))
    now = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    for _ in range(5):
        engine.alert(f"stranger:{STRANGER}:2026-10-09", "deneme", now, urgent=False)
    assert len(engine.store.pending(now)) == 1

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ekampus.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        username="u", password="p", telegram_token="t", telegram_owner_chat_id=1,
        llm_provider="deepseek", llm_api_key="", llm_model="", llm_base_url="https://api.deepseek.com",
        llm_daily_token_budget=1000, tz=ZoneInfo("Europe/Istanbul"), poll_interval_min=15,
        night_poll_interval_min=60, night_start=time(1, 0), night_end=time(7, 0), daily_digest_time=time(8, 0),
        reminder_hours=(24, 3), live_lesson_reminder_min=15, data_dir=tmp_path, headless=True,
    )

"""Keşif güvenlik kuralları ve login kilidi."""

import json

import pytest

from ekampus.browser import AuthBlocked, AuthGuard, LoginCooldown
from ekampus.explore import deny_reason, url_pattern

BASE = "https://student.ekampus.ticaret.edu.tr"


@pytest.mark.parametrize("url,text", [
    (f"{BASE}/Account/Logout", ""),
    (f"{BASE}/Home/Index", "Çıkış Yap"),
    (f"{BASE}/Assignment/Submit/12", ""),
    (f"{BASE}/Odev/Teslim/12", ""),
    (f"{BASE}/Exam/Start/5", ""),
    (f"{BASE}/Course/Detail/5", "Sınava Başla"),
    (f"{BASE}/Live/Join/9", ""),
    (f"{BASE}/Course/Detail/5", "Derse Katıl"),
    (f"{BASE}/File/Upload", ""),
    (f"{BASE}/Message/Delete/3", ""),
    (f"{BASE}/Message/Detail/3", "Sil"),
    (f"{BASE}/content/enroll/2066?ceid=21789&GroupId=967", "lecture 3"),
    (f"{BASE}/home/sync", "tıklayın"),
])
def test_state_changing_links_are_denied(url, text):
    assert deny_reason(url, text)


@pytest.mark.parametrize("url,text", [
    (f"{BASE}/", "Ana Sayfa"),
    (f"{BASE}/Course/Detail/5", "Veri Yapıları"),
    (f"{BASE}/Announcement/List", "Duyurular"),
    (f"{BASE}/Calendar", "Takvim"),
    (f"{BASE}/Course/Silabus/5", "Ders İzlencesi"),  # "sil" alt dizesi yanlış pozitif vermemeli
])
def test_read_only_links_are_allowed(url, text):
    assert deny_reason(url, text) is None


def test_url_pattern_groups_ids():
    assert url_pattern(f"{BASE}/Course/Detail/5") == url_pattern(f"{BASE}/Course/Detail/123")
    assert url_pattern(f"{BASE}/X?id=1&tab=a") == url_pattern(f"{BASE}/X?tab=b&id=2")


def test_guard_blocks_same_credentials_until_reset(tmp_path):
    path = tmp_path / "guard.json"
    guard = AuthGuard(path, "user", "yanlis")
    guard.check()
    guard.record_attempt()
    guard.block("Giriş reddedildi")
    with pytest.raises(AuthBlocked):
        AuthGuard(path, "user", "yanlis").check()
    assert "yanlis" not in path.read_text(encoding="utf-8")  # şifre diske yazılmaz

    # şifre değişince kilit kalkar (ama bekleme süresi hâlâ geçerli)
    with pytest.raises(LoginCooldown):
        AuthGuard(path, "user", "dogru").check()

    guard.reset()
    AuthGuard(path, "user", "yanlis").check()


def test_guard_cooldown_between_attempts(tmp_path):
    path = tmp_path / "guard.json"
    guard = AuthGuard(path, "u", "p")
    guard.record_attempt()
    with pytest.raises(LoginCooldown):
        guard.check()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["last_attempt_ts"] -= 11 * 60
    path.write_text(json.dumps(data), encoding="utf-8")
    guard.check()

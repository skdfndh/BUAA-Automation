from unittest.mock import Mock

import pytest

from autoboya import session
from autoboya.exceptions import InteractiveLoginRequired, OperationInProgress
from autoboya.models import UserRecord
from autoboya.scheduler import AutomationRunner, journal_key
from autoboya.storage import AutoBoyaStore


def test_remove_user_cleans_only_its_credentials_and_caches(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="first", password_ref="keyring"))
    store.upsert_user(UserRecord(username="second", password_ref="unsafe-file"))
    store.save_unsafe_password("first", "old-secret")
    store.save_unsafe_password("second", "other-secret")
    store.save_json("sessions/first.json", {"bykc_token": "first-token"})
    store.save_json("sessions/second.json", {"bykc_token": "second-token"})
    store.save_json("cache/selected.json", {"first": [], "second": [{"id": 2}]})
    store.save_json("cache/statistics.json", {"first": {}, "second": {"validCount": 1}})
    store.save_json("cache/action_journal.json", {
        journal_key("first", "sign", 1): "signed", "first:select_failed:1": 2,
        journal_key("second", "sign", 2): "signed",
    })
    delete_password = Mock()
    monkeypatch.setattr("keyring.get_password", lambda service, user: "mock-secret")
    monkeypatch.setattr("keyring.delete_password", delete_password)

    assert store.remove_user("first")

    delete_password.assert_called_once_with("autoboya", "first")
    assert store.get_user("first") is None
    assert store.get_user("second") is not None
    assert store.load_json("secrets.json") == {"second": "other-secret"}
    assert not store.path("sessions/first.json").exists()
    assert store.path("sessions/second.json").exists()
    assert store.load_json("cache/selected.json") == {"second": [{"id": 2}]}
    assert store.load_json("cache/statistics.json") == {"second": {"validCount": 1}}
    assert store.load_json("cache/action_journal.json") == {journal_key("second", "sign", 2): "signed"}


def test_keyring_failure_preserves_user_for_retry(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="audit", password_ref="keyring"))
    monkeypatch.setattr("keyring.get_password", lambda service, user: "mock-secret")
    monkeypatch.setattr("keyring.delete_password", Mock(side_effect=RuntimeError("钥匙串暂不可用")))
    with pytest.raises(RuntimeError, match="钥匙串暂不可用"):
        store.remove_user("audit")
    assert store.get_user("audit") is not None


def test_remove_is_blocked_while_automation_owns_lock(tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="audit"))
    with store.lock("run/autoboya.lock"):
        with pytest.raises(OperationInProgress):
            store.remove_user("audit")
    assert store.get_user("audit") is not None


def test_captcha_waits_for_manual_login_before_retry(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    runner = AutomationRunner(store)
    ensure = Mock(side_effect=[InteractiveLoginRequired("需要验证码"), "new-client"])
    monkeypatch.setattr("autoboya.scheduler.ensure_bykc_client", ensure)
    for _ in range(3):
        with pytest.raises(InteractiveLoginRequired):
            runner._with_reauth("audit", lambda client: client)
    assert ensure.call_count == 1

    store.save_json("sessions/audit.json", {"bykc_token": "new-token"})
    assert runner._with_reauth("audit", lambda client: client) == "new-client"
    assert ensure.call_count == 2


def test_background_captcha_uses_distinct_actionable_error(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    monkeypatch.setattr(session, "password_for", lambda store, username: "mock-secret")
    auth = Mock()
    auth.preflight_login.side_effect = session.CaptchaRequired("mock-challenge")
    monkeypatch.setattr(session, "AuthClient", lambda store: auth)
    with pytest.raises(InteractiveLoginRequired, match="CAPTCHA required"):
        session.login_and_save(store, "audit")
